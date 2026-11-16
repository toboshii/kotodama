"""Real Windows audio APIs: run only on Windows (py -3 -m unittest discover
-s tests). They play a moment of silence and touch only this test's own
audio session, never another app's volume."""

from __future__ import annotations

import os
import sys
import time
import unittest

from kotodama import winaudio

SILENCE_300MS = b"\x00\x00" * 7200     # 300 ms at 24 kHz


@unittest.skipUnless(sys.platform == "win32", "needs Windows")
class RealWaveOutTests(unittest.TestCase):
    def test_plays_silence_and_kill_is_immediate(self):
        mm = winaudio.load_winmm()
        self.assertIsNotNone(mm, "winmm.dll loads")
        h = winaudio.WaveOutHandle(mm, 24000)
        self.assertTrue(h.write(SILENCE_300MS), "the default output device opened and took a buffer")
        t0 = time.monotonic()
        h.kill()
        self.assertLess(time.monotonic() - t0, 0.1)
        self.assertTrue(h.wait(0))
        self.assertFalse(h.write(SILENCE_300MS))

    def test_a_line_plays_to_the_end(self):
        mm = winaudio.load_winmm()
        h = winaudio.WaveOutHandle(mm, 24000)
        self.assertTrue(h.write(SILENCE_300MS))
        h.finish_input()
        t0 = time.monotonic()
        self.assertTrue(h.wait(3.0))
        self.assertGreater(time.monotonic() - t0, 0.15, "it waited for the audio to play")


@unittest.skipUnless(sys.platform == "win32", "needs Windows")
class RealCoreAudioTests(unittest.TestCase):
    def setUp(self):
        # Our own session exists while we have a waveOut device open.
        self.mm = winaudio.load_winmm()
        self.handle = winaudio.WaveOutHandle(self.mm, 24000)
        self.handle.write(SILENCE_300MS * 3)
        self.addCleanup(self.handle.kill)
        self.com = winaudio.CoreAudioCOM()

    def own_session(self):
        found = self.com.sessions()
        mine = None
        for info, volume in found:
            if info.pid == os.getpid() and mine is None:
                mine = (info, volume)
            else:
                self.com.release(volume)
        return mine

    def test_a_scan_finds_our_own_session(self):
        mine = self.own_session()
        self.assertIsNotNone(mine, "this process's audio session is listed")
        info, volume = mine
        self.addCleanup(self.com.release, volume)
        self.assertTrue(info.instance)
        self.assertTrue(info.identifier)
        self.assertEqual(info.exe.lower().rsplit(".", 1)[0][:6], "python")
        self.assertTrue(0.0 <= info.volume <= 1.0)

    def test_our_session_volume_can_be_set_and_restored(self):
        mine = self.own_session()
        self.assertIsNotNone(mine)
        info, volume = mine
        self.addCleanup(self.com.release, volume)
        original = self.com.get_volume(volume)
        self.addCleanup(self.com.set_volume, volume, original)
        self.assertTrue(self.com.set_volume(volume, 0.5))
        self.assertAlmostEqual(self.com.get_volume(volume), 0.5, places=3)
        self.assertTrue(self.com.set_volume(volume, original))
        self.assertAlmostEqual(self.com.get_volume(volume), original, places=3)

    def test_the_backend_leaves_our_own_session_out(self):
        backend = winaudio.CoreAudioBackend()
        self.addCleanup(backend.close)
        streams = backend.list_streams()
        self.assertFalse(any(s.props.get("application.process.id") == str(os.getpid()) for s in streams))


if __name__ == "__main__":
    unittest.main()
