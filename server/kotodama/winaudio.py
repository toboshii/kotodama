"""Windows audio through the system's own DLLs (ctypes only, no installs).

WaveOutHandle plays one line through winmm's waveOut API with the same
interface as player.PlayerHandle (write / finish_input / wait / kill), so
the engine can't tell it from a pw-play pipe.

CoreAudioBackend is the Ducker's backend on Windows: it lowers FFXI's
per-app session volume (the Volume Mixer slider) through Core Audio's COM
interfaces.

The ctypes layers (WinMM, CoreAudioCOM) are loaded only on Windows; tests
drive the rest with fakes that have the same methods.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from .audio import SESSION_KEY, DuckingUnavailable, SinkInput

log = logging.getLogger("kotodama.winaudio")

WAVE_MAPPER = 0xFFFFFFFF        # the default output device
WAVE_FORMAT_PCM = 1
CALLBACK_NULL = 0
WHDR_DONE = 0x00000001
WAVERR_STILLPLAYING = 33
MMSYSERR_NOERROR = 0

# About as much audio as a full 64 KB pipe holds at 24 kHz on Linux, so
# write() paces a download the same way on both platforms.
MAX_QUEUED_SECONDS = 1.5
# Silence queued after the last buffer: the device reports a buffer done
# slightly before its tail is heard, and closing then would clip it.
TAIL_SECONDS = 0.08
POLL_SECONDS = 0.01


class WinMMError(OSError):
    def __init__(self, what: str, code: int, text: str = ""):
        super().__init__(f"{what} failed ({code}{': ' + text if text else ''})")
        self.code = code


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1                  # mmsystem.h is byte-packed: sizeof == 18
    _fields_ = [("wFormatTag", ctypes.c_ushort), ("nChannels", ctypes.c_ushort),
                ("nSamplesPerSec", ctypes.c_uint32), ("nAvgBytesPerSec", ctypes.c_uint32),
                ("nBlockAlign", ctypes.c_ushort), ("wBitsPerSample", ctypes.c_ushort),
                ("cbSize", ctypes.c_ushort)]


class WAVEHDR(ctypes.Structure):
    _fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", ctypes.c_uint32),
                ("dwBytesRecorded", ctypes.c_uint32), ("dwUser", ctypes.c_size_t),
                ("dwFlags", ctypes.c_uint32), ("dwLoops", ctypes.c_uint32),
                ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_size_t)]


class _Header:
    """A prepared WAVEHDR and the buffer it points into. Both must stay
    alive until the device is done with them."""

    __slots__ = ("hdr", "buf", "size")

    def __init__(self, hdr: WAVEHDR, buf: Any, size: int):
        self.hdr, self.buf, self.size = hdr, buf, size


class WinMM:
    """The handful of waveOut calls a line needs."""

    def __init__(self, dll: Any = None):
        self.dll = dll if dll is not None else ctypes.WinDLL("winmm")   # type: ignore[attr-defined]
        d = self.dll
        hwo, phdr, uint = ctypes.c_void_p, ctypes.POINTER(WAVEHDR), ctypes.c_uint
        d.waveOutOpen.argtypes = [ctypes.POINTER(ctypes.c_void_p), uint, ctypes.POINTER(WAVEFORMATEX),
                                  ctypes.c_size_t, ctypes.c_size_t, ctypes.c_uint32]
        for name in ("waveOutPrepareHeader", "waveOutUnprepareHeader", "waveOutWrite"):
            getattr(d, name).argtypes = [hwo, phdr, uint]
        for name in ("waveOutReset", "waveOutClose"):
            getattr(d, name).argtypes = [hwo]
        d.waveOutGetErrorTextW.argtypes = [uint, ctypes.c_wchar_p, uint]
        for name in ("waveOutOpen", "waveOutPrepareHeader", "waveOutUnprepareHeader", "waveOutWrite",
                     "waveOutReset", "waveOutClose", "waveOutGetErrorTextW"):
            getattr(d, name).restype = uint

    def _check(self, code: int, what: str) -> None:
        if code != MMSYSERR_NOERROR:
            buf = ctypes.create_unicode_buffer(256)
            text = buf.value if self.dll.waveOutGetErrorTextW(code, buf, 256) == 0 else ""
            raise WinMMError(what, code, text)

    def open(self, rate: int) -> Any:
        fmt = WAVEFORMATEX(WAVE_FORMAT_PCM, 1, rate, rate * 2, 2, 16, 0)
        dev = ctypes.c_void_p()
        self._check(self.dll.waveOutOpen(ctypes.byref(dev), WAVE_MAPPER, ctypes.byref(fmt), 0, 0,
                                         CALLBACK_NULL), "waveOutOpen")
        return dev

    def prepare(self, dev: Any, data: bytes) -> _Header:
        buf = ctypes.create_string_buffer(data, len(data))
        hdr = WAVEHDR()
        hdr.lpData = ctypes.addressof(buf)
        hdr.dwBufferLength = len(data)
        self._check(self.dll.waveOutPrepareHeader(dev, ctypes.byref(hdr), ctypes.sizeof(WAVEHDR)),
                    "waveOutPrepareHeader")
        return _Header(hdr, buf, len(data))

    def write(self, dev: Any, h: _Header) -> None:
        self._check(self.dll.waveOutWrite(dev, ctypes.byref(h.hdr), ctypes.sizeof(WAVEHDR)), "waveOutWrite")

    @staticmethod
    def done(h: _Header) -> bool:
        return bool(h.hdr.dwFlags & WHDR_DONE)

    def unprepare(self, dev: Any, h: _Header) -> bool:
        """False while the device still owns the buffer."""
        code = self.dll.waveOutUnprepareHeader(dev, ctypes.byref(h.hdr), ctypes.sizeof(WAVEHDR))
        if code == WAVERR_STILLPLAYING:
            return False
        self._check(code, "waveOutUnprepareHeader")
        return True

    def reset(self, dev: Any) -> None:
        self._check(self.dll.waveOutReset(dev), "waveOutReset")

    def close(self, dev: Any) -> None:
        self._check(self.dll.waveOutClose(dev), "waveOutClose")


def load_winmm() -> WinMM | None:
    """The real waveOut API, or None off Windows or if winmm won't load."""
    if sys.platform != "win32":
        return None
    try:
        return WinMM()
    except (OSError, AttributeError) as exc:
        log.error("could not load winmm.dll: %s", exc)
        return None


class WaveOutHandle:
    """One playing line on the default output device (16-bit mono PCM).

    write() queues a buffer and blocks while more than MAX_QUEUED_SECONDS
    are waiting to play, like a full pipe. kill() may be called from any
    thread; it stops the sound at once and wakes a blocked write()."""

    def __init__(self, mm: Any, rate: int, *, max_queued: float = MAX_QUEUED_SECONDS,
                 tail: float = TAIL_SECONDS, poll: float = POLL_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self._mm = mm
        self._lock = threading.Lock()          # serializes every device call
        self._killed = threading.Event()
        self._pending: list[Any] = []          # headers the device hasn't finished
        self._queued = 0                       # bytes in _pending
        self._max_bytes = max(2, int(rate * max_queued)) * 2
        self._tail = b"\x00\x00" * int(rate * tail)
        self._poll = poll
        self._clock = clock
        self._finished = False
        self._wrote = False
        self._dev: Any = None
        try:
            self._dev = mm.open(rate)
        except OSError as exc:
            log.error("could not open the audio device: %s", exc)

    # -- PlayerHandle interface -------------------------------------------

    def write(self, data: bytes) -> bool:
        if not data:
            return self._dev is not None
        while True:
            with self._lock:
                if self._dev is None or self._killed.is_set() or self._finished:
                    return False
                self._reap()
                if self._queued < self._max_bytes:
                    return self._queue(data)
            if self._killed.wait(self._poll):
                return False

    def finish_input(self) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
            if self._dev is not None and self._wrote and self._tail:
                self._queue(self._tail)

    def wait(self, timeout: float | None = None) -> bool:
        """True once every buffer has played after finish_input(), or after kill()."""
        deadline = None if timeout is None else self._clock() + timeout
        while True:
            with self._lock:
                if self._dev is None:
                    return True
                self._reap()
                if self._finished and not self._pending:
                    self._close()
                    return True
            if deadline is not None:
                left = deadline - self._clock()
                if left <= 0:
                    return False
                self._killed.wait(min(self._poll, left))
            else:
                self._killed.wait(self._poll)

    def kill(self) -> None:
        self._killed.set()
        with self._lock:
            if self._dev is None:
                return
            try:
                self._mm.reset(self._dev)      # stops at once; every buffer becomes done
            except OSError as exc:
                log.debug("waveOutReset: %s", exc)
            self._reap()                       # all of them, after the reset
            self._close()

    # -- internals (lock held) --------------------------------------------

    def _queue(self, data: bytes) -> bool:
        try:
            h = self._mm.prepare(self._dev, data)
        except OSError as exc:
            log.error("audio device refused a buffer: %s", exc)
            self._fail()
            return False
        try:
            self._mm.write(self._dev, h)
        except OSError as exc:
            log.error("audio device refused a buffer: %s", exc)
            self._unprepare(h)
            self._fail()
            return False
        self._pending.append(h)
        self._queued += h.size
        self._wrote = True
        return True

    def _reap(self) -> None:
        """Unprepare finished buffers. One the device still owns stays in
        _pending, so its memory stays alive (say, if a reset failed)."""
        while self._pending and self._mm.done(self._pending[0]):
            h = self._pending.pop(0)
            self._queued -= h.size
            self._unprepare(h)

    def _unprepare(self, h: Any) -> None:
        try:
            self._mm.unprepare(self._dev, h)
        except OSError as exc:
            log.debug("waveOutUnprepareHeader: %s", exc)

    def _fail(self) -> None:
        try:
            self._mm.reset(self._dev)
        except OSError:
            pass
        self._reap()
        self._close()

    def _close(self) -> None:
        if self._dev is None:
            return
        try:
            self._mm.close(self._dev)
        except OSError as exc:
            log.debug("waveOutClose: %s", exc)
        self._dev = None


# -- Core Audio sessions (ducking) -------------------------------------------
#
# Windows keeps one volume per app session: the slider in the Volume Mixer.
# Ducking turns FFXI's down (pol.exe owns its audio) and back up, through
# COM interfaces called by vtable index. The indices follow the declaration
# order in mmdeviceapi.idl / audiopolicy.idl / audioclient.idl; every
# interface starts with IUnknown's QueryInterface (0), AddRef (1), Release (2).

S_OK = 0
CLSCTX_ALL = 0x17
COINIT_MULTITHREADED = 0x0
RPC_E_CHANGED_MODE = -2147417850          # 0x80010106: this thread already chose STA: usable
E_RENDER = 0
DEVICE_STATE_ACTIVE = 0x1
SESSION_EXPIRED = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

CLSID_MMDeviceEnumerator = "BCDE0395-E52F-467C-8E3D-C4579291692E"
IID_IMMDeviceEnumerator = "A95664D2-9614-4F35-A746-DE8DB63617E6"
IID_IAudioSessionManager2 = "77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F"
IID_IAudioSessionControl2 = "BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D"
IID_ISimpleAudioVolume = "87CE5498-68D6-44E5-9215-6DA47EF883D8"

# vtable indices
QUERY_INTERFACE, RELEASE = 0, 2
ENUM_AUDIO_ENDPOINTS = 3                  # IMMDeviceEnumerator
COLLECTION_GET_COUNT, COLLECTION_ITEM = 3, 4   # IMMDeviceCollection
DEVICE_ACTIVATE = 3                       # IMMDevice
GET_SESSION_ENUMERATOR = 5                # IAudioSessionManager2 (3, 4 are IAudioSessionManager's)
SESSIONS_GET_COUNT, SESSIONS_GET_SESSION = 3, 4   # IAudioSessionEnumerator
CONTROL_GET_STATE, CONTROL_GET_DISPLAY_NAME = 3, 4   # IAudioSessionControl
CONTROL2_GET_SESSION_IDENTIFIER, CONTROL2_GET_INSTANCE_IDENTIFIER, CONTROL2_GET_PROCESS_ID = 12, 13, 14
CONTROL2_IS_SYSTEM_SOUNDS = 15
VOLUME_SET_MASTER, VOLUME_GET_MASTER = 3, 4      # ISimpleAudioVolume

VOLUME_SCALE = 65536                      # pactl's 100%, so the Ducker's arithmetic is shared


@dataclass
class SessionInfo:
    """One audio session, as read from Core Audio."""
    instance: str                         # unique to this run of the app
    identifier: str                       # the same every time the same exe plays
    pid: int
    exe: str
    display: str
    volume: float                         # 0.0 - 1.0
    system: bool = False                  # the "System Sounds" session


def exe_from_identifier(identifier: str) -> str:
    """The exe name inside a session identifier such as
    "{0.0.0.00000000}.{guid}|\\Device\\HarddiskVolume3\\...\\pol.exe%b{guid}"."""
    _, sep, rest = identifier.partition("|")
    if not sep:
        return ""
    path = rest.split("%b", 1)[0]
    return path.replace("/", "\\").rsplit("\\", 1)[-1]


def session_stream(info: SessionInfo, own_pid: int) -> SinkInput | None:
    """A session as the Ducker's stream type, or None for ours and System Sounds."""
    if info.system or info.pid == own_pid or not info.instance:
        return None
    exe = info.exe or exe_from_identifier(info.identifier)
    props = {"application.process.binary": exe,
             "application.name": info.display if info.display and not info.display.startswith("@") else exe,
             "application.process.id": str(info.pid),
             SESSION_KEY: info.identifier}
    return SinkInput(id=info.instance, volumes=[int(round(max(0.0, min(1.0, info.volume)) * VOLUME_SCALE))],
                     props=props)


class CoreAudioBackend:
    """Ducking backend for Windows: per-app session volumes."""

    name = "Core Audio"
    missing_hint = "Windows audio (Core Audio) is unavailable"

    def __init__(self, com: Any = None, own_pid: int | None = None):
        self._com = com
        self.own_pid = os.getpid() if own_pid is None else own_pid
        self._lock = threading.Lock()
        self._volumes: dict[str, Any] = {}    # instance id -> ISimpleAudioVolume from the last scan

    def _layer(self) -> Any:
        if self._com is None:
            self._com = CoreAudioCOM()
        return self._com

    def list_streams(self) -> list[SinkInput]:
        try:
            found = self._layer().sessions()
        except OSError as exc:
            raise DuckingUnavailable(f"{self.missing_hint} ({exc})") from exc
        streams: list[SinkInput] = []
        keep: dict[str, Any] = {}
        for info, volume in found:
            st = session_stream(info, self.own_pid)
            if st is None or st.id in keep:
                self._layer().release(volume)
                continue
            keep[st.id] = volume
            streams.append(st)
        with self._lock:
            old, self._volumes = self._volumes, keep
        for volume in old.values():
            self._layer().release(volume)
        return streams

    def set_volumes(self, sid: Any, volumes: list[int]) -> bool:
        with self._lock:
            volume = self._volumes.get(str(sid))
        if volume is None or not volumes:
            return False
        level = max(0.0, min(1.0, volumes[0] / VOLUME_SCALE))
        try:
            return self._layer().set_volume(volume, level)
        except OSError:
            return False

    def close(self) -> None:
        with self._lock:
            old, self._volumes = self._volumes, {}
        for volume in old.values():
            self._layer().release(volume)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> "GUID":
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


_FUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)   # stdcall on Windows


def _call(ptr: Any, index: int, *args: Any, argtypes: tuple = ()) -> int:
    """Call method `index` of the COM object at ptr; returns the HRESULT."""
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = _FUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    return proto(vtable[index])(ptr, *args)


def _check(hr: int, what: str) -> None:
    if hr < 0:
        raise OSError(f"{what} failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")


class CoreAudioCOM:
    """The real COM walk. Windows only; every thread that uses it is
    initialized for COM (multithreaded apartment) on first use."""

    def __init__(self, ole32: Any = None, kernel32: Any = None) -> None:
        self.ole32 = ole32 if ole32 is not None else ctypes.WinDLL("ole32")              # type: ignore[attr-defined]
        self.kernel32 = kernel32 if kernel32 is not None else ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
        self.ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.ole32.CoInitializeEx.restype = ctypes.c_long
        self.ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, ctypes.c_uint32,
                                                ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        self.ole32.CoCreateInstance.restype = ctypes.c_long
        self.ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        self.ole32.CoTaskMemFree.restype = None
        self.kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        self.kernel32.OpenProcess.restype = ctypes.c_void_p
        self.kernel32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p,
                                                            ctypes.POINTER(ctypes.c_uint32)]
        self.kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
        self.kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel32.CloseHandle.restype = ctypes.c_int
        self._local = threading.local()
        self._guids = {name: GUID.parse(value) for name, value in (
            ("clsid_enum", CLSID_MMDeviceEnumerator), ("iid_enum", IID_IMMDeviceEnumerator),
            ("iid_mgr2", IID_IAudioSessionManager2), ("iid_ctl2", IID_IAudioSessionControl2),
            ("iid_vol", IID_ISimpleAudioVolume))}

    # -- COM plumbing -----------------------------------------------------

    def _init_thread(self) -> None:
        if getattr(self._local, "ready", False):
            return
        hr = self.ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        if hr < 0 and hr != RPC_E_CHANGED_MODE:
            _check(hr, "CoInitializeEx")
        self._local.ready = True

    def release(self, ptr: Any) -> None:
        if ptr:
            _call(ptr, RELEASE)

    def _out_ptr(self, ptr: Any, index: int, what: str, *args: Any, argtypes: tuple = ()) -> ctypes.c_void_p:
        out = ctypes.c_void_p()
        _check(_call(ptr, index, *args, ctypes.byref(out),
                     argtypes=(*argtypes, ctypes.POINTER(ctypes.c_void_p))), what)
        return out

    def _query(self, ptr: Any, iid: str, what: str) -> ctypes.c_void_p:
        return self._out_ptr(ptr, QUERY_INTERFACE, what, ctypes.byref(self._guids[iid]),
                             argtypes=(ctypes.POINTER(GUID),))

    def _string(self, ptr: Any, index: int) -> str:
        out = ctypes.c_void_p()
        if _call(ptr, index, ctypes.byref(out), argtypes=(ctypes.POINTER(ctypes.c_void_p),)) < 0 or not out:
            return ""
        try:
            return ctypes.wstring_at(out.value)
        finally:
            self.ole32.CoTaskMemFree(out)

    def _count(self, ptr: Any, index: int, what: str) -> int:
        n = ctypes.c_uint32()
        _check(_call(ptr, index, ctypes.byref(n), argtypes=(ctypes.POINTER(ctypes.c_uint32),)), what)
        return int(n.value)

    def _exe(self, pid: int) -> str:
        if pid <= 0:
            return ""
        handle = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_uint32(len(buf))
            if not self.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            return buf.value.replace("/", "\\").rsplit("\\", 1)[-1]
        finally:
            self.kernel32.CloseHandle(handle)

    # -- the walk ---------------------------------------------------------

    def sessions(self) -> list[tuple[SessionInfo, Any]]:
        """Every session on every active output device, with its
        ISimpleAudioVolume (the caller releases those)."""
        self._init_thread()
        enum = ctypes.c_void_p()
        _check(self.ole32.CoCreateInstance(ctypes.byref(self._guids["clsid_enum"]), None, CLSCTX_ALL,
                                           ctypes.byref(self._guids["iid_enum"]), ctypes.byref(enum)),
               "CoCreateInstance(MMDeviceEnumerator)")
        found: list[tuple[SessionInfo, Any]] = []
        try:
            devices = self._out_ptr(enum, ENUM_AUDIO_ENDPOINTS, "EnumAudioEndpoints", E_RENDER,
                                    DEVICE_STATE_ACTIVE, argtypes=(ctypes.c_int, ctypes.c_uint32))
            try:
                for i in range(self._count(devices, COLLECTION_GET_COUNT, "GetCount(devices)")):
                    device = self._out_ptr(devices, COLLECTION_ITEM, "Item", i, argtypes=(ctypes.c_uint32,))
                    try:
                        found.extend(self._device_sessions(device))
                    except OSError as exc:
                        log.debug("skipping an audio device: %s", exc)
                    finally:
                        self.release(device)
            finally:
                self.release(devices)
        finally:
            self.release(enum)
        return found

    def _device_sessions(self, device: Any) -> list[tuple[SessionInfo, Any]]:
        mgr = self._out_ptr(device, DEVICE_ACTIVATE, "Activate(IAudioSessionManager2)",
                            ctypes.byref(self._guids["iid_mgr2"]), CLSCTX_ALL, None,
                            argtypes=(ctypes.POINTER(GUID), ctypes.c_uint32, ctypes.c_void_p))
        out: list[tuple[SessionInfo, Any]] = []
        try:
            sessions = self._out_ptr(mgr, GET_SESSION_ENUMERATOR, "GetSessionEnumerator")
            try:
                n = ctypes.c_int()
                _check(_call(sessions, SESSIONS_GET_COUNT, ctypes.byref(n),
                             argtypes=(ctypes.POINTER(ctypes.c_int),)), "GetCount(sessions)")
                for i in range(n.value):
                    ctl = self._out_ptr(sessions, SESSIONS_GET_SESSION, "GetSession", i, argtypes=(ctypes.c_int,))
                    try:
                        item = self._session(ctl)
                    except OSError as exc:
                        log.debug("skipping an audio session: %s", exc)
                        item = None
                    finally:
                        self.release(ctl)
                    if item is not None:
                        out.append(item)
            finally:
                self.release(sessions)
        finally:
            self.release(mgr)
        return out

    def _session(self, ctl: Any) -> tuple[SessionInfo, Any] | None:
        state = ctypes.c_int()
        if _call(ctl, CONTROL_GET_STATE, ctypes.byref(state), argtypes=(ctypes.POINTER(ctypes.c_int),)) >= 0 \
                and state.value == SESSION_EXPIRED:
            return None
        ctl2 = self._query(ctl, "iid_ctl2", "QueryInterface(IAudioSessionControl2)")
        try:
            pid = ctypes.c_uint32()
            _call(ctl2, CONTROL2_GET_PROCESS_ID, ctypes.byref(pid), argtypes=(ctypes.POINTER(ctypes.c_uint32),))
            system = _call(ctl2, CONTROL2_IS_SYSTEM_SOUNDS) == S_OK
            identifier = self._string(ctl2, CONTROL2_GET_SESSION_IDENTIFIER)
            instance = self._string(ctl2, CONTROL2_GET_INSTANCE_IDENTIFIER)
        finally:
            self.release(ctl2)
        display = self._string(ctl, CONTROL_GET_DISPLAY_NAME)
        volume = self._query(ctl, "iid_vol", "QueryInterface(ISimpleAudioVolume)")
        level = ctypes.c_float()
        if _call(volume, VOLUME_GET_MASTER, ctypes.byref(level), argtypes=(ctypes.POINTER(ctypes.c_float),)) < 0:
            self.release(volume)
            return None
        info = SessionInfo(instance=instance, identifier=identifier, pid=int(pid.value),
                           exe=self._exe(int(pid.value)), display=display, volume=float(level.value),
                           system=system)
        return info, volume

    def set_volume(self, volume: Any, level: float) -> bool:
        self._init_thread()
        return _call(volume, VOLUME_SET_MASTER, ctypes.c_float(level), None,
                     argtypes=(ctypes.c_float, ctypes.c_void_p)) >= 0

    def get_volume(self, volume: Any) -> float | None:
        self._init_thread()
        level = ctypes.c_float()
        if _call(volume, VOLUME_GET_MASTER, ctypes.byref(level), argtypes=(ctypes.POINTER(ctypes.c_float),)) < 0:
            return None
        return float(level.value)


# -- closing the console window ----------------------------------------------

CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT = 2, 5, 6
_handlers: list[Any] = []                 # ctypes callbacks must outlive their registration


def install_close_handler(on_close: Callable[[], None], kernel32: Any = None) -> Any:
    """Run on_close when the console window is closed, or Windows logs off or
    shuts down. Windows gives a handler about 5 seconds, then ends the
    process; Ctrl+C is left to Python (KeyboardInterrupt)."""
    k32 = kernel32 if kernel32 is not None else ctypes.WinDLL("kernel32")   # type: ignore[attr-defined]
    proto = _FUNCTYPE(ctypes.c_int, ctypes.c_uint32)

    def handler(event: int) -> int:
        if event in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
            try:
                on_close()
            except Exception:  # noqa: BLE001 - nothing can be reported now
                log.exception("restoring on close failed")
            return 1
        return 0

    callback = proto(handler)
    _handlers.append(callback)
    if not k32.SetConsoleCtrlHandler(callback, 1):
        log.debug("SetConsoleCtrlHandler failed")
    return callback
