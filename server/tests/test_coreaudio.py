"""Ducking on Windows: Core Audio sessions behind the Ducker's backend
interface, driven through a fake COM layer so it runs on any platform."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import io
import json
import threading
import time
import types
import unittest
from ctypes import CFUNCTYPE, POINTER, c_float, c_int, c_long, c_uint32, c_void_p, cast
from unittest import mock

from kotodama import cli, engine as engine_mod, winaudio
from kotodama.audio import (SESSION_KEY, Ducker, DuckingUnavailable, PactlBackend, SinkInput, is_game_stream,
                            make_backend)
from kotodama.config import DEFAULTS
from kotodama.winaudio import CoreAudioBackend, SessionInfo, exe_from_identifier, session_stream

from helpers import KotodamaTestCase

MATCH = DEFAULTS["duck"]["match"]
OWN_PID = 4000
POL_ID = r"{0.0.0.00000000}.{11111111-2222}|\Device\HarddiskVolume3\Program Files (x86)\PlayOnline" \
         r"\SquareEnix\PlayOnlineViewer\pol.exe%b{00000000-0000-0000-0000-000000000000}"


def info(instance, pid, exe="", *, identifier=None, display="", volume=1.0, system=False):
    return SessionInfo(instance=instance, identifier=identifier if identifier is not None else f"id-{exe}",
                       pid=pid, exe=exe, display=display, volume=volume, system=system)


class _Vol:
    def __init__(self, level):
        self.level, self.released = level, False


class FakeCom:
    """Sessions as CoreAudioCOM.sessions() returns them, one fresh
    ISimpleAudioVolume per session per scan."""

    def __init__(self, sessions):
        self.sessions_now = list(sessions)
        self.levels = {s.instance: s.volume for s in sessions}
        self.handed_out: list[_Vol] = []
        self.sets: list[tuple[str, float]] = []
        self.fail = None
        self.fail_set = False
        self.scans = 0
        self.lock = threading.Lock()

    def sessions(self):
        self.scans += 1
        if self.fail:
            raise self.fail
        out = []
        with self.lock:
            for s in self.sessions_now:
                s.volume = self.levels.get(s.instance, s.volume)
                v = _Vol(s.volume)
                v.instance = s.instance
                self.handed_out.append(v)
                out.append((s, v))
        return out

    def set_volume(self, vol, level):
        if self.fail_set:
            raise OSError("RPC server unavailable")
        assert not vol.released, "volume used after release"
        with self.lock:
            self.levels[vol.instance] = level
            self.sets.append((vol.instance, level))
        return True

    def release(self, vol):
        assert not vol.released, "released twice"
        vol.released = True

    def live(self):
        return [v for v in self.handed_out if not v.released]


def standard_sessions():
    return [
        info("inst-pol", 1200, "pol.exe", identifier=POL_ID, display="", volume=0.8),
        info("inst-firefox", 1300, "firefox.exe", display="Firefox"),
        info("inst-us", OWN_PID, "python.exe"),                      # our own waveOut player
        info("inst-sys", 0, "", identifier="sys", display="@%SystemRoot%\\System32\\AudioSrv.Dll,-202",
             system=True),
    ]


class MappingTests(unittest.TestCase):
    def test_exe_from_the_session_identifier(self):
        self.assertEqual(exe_from_identifier(POL_ID), "pol.exe")
        self.assertEqual(exe_from_identifier("x|C:/Games/ffxi/pol.exe"), "pol.exe")
        self.assertEqual(exe_from_identifier("no separator"), "")

    def test_a_session_becomes_a_stream(self):
        st = session_stream(info("inst", 1200, "", identifier=POL_ID, volume=0.8), OWN_PID)
        self.assertEqual(st.id, "inst")
        self.assertEqual(st.volumes, [round(0.8 * 65536)])
        self.assertEqual(st.props["application.process.binary"], "pol.exe", "from the identifier")
        self.assertEqual(st.props["application.name"], "pol.exe")
        self.assertEqual(st.props["application.process.id"], "1200")
        self.assertEqual(st.props[SESSION_KEY], POL_ID)
        self.assertTrue(is_game_stream(st, MATCH))

    def test_display_names_and_clamping(self):
        named = session_stream(info("a", 1, "firefox.exe", display="Firefox", volume=1.7), OWN_PID)
        self.assertEqual((named.props["application.name"], named.volumes), ("Firefox", [65536]))
        resource = session_stream(info("b", 1, "game.exe", display="@%SystemRoot%\\x.dll,-1"), OWN_PID)
        self.assertEqual(resource.props["application.name"], "game.exe", "an unresolved resource name")

    def test_ours_and_system_sounds_are_left_out(self):
        self.assertIsNone(session_stream(info("us", OWN_PID, "python.exe"), OWN_PID))
        self.assertIsNone(session_stream(info("sys", 0, system=True), OWN_PID))
        self.assertIsNone(session_stream(info("", 5, "x.exe"), OWN_PID))

    def test_own_players_match_with_or_without_exe(self):
        ffplay = SinkInput(id="f", volumes=[65536], props={"application.process.binary": "FFPLAY.EXE",
                                                           "application.name": "ffplay.exe"})
        self.assertFalse(is_game_stream(ffplay, ["ffplay"]))
        pol = SinkInput(id="p", volumes=[65536], props={"application.process.binary": "pol.exe"})
        self.assertTrue(is_game_stream(pol, MATCH))


class BackendTests(unittest.TestCase):
    def backend(self, sessions=None):
        com = FakeCom(sessions if sessions is not None else standard_sessions())
        return com, CoreAudioBackend(com, own_pid=OWN_PID)

    def test_lists_streams_and_keeps_only_their_volumes(self):
        com, be = self.backend()
        streams = be.list_streams()
        self.assertEqual([s.id for s in streams], ["inst-pol", "inst-firefox"])
        self.assertEqual({v.instance for v in com.live()}, {"inst-pol", "inst-firefox"},
                         "skipped sessions' volumes are released at once")
        be.list_streams()
        self.assertEqual(len(com.live()), 2, "a rescan releases the previous scan's volumes")
        be.close()
        self.assertEqual(com.live(), [])

    def test_set_volumes_scales_and_reports_gone_streams(self):
        com, be = self.backend()
        be.list_streams()
        self.assertTrue(be.set_volumes("inst-pol", [32768]))
        self.assertTrue(be.set_volumes("inst-pol", [999999]))
        self.assertEqual(com.sets, [("inst-pol", 0.5), ("inst-pol", 1.0)])
        self.assertFalse(be.set_volumes("inst-gone", [100]))
        self.assertFalse(be.set_volumes("inst-pol", []))
        com.fail_set = True
        self.assertFalse(be.set_volumes("inst-pol", [100]))

    def test_no_core_audio_means_unavailable(self):
        com, be = self.backend()
        com.fail = OSError("CoCreateInstance failed")
        with self.assertRaises(DuckingUnavailable) as cm:
            be.list_streams()
        self.assertIn("Core Audio", str(cm.exception))
        self.assertIn("CoCreateInstance failed", str(cm.exception))

    def test_duplicate_instances_are_listed_once(self):
        twice = [info("inst-pol", 1200, "pol.exe"), info("inst-pol", 1200, "pol.exe")]
        com, be = self.backend(twice)
        self.assertEqual(len(be.list_streams()), 1)
        self.assertEqual(len(com.live()), 1)

    def test_platform_picks_the_backend(self):
        self.assertIsInstance(make_backend("win32"), CoreAudioBackend)
        self.assertIsInstance(make_backend("linux"), PactlBackend)


class DuckerOnWindowsTests(KotodamaTestCase):
    def ducker(self, com, **kw):
        params = dict(enabled=True, level=40, match=MATCH, attack_ms=30, release_ms=30, hold_ms=40,
                      state_path=self.tmp / "duck.json", backend=CoreAudioBackend(com, own_pid=OWN_PID))
        params.update(kw)
        return Ducker(**params)

    def test_ducks_ffxi_and_restores_it_exactly(self):
        com = FakeCom(standard_sessions())
        d = self.ducker(com)
        d.acquire()
        deadline = time.time() + 2
        while com.levels["inst-pol"] > 0.33 and time.time() < deadline:
            time.sleep(0.01)
        self.assertAlmostEqual(com.levels["inst-pol"], int(round(0.8 * 65536) * 0.4) / 65536, places=6)
        self.assertEqual(com.levels["inst-firefox"], 1.0, "other apps are untouched")
        self.assertNotIn("inst-us", [i for i, _ in com.sets], "our own audio is untouched")
        state = json.loads((self.tmp / "duck.json").read_text(encoding="utf-8"))
        self.assertEqual(state["streams"], {"inst-pol": [round(0.8 * 65536)]})
        self.assertEqual(state["keys"], {"inst-pol": POL_ID})
        d.release()
        self.assertTrue(d.settled(3))
        self.assertAlmostEqual(com.levels["inst-pol"], round(0.8 * 65536) / 65536, places=6)
        self.assertFalse((self.tmp / "duck.json").exists())
        d.shutdown()
        self.assertEqual(com.live(), [], "shutdown releases every COM pointer")

    def test_restore_after_a_crash_finds_a_restarted_game_by_its_identifier(self):
        # A crashed run ducked session "inst-old"; the game has restarted
        # since, and Windows brought its new session back at the ducked level.
        (self.tmp / "duck.json").write_text(json.dumps({
            "pid": 1, "streams": {"inst-old": [52429], "inst-gone": [65536]},
            "keys": {"inst-old": POL_ID, "inst-gone": "id-gone.exe"}}), encoding="utf-8")
        com = FakeCom([info("inst-new", 2200, "pol.exe", identifier=POL_ID, volume=0.32),
                       info("inst-firefox", 1300, "firefox.exe")])
        d = self.ducker(com)
        self.assertEqual(d.restore_stale(), 1)
        self.assertAlmostEqual(com.levels["inst-new"], 52429 / 65536, places=6)
        self.assertEqual(com.levels["inst-firefox"], 1.0)
        self.assertFalse((self.tmp / "duck.json").exists())

    def test_restore_prefers_the_same_session_and_sets_each_once(self):
        (self.tmp / "duck.json").write_text(json.dumps({
            "pid": 1, "streams": {"inst-pol": [52429], "inst-older": [30000]},
            "keys": {"inst-pol": POL_ID, "inst-older": POL_ID}}), encoding="utf-8")
        com = FakeCom([info("inst-pol", 2200, "pol.exe", identifier=POL_ID, volume=0.32)])
        d = self.ducker(com)
        self.assertEqual(d.restore_stale(), 1)
        self.assertEqual(com.sets, [("inst-pol", 52429 / 65536)])

    def test_two_copies_of_the_game_each_get_their_own_volume_back(self):
        # Multi-boxing: two pol.exe sessions share one identifier.
        (self.tmp / "duck.json").write_text(json.dumps({
            "pid": 1, "streams": {"inst-a": [52429], "inst-b": [32768]},
            "keys": {"inst-a": POL_ID, "inst-b": POL_ID}}), encoding="utf-8")
        com = FakeCom([info("inst-b", 2300, "pol.exe", identifier=POL_ID, volume=0.2),
                       info("inst-a", 2200, "pol.exe", identifier=POL_ID, volume=0.32)])
        self.assertEqual(self.ducker(com).restore_stale(), 2)
        self.assertAlmostEqual(com.levels["inst-a"], 52429 / 65536, places=6)
        self.assertAlmostEqual(com.levels["inst-b"], 32768 / 65536, places=6)

    def test_an_exact_session_is_never_taken_by_a_restarted_one(self):
        # inst-old's game was restarted (now inst-new); inst-b is still the
        # same session. inst-old comes first in the file, but must not take inst-b.
        (self.tmp / "duck.json").write_text(json.dumps({
            "pid": 1, "streams": {"inst-old": [52429], "inst-b": [32768]},
            "keys": {"inst-old": POL_ID, "inst-b": POL_ID}}), encoding="utf-8")
        com = FakeCom([info("inst-b", 2300, "pol.exe", identifier=POL_ID, volume=0.2),
                       info("inst-new", 2400, "pol.exe", identifier=POL_ID, volume=0.32)])
        self.assertEqual(self.ducker(com).restore_stale(), 2)
        self.assertAlmostEqual(com.levels["inst-b"], 32768 / 65536, places=6)
        self.assertAlmostEqual(com.levels["inst-new"], 52429 / 65536, places=6)

    def test_both_restarted_copies_come_back(self):
        (self.tmp / "duck.json").write_text(json.dumps({
            "pid": 1, "streams": {"inst-a": [52429], "inst-b": [52429]},
            "keys": {"inst-a": POL_ID, "inst-b": POL_ID}}), encoding="utf-8")
        com = FakeCom([info("inst-c", 2300, "pol.exe", identifier=POL_ID, volume=0.32),
                       info("inst-d", 2400, "pol.exe", identifier=POL_ID, volume=0.32)])
        self.assertEqual(self.ducker(com).restore_stale(), 2)
        self.assertEqual(sorted(i for i, _ in com.sets), ["inst-c", "inst-d"])

    def test_linux_state_files_without_keys_still_restore(self):
        (self.tmp / "duck.json").write_text(json.dumps({"pid": 1, "streams": {"inst-pol": [65536]}}),
                                            encoding="utf-8")
        com = FakeCom([info("inst-pol", 2200, "pol.exe", identifier=POL_ID, volume=0.4)])
        self.assertEqual(self.ducker(com).restore_stale(), 1)
        self.assertEqual(com.levels["inst-pol"], 1.0)

    def test_unavailable_core_audio_turns_ducking_off_with_one_notice(self):
        com = FakeCom(standard_sessions())
        com.fail = OSError("no audio service")
        notes: list[str] = []
        d = self.ducker(com, notify=notes.append)
        d.acquire()
        time.sleep(0.15)
        d.release()
        d.acquire()
        time.sleep(0.1)
        d.release()
        d.shutdown()
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("ducking is off: Windows audio (Core Audio) is unavailable", notes[0])
        self.assertEqual(com.scans, 1, "not retried every line once it's known to be missing")


class PactlMissingTests(KotodamaTestCase):
    def test_missing_pactl_turns_ducking_off_with_one_notice(self):
        calls: list[tuple] = []

        def no_pactl(*a):
            calls.append(a)
            raise FileNotFoundError("pactl")

        notes: list[str] = []
        d = Ducker(enabled=True, match=MATCH, attack_ms=10, release_ms=10, hold_ms=10, runner=no_pactl,
                   notify=notes.append)
        for _ in range(2):
            d.acquire()
            time.sleep(0.1)
            d.release()
            time.sleep(0.05)
        d.shutdown()
        self.assertEqual(len(calls), 1, "not retried once it's known to be missing")
        self.assertEqual(notes, ["ducking is off: `pactl` not found (install pulseaudio-utils or pipewire-pulse)"])
        with self.assertRaises(DuckingUnavailable):
            PactlBackend(no_pactl).list_streams()


class CloseHandlerTests(unittest.TestCase):
    class _Kernel32:
        def __init__(self):
            self.registered = []

        def SetConsoleCtrlHandler(self, callback, add):
            self.registered.append((callback, add))
            return 1

    def test_close_logoff_and_shutdown_restore_but_ctrl_c_does_not(self):
        calls: list[int] = []
        k32 = self._Kernel32()
        callback = winaudio.install_close_handler(lambda: calls.append(1), kernel32=k32)
        self.assertEqual(k32.registered, [(callback, 1)])
        self.assertIn(callback, winaudio._handlers, "kept alive while registered")
        self.assertEqual(callback(0), 0, "Ctrl+C is left to Python")
        self.assertEqual(callback(1), 0, "Ctrl+Break is left to Python")
        self.assertEqual(calls, [])
        for event in (winaudio.CTRL_CLOSE_EVENT, winaudio.CTRL_LOGOFF_EVENT, winaudio.CTRL_SHUTDOWN_EVENT):
            self.assertEqual(callback(event), 1)
        self.assertEqual(calls, [1, 1, 1])

    def test_a_failing_restore_is_swallowed(self):
        def boom():
            raise RuntimeError("boom")

        callback = winaudio.install_close_handler(boom, kernel32=self._Kernel32())
        with self.assertLogs("kotodama.winaudio", "ERROR"):
            self.assertEqual(callback(winaudio.CTRL_CLOSE_EVENT), 1)


# -- a fake COM world: real vtables in memory, calling back into Python ----

E_FAIL = -2147467259
GUIDS = {name: bytes(winaudio.GUID.parse(getattr(winaudio, name)))
         for name in ("CLSID_MMDeviceEnumerator", "IID_IMMDeviceEnumerator", "IID_IAudioSessionManager2",
                      "IID_IAudioSessionControl2", "IID_ISimpleAudioVolume")}


class _World:
    def __init__(self):
        self.objects: dict[int, "_Obj"] = {}
        self.strings: dict[int, ctypes.Array] = {}
        self.freed: list[int] = []
        self.inits: list[int] = []
        self.handles_closed: list[int] = []

    def string(self, text: str) -> int:
        buf = ctypes.create_unicode_buffer(text)
        addr = ctypes.addressof(buf)
        self.strings[addr] = buf
        return addr

    def unbalanced(self) -> dict[str, int]:
        """Objects with references handed out and not released."""
        return {o.name: o.refs - o.released for o in self.objects.values() if o.refs != o.released}


class _Obj:
    """A COM object: memory whose first field points at a vtable of
    CFUNCTYPE callbacks (stdcall and cdecl are the same off 32-bit Windows)."""

    def __init__(self, world: _World, name: str, methods: dict):
        self.world, self.name = world, name
        self.refs = self.released = 0
        table = {2: ((), self._release)}
        table.update(methods)
        self._callbacks = []
        self.vtable = (c_void_p * (max(table) + 1))()
        for index, (argtypes, fn) in table.items():
            cb = CFUNCTYPE(c_long, c_void_p, *argtypes)(lambda this, *args, fn=fn: fn(*args))
            self._callbacks.append(cb)
            self.vtable[index] = cast(cb, c_void_p).value
        self.body = (c_void_p * 1)(cast(self.vtable, c_void_p).value)
        self.address = cast(self.body, c_void_p).value
        world.objects[self.address] = self

    def _release(self):
        self.released += 1
        return 0

    def give(self, out) -> int:
        self.refs += 1
        out[0] = self.address
        return 0


PP = POINTER(c_void_p)


def fake_session(world, *, pid, ident, instance, display, level, state=1, system=False):
    vol_level = {"v": level}
    volume = _Obj(world, f"volume:{instance}", {
        3: ((c_float, c_void_p), lambda v, ctx: vol_level.__setitem__("v", v) or 0),
        4: ((POINTER(c_float),), lambda out: out.__setitem__(0, vol_level["v"]) or 0),
    })
    volume.level = vol_level

    def put_string(text):
        def method(out):
            out[0] = world.string(text)
            return 0
        return method

    def get_pid(out):
        out[0] = pid
        return 0

    ctl2 = _Obj(world, f"control2:{instance}", {
        12: ((PP,), put_string(ident)),
        13: ((PP,), put_string(instance)),
        14: ((POINTER(c_uint32),), get_pid),
        15: ((), lambda: 0 if system else 1),
    })

    def query(riid, out):
        wanted = bytes(riid.contents)
        if wanted == GUIDS["IID_IAudioSessionControl2"]:
            return ctl2.give(out)
        if wanted == GUIDS["IID_ISimpleAudioVolume"]:
            return volume.give(out)
        return -2147467262                     # E_NOINTERFACE

    def get_state(out):
        out[0] = state
        return 0

    ctl = _Obj(world, f"control:{instance}", {
        0: ((POINTER(winaudio.GUID), PP), query),
        3: ((POINTER(c_int),), get_state),
        4: ((PP,), put_string(display)),
    })
    return ctl, volume


def fake_device(world, name, sessions, *, activate_fails=False):
    def count(out):
        out[0] = len(sessions)
        return 0

    def get(i, out):
        return sessions[i][0].give(out)

    enum = _Obj(world, f"sessions:{name}", {3: ((POINTER(c_int),), count), 4: ((c_int, PP), get)})
    mgr = _Obj(world, f"manager:{name}", {5: ((PP,), enum.give)})

    def activate(riid, ctx, params, out):
        if activate_fails:
            return E_FAIL
        assert bytes(riid.contents) == GUIDS["IID_IAudioSessionManager2"]
        assert ctx == winaudio.CLSCTX_ALL and params is None
        return mgr.give(out)

    return _Obj(world, f"device:{name}", {3: ((POINTER(winaudio.GUID), c_uint32, c_void_p, PP), activate)})


def fake_system(world, devices, exes, *, create_fails=False):
    def dev_count(out):
        out[0] = len(devices)
        return 0

    collection = _Obj(world, "devices", {3: ((POINTER(c_uint32),), dev_count),
                                         4: ((c_uint32, PP), lambda i, out: devices[i].give(out))})

    def enum_endpoints(flow, mask, out):
        assert (flow, mask) == (winaudio.E_RENDER, winaudio.DEVICE_STATE_ACTIVE)
        return collection.give(out)

    enumerator = _Obj(world, "enumerator", {3: ((c_int, c_uint32, PP), enum_endpoints)})

    def co_initialize(reserved, flags):
        world.inits.append(threading.get_ident())
        return 0

    def co_create(clsid, outer, ctx, iid, out):
        if create_fails:
            return E_FAIL
        assert bytes(clsid._obj) == GUIDS["CLSID_MMDeviceEnumerator"]
        assert bytes(iid._obj) == GUIDS["IID_IMMDeviceEnumerator"]
        enumerator.refs += 1
        out._obj.value = enumerator.address
        return 0

    def co_free(ptr):
        world.freed.append(ptr.value if isinstance(ptr, c_void_p) else ptr)

    def open_process(access, inherit, pid):
        assert access == winaudio.PROCESS_QUERY_LIMITED_INFORMATION
        return 50000 + pid if pid in exes else 0

    def image_name(handle, flags, buf, size):
        buf.value = exes[handle - 50000]
        return 1

    def close_handle(handle):
        world.handles_closed.append(handle)
        return 1

    ole32 = types.SimpleNamespace(CoInitializeEx=co_initialize, CoCreateInstance=co_create,
                                  CoTaskMemFree=co_free)
    kernel32 = types.SimpleNamespace(OpenProcess=open_process, QueryFullProcessImageNameW=image_name,
                                     CloseHandle=close_handle)
    # Plain functions accept the argtypes/restype the real layer sets.
    for ns in (ole32, kernel32):
        for name in vars(ns):
            setattr(ns, name, (lambda f: (lambda *a: f(*a)))(getattr(ns, name)))
    return ole32, kernel32


class CoreAudioCOMTests(unittest.TestCase):
    """The ctypes layer itself, against fake COM objects."""

    def build(self, *, headphones_fail=False, create_fails=False):
        world = _World()
        pol = fake_session(world, pid=1200, ident=POL_ID, instance="inst-pol", display="", level=0.8)
        system = fake_session(world, pid=0, ident="sys", instance="inst-sys", display="@sys", level=1.0,
                              system=True)
        firefox = fake_session(world, pid=1300, ident="id-ff", instance="inst-ff", display="Firefox", level=0.5)
        expired = fake_session(world, pid=1400, ident="id-old", instance="inst-old", display="", level=1.0,
                               state=winaudio.SESSION_EXPIRED)
        speakers = fake_device(world, "speakers", [pol, system])
        headphones = fake_device(world, "headphones", [firefox, expired], activate_fails=headphones_fail)
        ole32, kernel32 = fake_system(world, [speakers, headphones],
                                      {1200: r"C:\PlayOnline\SquareEnix\PlayOnlineViewer\pol.exe",
                                       1300: r"C:\Program Files\Mozilla Firefox\firefox.exe"},
                                      create_fails=create_fails)
        return world, winaudio.CoreAudioCOM(ole32=ole32, kernel32=kernel32), {"pol": pol, "firefox": firefox}

    def test_walks_every_device_and_session(self):
        world, com, _ = self.build()
        found = com.sessions()
        infos = [i for i, _v in found]
        self.assertEqual([i.instance for i in infos], ["inst-pol", "inst-sys", "inst-ff"], "expired skipped")
        pol = infos[0]
        self.assertEqual((pol.pid, pol.exe, pol.identifier, pol.display), (1200, "pol.exe", POL_ID, ""))
        self.assertAlmostEqual(pol.volume, 0.8, places=6)
        self.assertTrue(infos[1].system)
        self.assertFalse(pol.system)
        self.assertEqual(infos[2].exe, "firefox.exe")
        # Every reference is released except the volumes handed to the caller.
        self.assertEqual(world.unbalanced(), {"volume:inst-pol": 1, "volume:inst-sys": 1, "volume:inst-ff": 1})
        self.assertEqual(sorted(world.freed), sorted(world.strings), "every string freed once")
        self.assertEqual(len(world.freed), len(set(world.freed)))
        self.assertEqual(sorted(world.handles_closed), [51200, 51300])
        for _i, v in found:
            com.release(v)
        self.assertEqual(world.unbalanced(), {})

    def test_volume_get_and_set(self):
        world, com, sessions = self.build()
        found = dict((i.instance, v) for i, v in com.sessions())
        self.assertAlmostEqual(com.get_volume(found["inst-ff"]), 0.5, places=6)
        self.assertTrue(com.set_volume(found["inst-ff"], 0.25))
        self.assertAlmostEqual(sessions["firefox"][1].level["v"], 0.25, places=6)
        self.assertAlmostEqual(com.get_volume(found["inst-ff"]), 0.25, places=6)

    def test_com_is_initialized_once_per_thread(self):
        world, com, _ = self.build()
        for _ in range(2):
            for _i, v in com.sessions():
                com.release(v)
        self.assertEqual(len(world.inits), 1)
        t = threading.Thread(target=lambda: [com.release(v) for _i, v in com.sessions()])
        t.start()
        t.join(5)
        self.assertEqual(len(world.inits), 2)
        self.assertEqual(world.unbalanced(), {})

    def test_a_failing_device_is_skipped(self):
        world, com, _ = self.build(headphones_fail=True)
        with self.assertLogs("kotodama.winaudio", "DEBUG"):
            found = com.sessions()
        self.assertEqual([i.instance for i, _v in found], ["inst-pol", "inst-sys"])
        for _i, v in found:
            com.release(v)
        self.assertEqual(world.unbalanced(), {})

    def test_no_enumerator_raises_os_error(self):
        world, com, _ = self.build(create_fails=True)
        with self.assertRaises(OSError) as cm:
            com.sessions()
        self.assertIn("0x80004005", str(cm.exception))

    def test_the_backend_on_the_real_layer(self):
        world, com, sessions = self.build()
        backend = CoreAudioBackend(com, own_pid=OWN_PID)
        streams = backend.list_streams()
        self.assertEqual([s.id for s in streams], ["inst-pol", "inst-ff"])
        self.assertTrue(backend.set_volumes("inst-pol", [26214]))
        self.assertAlmostEqual(sessions["pol"][1].level["v"], 26214 / 65536, places=6)
        backend.close()
        self.assertEqual(world.unbalanced(), {})


class _FakeServer:
    def __init__(self, cfg, engine):
        pass

    def serve_forever(self):
        pass

    def shutdown(self):
        pass


class CliTests(KotodamaTestCase):
    def windows_ducker(self):
        com = FakeCom(standard_sessions())
        return Ducker(enabled=True, match=MATCH, state_path=self.tmp / "duck.json",
                      backend=CoreAudioBackend(com, own_pid=OWN_PID))

    def test_serve_on_windows_restores_on_close(self):
        cfg = self.make_config(listen={"host": "127.0.0.1", "port": 0})
        engine = self.make_engine(cfg=cfg)
        with mock.patch.object(cli, "UDPServer", _FakeServer), mock.patch.object(cli.sys, "platform", "win32"), \
                mock.patch.object(winaudio, "install_close_handler") as install:
            cli.cmd_serve(cfg, argparse.Namespace(), stdin=io.StringIO(), engine=engine)
        install.assert_called_once_with(engine.ducker.shutdown)

    def test_serve_elsewhere_installs_no_handler(self):
        cfg = self.make_config(listen={"host": "127.0.0.1", "port": 0})
        engine = self.make_engine(cfg=cfg)
        with mock.patch.object(cli, "UDPServer", _FakeServer), mock.patch.object(cli.sys, "platform", "linux"), \
                mock.patch.object(winaudio, "install_close_handler") as install:
            cli.cmd_serve(cfg, argparse.Namespace(), stdin=io.StringIO(), engine=engine)
        install.assert_not_called()

    def test_audio_list_shows_windows_sessions_by_process(self):
        out = io.StringIO()
        with mock.patch.object(cli, "make_ducker", lambda *_a, **_k: self.windows_ducker()), \
                contextlib.redirect_stdout(out):
            rc = cli.cmd_audio(self.make_config(), argparse.Namespace(action="list"))
        self.assertEqual(rc, 0)
        lines = out.getvalue().splitlines()
        pol = [line for line in lines if "binary=pol.exe" in line]
        self.assertEqual(len(pol), 1, lines)
        self.assertTrue(pol[0].startswith("DUCK  pid 1200"), pol[0])
        self.assertIn("vol 80%", pol[0])
        self.assertNotIn("app_id", out.getvalue())
        self.assertNotIn("python.exe", out.getvalue(), "our own session isn't listed")

    def test_audio_without_a_backend(self):
        def no_pactl(*_a):
            raise FileNotFoundError("pactl")

        out = io.StringIO()
        ducker = Ducker(enabled=True, match=MATCH, runner=no_pactl)
        with mock.patch.object(cli, "make_ducker", lambda *_a, **_k: ducker), contextlib.redirect_stdout(out):
            rc = cli.cmd_audio(self.make_config(), argparse.Namespace(action="list"))
        self.assertEqual(rc, 1)
        self.assertIn("ducking is unavailable: `pactl` not found", out.getvalue())

    def test_doctor_reports_the_windows_backend(self):
        cfg = self.make_config(listen={"host": "127.0.0.1", "port": 0})
        engine = self.make_engine(cfg=cfg)
        com = FakeCom(standard_sessions())
        out = io.StringIO()
        with mock.patch.object(cli, "build_engine", lambda *_a, **_k: engine), \
                mock.patch.object(cli, "make_backend", lambda *_a, **_k: CoreAudioBackend(com, own_pid=OWN_PID)), \
                contextlib.redirect_stdout(out):
            cli.cmd_doctor(cfg, argparse.Namespace(env_file=None, live=False, provider=None))
        self.assertIn("[ok] ducking (Core Audio): pid 1200 pol.exe", out.getvalue())
        self.assertEqual(com.live(), [], "doctor releases what it scanned")

    def test_make_ducker_uses_the_platform_backend(self):
        cfg = self.make_config()
        with mock.patch.object(engine_mod, "make_backend", return_value="sentinel") as mb:
            d = engine_mod.make_ducker(cfg)
        mb.assert_called_once_with()
        self.assertEqual(d.backend, "sentinel")


if __name__ == "__main__":
    unittest.main()
