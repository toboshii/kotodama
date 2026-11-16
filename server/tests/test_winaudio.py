"""The built-in Windows player (waveOut), driven through a fake device so
it runs on any platform."""

from __future__ import annotations

import ctypes
import threading
import time
import unittest

from kotodama import protocol, winaudio
from kotodama.player import Player, PlayerHandle
from kotodama.winaudio import WAVEHDR, WHDR_DONE, WaveOutHandle, WinMM, WinMMError

from helpers import FakeTTS, KotodamaTestCase, speak_msg

RATE = 1000                       # 2000 bytes a second: the cap is 3000 bytes
CHUNK = b"\x01\x00" * 500         # 1000 bytes


class _Hdr:
    def __init__(self, data: bytes):
        self.data, self.size, self.done = data, len(data), False


class FakeWinMM:
    """A device that plays only when told to (play()), or at once (autoplay)."""

    def __init__(self, *, fail_open=False, fail_prepare_after=None, autoplay=False):
        self.fail_open, self.fail_prepare_after, self.autoplay = fail_open, fail_prepare_after, autoplay
        self.lock = threading.Lock()
        self.prepared: list[_Hdr] = []
        self.queue: list[_Hdr] = []
        self.unprepared: list[_Hdr] = []
        self.early_unprepare = 0
        self.resets = 0
        self.opened: list[str] = []
        self.closed_devs: list[str] = []
        self.rate = None

    def open(self, rate):
        if self.fail_open:
            raise WinMMError("waveOutOpen", 2, "no driver")
        self.rate = rate
        self.opened.append(f"dev{len(self.opened) + 1}")
        return self.opened[-1]

    def prepare(self, dev, data):
        assert dev in self.opened and dev not in self.closed_devs, "buffer for a closed device"
        if self.fail_prepare_after is not None and len(self.prepared) >= self.fail_prepare_after:
            raise WinMMError("waveOutPrepareHeader", 7)
        h = _Hdr(data)
        self.prepared.append(h)
        return h

    def write(self, dev, h):
        with self.lock:
            if self.autoplay:
                h.done = True
            else:
                self.queue.append(h)

    def done(self, h):
        with self.lock:
            return h.done

    def unprepare(self, dev, h):
        with self.lock:
            if not h.done:
                self.early_unprepare += 1
                return False
            self.unprepared.append(h)
            return True

    def reset(self, dev):
        with self.lock:
            self.resets += 1
            for h in self.queue:
                h.done = True
            self.queue.clear()

    def close(self, dev):
        with self.lock:
            if self.queue:
                raise WinMMError("waveOutClose", winaudio.WAVERR_STILLPLAYING)
            assert dev not in self.closed_devs, "closed twice"
            self.closed_devs.append(dev)

    @property
    def closed(self) -> int:
        return len(self.closed_devs)

    def play(self, n=1):
        with self.lock:
            for h in self.queue[:n]:
                h.done = True
            del self.queue[:n]

    def played_bytes(self) -> bytes:
        return b"".join(h.data for h in self.prepared)


def eventually(cond, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


class WaveOutHandleTests(unittest.TestCase):
    def handle(self, mm=None, **kw):
        mm = mm or FakeWinMM()
        return mm, WaveOutHandle(mm, RATE, poll=0.005, **kw)

    def test_write_blocks_while_the_queue_is_full(self):
        mm, h = self.handle()
        self.assertEqual(mm.rate, RATE)
        for _ in range(3):
            self.assertTrue(h.write(CHUNK))          # 0, 1000, 2000 bytes queued: room
        result: list[bool] = []
        t = threading.Thread(target=lambda: result.append(h.write(CHUNK)))
        t.start()
        time.sleep(0.05)
        self.assertEqual(result, [], "a full queue makes write wait, like a full pipe")
        mm.play(1)
        t.join(1)
        self.assertEqual(result, [True])
        self.assertEqual(len(mm.prepared), 4)
        self.assertEqual(mm.unprepared, mm.prepared[:1], "the finished buffer was released")
        self.assertEqual(mm.early_unprepare, 0)

    def test_finish_then_wait_plays_everything_and_closes(self):
        mm, h = self.handle()
        h.write(CHUNK)
        h.write(CHUNK)
        mm.play(2)
        self.assertFalse(h.wait(0.02), "drained, but more audio may still come")
        h.write(CHUNK)
        h.write(CHUNK)
        h.finish_input()
        self.assertFalse(h.write(CHUNK), "no writes after finish_input")
        tail = mm.prepared[-1]
        self.assertEqual(tail.data, b"\x00\x00" * int(RATE * winaudio.TAIL_SECONDS), "a silent tail")
        self.assertFalse(h.wait(0.02))
        mm.play(2)
        self.assertFalse(h.wait(0.02), "the tail is still playing")
        mm.play(1)
        self.assertTrue(h.wait(0.5))
        self.assertEqual(mm.closed, 1)
        self.assertEqual(mm.unprepared, mm.prepared, "every buffer unprepared")
        self.assertEqual(mm.resets, 0, "a line that ends normally isn't cut")
        self.assertTrue(h.wait(0))
        h.kill()                                     # after the end: nothing more to do
        self.assertEqual((mm.closed, mm.resets), (1, 0))

    def test_kill_from_another_thread_wakes_a_blocked_write(self):
        mm = FakeWinMM()
        h = WaveOutHandle(mm, RATE, poll=1.0)        # slow polling: only kill() itself can wake it
        for _ in range(3):
            h.write(CHUNK)
        result: list[bool] = []
        started = threading.Event()

        def writer():
            started.set()
            result.append(h.write(CHUNK))

        t = threading.Thread(target=writer)
        t.start()
        started.wait(1)
        time.sleep(0.03)
        t0 = time.monotonic()
        threading.Thread(target=h.kill).start()
        t.join(1)
        self.assertEqual(result, [False])
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertEqual(mm.resets, 1)
        self.assertEqual(mm.closed, 1)
        self.assertEqual(mm.unprepared, mm.prepared, "every buffer unprepared after the reset")
        self.assertEqual(mm.early_unprepare, 0)
        self.assertFalse(h.write(CHUNK), "write after kill")
        self.assertTrue(h.wait(0))
        h.kill()                                     # twice is fine
        self.assertEqual(mm.closed, 1)

    def test_finish_without_audio_ends_at_once(self):
        mm, h = self.handle()
        h.finish_input()
        self.assertEqual(mm.prepared, [], "no tail for a line that never played")
        self.assertTrue(h.wait(0.1))
        self.assertEqual(mm.closed, 1)

    def test_empty_write_is_a_no_op(self):
        mm, h = self.handle()
        self.assertTrue(h.write(b""))
        self.assertEqual(mm.prepared, [])

    def test_no_device_behaves_like_no_player(self):
        mm, h = self.handle(FakeWinMM(fail_open=True))
        self.assertFalse(h.write(CHUNK))
        h.finish_input()
        self.assertTrue(h.wait(0))
        h.kill()
        self.assertEqual((mm.prepared, mm.closed), ([], 0))

    def test_a_refused_buffer_ends_the_line(self):
        mm, h = self.handle(FakeWinMM(fail_prepare_after=1))
        self.assertTrue(h.write(CHUNK))
        self.assertFalse(h.write(CHUNK))
        self.assertEqual(mm.resets, 1)
        self.assertEqual(mm.closed, 1)
        self.assertEqual(mm.unprepared, mm.prepared)
        self.assertTrue(h.wait(0))

    def test_wait_without_timeout_returns_when_done(self):
        mm, h = self.handle()
        h.write(CHUNK)
        h.finish_input()
        threading.Timer(0.05, mm.play, [5]).start()
        self.assertTrue(h.wait())


class _Fn:
    """A fake DLL function: records calls, returns a scripted code."""

    def __init__(self, name, log, result=0, effect=None):
        self.name, self.log, self.result, self.effect = name, log, result, effect

    def __call__(self, *args):
        self.log.append((self.name, args))
        if self.effect:
            self.effect(*args)
        return self.result


class _FakeDLL:
    def __init__(self, **results):
        self.log: list = []
        for name in ("waveOutOpen", "waveOutPrepareHeader", "waveOutUnprepareHeader", "waveOutWrite",
                     "waveOutReset", "waveOutClose", "waveOutGetErrorTextW"):
            setattr(self, name, _Fn(name, self.log, results.get(name, 0)))

        def error_text(code, buf, size):
            buf.value = f"error {code}"
        self.waveOutGetErrorTextW.effect = error_text


class WinMMTests(unittest.TestCase):
    """The ctypes layer, against a fake DLL: structure layout and calls."""

    def test_structures_match_the_windows_headers(self):
        self.assertEqual(ctypes.sizeof(winaudio.WAVEFORMATEX), 18)
        self.assertEqual(ctypes.sizeof(WAVEHDR), 8 * ctypes.sizeof(ctypes.c_void_p)
                         if ctypes.sizeof(ctypes.c_void_p) == 4 else 48)

    def test_open_asks_for_16_bit_mono_on_the_default_device(self):
        dll = _FakeDLL()
        WinMM(dll).open(24000)
        name, (phwo, device, pfmt, cb, inst, flags) = dll.log[0]
        self.assertEqual(name, "waveOutOpen")
        self.assertEqual(device, winaudio.WAVE_MAPPER)
        fmt = pfmt._obj
        self.assertEqual((fmt.wFormatTag, fmt.nChannels, fmt.nSamplesPerSec, fmt.nAvgBytesPerSec,
                          fmt.nBlockAlign, fmt.wBitsPerSample), (1, 1, 24000, 48000, 2, 16))
        self.assertEqual(flags, winaudio.CALLBACK_NULL)

    def test_prepare_points_the_header_at_a_kept_copy(self):
        mm = WinMM(_FakeDLL())
        h = mm.prepare("dev", b"\x01\x02\x03\x04")
        self.assertEqual(h.hdr.dwBufferLength, 4)
        self.assertEqual(h.hdr.lpData, ctypes.addressof(h.buf))
        self.assertEqual(ctypes.string_at(h.hdr.lpData, 4), b"\x01\x02\x03\x04")
        self.assertFalse(mm.done(h))
        h.hdr.dwFlags |= WHDR_DONE
        self.assertTrue(mm.done(h))

    def test_still_playing_is_not_an_error(self):
        mm = WinMM(_FakeDLL(waveOutUnprepareHeader=winaudio.WAVERR_STILLPLAYING))
        self.assertFalse(mm.unprepare("dev", mm.prepare("dev", b"\x00\x00")))

    def test_failures_raise_with_the_system_text(self):
        mm = WinMM(_FakeDLL(waveOutOpen=2))
        with self.assertRaises(WinMMError) as cm:
            mm.open(24000)
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("error 2", str(cm.exception))
        self.assertIsInstance(cm.exception, OSError)


class PlayerTests(KotodamaTestCase):
    def test_windows_uses_waveout(self):
        mm = FakeWinMM()
        player = Player(platform="win32", winmm=lambda: mm)
        self.assertEqual(player.kind, "waveout")
        self.assertTrue(player.available)
        handle = player.start_stream(100, 24000)
        self.assertIsInstance(handle, WaveOutHandle)
        self.assertEqual(mm.rate, 24000)

    def test_a_configured_command_still_wins_on_windows(self):
        player = Player({"stream": ["ffplay", "-i", "pipe:0"]}, platform="win32")
        self.assertEqual(player.kind, "custom")

    def test_without_winmm_lines_are_silent_and_it_tries_once(self):
        calls = []
        player = Player(platform="win32", winmm=lambda: calls.append(1))
        for _ in range(3):
            handle = player.start_stream(100, 24000)
            self.assertIsInstance(handle, PlayerHandle)
            self.assertFalse(handle.write(b"\x00\x00"))
        self.assertEqual(len(calls), 1)

    def test_load_winmm_is_none_off_windows(self):
        if winaudio.sys.platform != "win32":
            self.assertIsNone(winaudio.load_winmm())

    def test_engine_plays_a_line_through_waveout(self):
        mm = FakeWinMM(autoplay=True)
        tts = FakeTTS(chunks=3, chunk_bytes=480)
        eng = self.make_engine(tts, player=Player(platform="win32", winmm=lambda: mm), null_player=False)
        job = eng.speak(protocol.parse_speak(speak_msg("Hello there.")))
        self.assertTrue(eng.wait_idle())
        self.assertEqual(job.source, "tts", job.error)
        self.assertTrue(eventually(lambda: mm.closed == 1))
        tail = b"\x00\x00" * int(24000 * winaudio.TAIL_SECONDS)
        played = mm.played_bytes()
        self.assertEqual(len(played), 3 * 480 + len(tail))
        self.assertTrue(played.endswith(tail))
        again = eng.speak(protocol.parse_speak(speak_msg("Hello there.", seq=2)))   # from the cache
        self.assertTrue(eng.wait_idle())
        self.assertEqual(again.source, "cache")
        self.assertTrue(eventually(lambda: mm.closed == 2))


if __name__ == "__main__":
    unittest.main()
