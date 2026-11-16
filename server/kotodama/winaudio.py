"""Windows audio through the system's own DLLs (ctypes only, no installs).

WaveOutHandle plays one line through winmm's waveOut API with the same
interface as player.PlayerHandle (write / finish_input / wait / kill), so
the engine can't tell it from a pw-play pipe.

The ctypes layer (WinMM) is loaded only on Windows; tests drive
WaveOutHandle with a fake that has the same methods.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from typing import Any, Callable

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
