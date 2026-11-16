"""Gain stage, sink-input parsing, game-stream ducking and the engine wiring."""

from __future__ import annotations

import array
import json
import math
import time
import unittest

from kotodama import protocol
from kotodama.audio import (Ducker, Limiter, db_to_gain, is_game_stream, parse_sink_inputs)
from kotodama.player import Player

from helpers import DISCARD_PLAYER, FakeTTS, KotodamaTestCase, capture_player_cmd, speak_msg

# Shape of real `LC_ALL=C pactl list sink-inputs` output (PipeWire), with the
# game stream plus a browser and our own player.
SINK_INPUTS = """\
Sink Input #1641
\tDriver: PipeWire
\tClient: 1635
\tSink: 65
\tCorked: no
\tMute: no
\tVolume: front-left: 41943 /  64% / -11.63 dB,   front-right: 41943 /  64% / -11.63 dB
\t        balance 0.00
\tProperties:
\t\tpipewire.access.portal.app_id = "net.windower.Lumoria"
\t\tapplication.name = "PlayOnline Viewer"
\t\tapplication.process.binary = "wine-preloader"
\t\tmedia.name = "audio stream #2"
Sink Input #1700
\tCorked: yes
\tMute: yes
\tVolume: mono: 65536 / 100% / 0.00 dB
\t        balance 0.00
\tProperties:
\t\tapplication.name = "Firefox"
\t\tapplication.process.binary = "firefox"
Sink Input #1750
\tCorked: no
\tMute: no
\tVolume: front-left: 65536 / 100% / 0.00 dB,   front-right: 65536 / 100% / 0.00 dB
\tProperties:
\t\tapplication.name = "pw-play"
\t\tapplication.process.binary = "pw-play"
\t\tpipewire.access.portal.app_id = "net.windower.Lumoria"
"""

MATCH = ["windower", "playonline", "ffxi", "pol.exe"]


def samples(pcm: bytes) -> list[int]:
    a = array.array("h")
    a.frombytes(pcm)
    return list(a)


def pcm_of(values: list[int]) -> bytes:
    return array.array("h", values).tobytes()


def apply(pcm: bytes, gain: float) -> bytes:
    """One line's worth of audio through a fresh limiter."""
    return Limiter().process(pcm, gain)


def sine(freq: float, amp: float, seconds: float, rate: int = 24000) -> list[int]:
    return [int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate)) for i in range(int(rate * seconds))]


def correlation(a: list[int], b: list[int]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    den = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
    return num / den


class GainTests(unittest.TestCase):
    def test_db_to_gain(self):
        self.assertAlmostEqual(db_to_gain(6.0), 1.995, places=2)
        self.assertAlmostEqual(db_to_gain(0.0), 1.0)
        self.assertAlmostEqual(db_to_gain(-20.0), 0.1)

    def test_quiet_audio_scales_exactly_linearly(self):
        out = samples(apply(pcm_of([1000, -1000, 0, 2000, 16]), 2.0))
        self.assertEqual(out, [2000, -2000, 0, 4000, 32])

    def test_attenuation_works_too(self):
        self.assertEqual(samples(apply(pcm_of([1000, -1000]), 0.5)), [500, -500])
        self.assertEqual(samples(apply(pcm_of([1000, -1000]), 0.0)), [0, 0])

    def test_never_clips_or_wraps_at_extreme_boost(self):
        wave_in = [32767, -32768, 20000, -20000, 5, -5] * 50
        for gain in (2.0, 4.0, 12.0, 100.0):
            out = samples(apply(pcm_of(wave_in), gain))
            self.assertTrue(all(-32767 <= v <= 32767 for v in out), gain)
            self.assertGreater(out[0], 0, "no sign flip from overflow")
            self.assertLess(out[1], 0)

    def test_heavy_boost_keeps_the_waveform_shape(self):
        """+12 dB on a loud sine: a hard clip would flatten the peaks (correlation ~0.97);
        the limiter lowers its gain instead, so the shape survives."""
        source = sine(440, 0.6, 0.5)
        out = samples(apply(pcm_of(source), 4.0))
        tail_in, tail_out = source[len(source) // 2:], out[len(out) // 2:]
        self.assertGreater(correlation(tail_in, tail_out), 0.995)
        peak = max(abs(v) for v in tail_out)
        self.assertLessEqual(peak, 32767)
        self.assertGreater(peak, 0.85 * 32767, "and it is actually loud, not just safe")

    def test_boost_raises_perceived_loudness(self):
        source = sine(300, 0.08, 0.5)     # a quiet voice
        rms = lambda v: math.sqrt(sum(x * x for x in v) / len(v))
        out = samples(apply(pcm_of(source), db_to_gain(9.0)))
        self.assertAlmostEqual(rms(out) / rms(source), db_to_gain(9.0), delta=0.02)

    def test_recovers_after_a_loud_peak(self):
        quiet = sine(300, 0.05, 0.2)
        loud = sine(300, 0.9, 0.1)
        lim = Limiter()
        lim.process(pcm_of(loud), 3.0)
        out = samples(lim.process(pcm_of(quiet * 4), 3.0))     # 0.8 s of quiet audio afterwards
        tail = out[-4000:]
        expected = [round(v * 3.0) for v in (quiet * 4)[-4000:]]
        self.assertLess(max(abs(a - b) for a, b in zip(tail, expected)), 0.03 * 32767,
                        "gain reduction fully recovers within a fraction of a second")

    def test_state_carries_across_chunks(self):
        source = pcm_of(sine(440, 0.6, 0.4))
        whole = Limiter().process(source, 4.0)
        lim = Limiter()
        parts = b"".join(lim.process(source[i:i + 4800], 4.0) for i in range(0, len(source), 4800))
        self.assertEqual(parts, whole, "chunking must not change the result")

    def test_silence_stays_silent_odd_byte_dropped_empty_ok(self):
        self.assertEqual(samples(apply(pcm_of([0] * 8), 9.0)), [0] * 8)
        self.assertEqual(len(apply(pcm_of([1000, 1000]) + b"\x01", 2.0)), 4)
        self.assertEqual(apply(b"", 2.0), b"")


class ParseTests(unittest.TestCase):
    def test_parse_real_shape(self):
        streams = {s.id: s for s in parse_sink_inputs(SINK_INPUTS)}
        self.assertEqual(sorted(streams), [1641, 1700, 1750])
        game = streams[1641]
        self.assertEqual(game.volumes, [41943, 41943])
        self.assertEqual(game.label, "PlayOnline Viewer")
        self.assertEqual(game.props["pipewire.access.portal.app_id"], "net.windower.Lumoria")
        self.assertFalse(game.muted or game.corked)
        ff = streams[1700]
        self.assertEqual(ff.volumes, [65536])
        self.assertTrue(ff.muted and ff.corked)

    def test_matching(self):
        s = {x.id: x for x in parse_sink_inputs(SINK_INPUTS)}
        self.assertTrue(is_game_stream(s[1641], MATCH))
        self.assertFalse(is_game_stream(s[1700], MATCH))
        self.assertFalse(is_game_stream(s[1750], MATCH), "our own player is never ducked, even with the game's app id")
        self.assertTrue(is_game_stream(s[1700], ["FIREFOX"]), "case-insensitive")
        self.assertFalse(is_game_stream(s[1641], []))

    def test_empty_and_garbage(self):
        self.assertEqual(parse_sink_inputs(""), [])
        self.assertEqual(parse_sink_inputs("no streams here"), [])


class FakePactl:
    """Stands in for pactl: serves `list sink-inputs`, records volume sets."""

    def __init__(self, text=SINK_INPUTS):
        self.text = text
        self.calls: list[tuple] = []
        self.live = {s.id: list(s.volumes) for s in parse_sink_inputs(text)}

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("list", "sink-inputs"):
            return self.text
        if args[0] == "set-sink-input-volume":
            sid = int(args[1])
            if sid not in self.live:
                raise RuntimeError("No such entity")
            self.live[sid] = [int(v) for v in args[2:]]
            return ""
        raise AssertionError(args)

    def sets(self, sid=1641):
        return [[int(v) for v in c[2:]] for c in self.calls if c[0] == "set-sink-input-volume" and int(c[1]) == sid]


def make_ducker(runner, **kw):
    params = dict(enabled=True, level=40, match=MATCH, attack_ms=30, release_ms=30, hold_ms=40,
                  state_path=None, runner=runner)
    params.update(kw)
    return Ducker(**params)


class DuckerTests(KotodamaTestCase):
    def test_ducks_only_the_game_stream_then_restores_exactly(self):
        pactl = FakePactl()
        d = make_ducker(pactl, state_path=self.tmp / "duck.json")
        self.addCleanup(d.shutdown)
        d.acquire()
        deadline = time.time() + 3
        while pactl.live[1641] == [41943, 41943] and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(pactl.live[1641], [int(41943 * 0.4)] * 2)
        self.assertEqual(pactl.live[1700], [65536], "other apps are untouched")
        self.assertEqual(pactl.live[1750], [65536, 65536], "our own stream is untouched")
        saved = json.loads((self.tmp / "duck.json").read_text())
        self.assertEqual(saved["streams"], {"1641": [41943, 41943]})

        d.release()
        self.assertTrue(d.settled(3))
        self.assertEqual(pactl.live[1641], [41943, 41943], "restored to the exact original raw values")
        self.assertFalse((self.tmp / "duck.json").exists(), "state file removed once restored")

    def test_ramps_smoothly(self):
        pactl = FakePactl()
        d = make_ducker(pactl, attack_ms=180, release_ms=240)
        self.addCleanup(d.shutdown)
        d.acquire()
        time.sleep(0.4)
        d.release()
        self.assertTrue(d.settled(3))
        seq = [v[0] for v in pactl.sets()]
        down, up = seq[:seq.index(min(seq)) + 1], seq[seq.index(min(seq)):]
        self.assertGreater(len(down), 2, "attack takes several steps")
        self.assertEqual(down, sorted(down, reverse=True))
        self.assertGreater(len(up), 3, "release takes several steps")
        self.assertEqual(up, sorted(up))
        self.assertEqual(seq[-1], 41943)

    def test_hold_keeps_music_down_between_back_to_back_lines(self):
        pactl = FakePactl()
        d = make_ducker(pactl, hold_ms=300)
        self.addCleanup(d.shutdown)
        d.acquire()
        time.sleep(0.15)
        d.release()
        time.sleep(0.1)       # next line starts inside the hold window
        d.acquire()
        time.sleep(0.15)
        self.assertNotIn([41943, 41943], pactl.sets(), "never restored between the two lines")
        d.release()
        self.assertTrue(d.settled(3))
        self.assertEqual(pactl.live[1641], [41943, 41943])

    def test_refcount_with_overlapping_lines(self):
        pactl = FakePactl()
        d = make_ducker(pactl)
        self.addCleanup(d.shutdown)
        d.acquire()
        d.acquire()
        d.release()
        time.sleep(0.25)
        self.assertEqual(pactl.live[1641], [int(41943 * 0.4)] * 2, "still one line playing")
        d.release()
        self.assertTrue(d.settled(3))

    def test_disabled_and_level_100_do_nothing(self):
        for kw in ({"enabled": False}, {"level": 100}):
            pactl = FakePactl()
            d = make_ducker(pactl, **kw)
            d.acquire()
            time.sleep(0.15)
            d.release()
            self.assertTrue(d.settled(2))
            d.shutdown()
            self.assertEqual(pactl.sets(), [], kw)

    def test_can_be_reconfigured_live(self):
        pactl = FakePactl()
        d = make_ducker(pactl)
        self.addCleanup(d.shutdown)
        d.configure(level=10)
        d.acquire()
        time.sleep(0.25)
        self.assertEqual(pactl.live[1641], [int(41943 * 0.1)] * 2)
        d.configure(enabled=False)
        time.sleep(0.25)
        self.assertEqual(pactl.live[1641], [41943, 41943], "turning it off mid-line restores the volume")
        d.release()

    def test_no_matching_stream_warns_once_and_does_nothing(self):
        warnings = []
        pactl = FakePactl()
        d = make_ducker(pactl, match=["nothing-matches-this"], notify=warnings.append)
        self.addCleanup(d.shutdown)
        for _ in range(3):
            d.acquire()
            time.sleep(0.1)
            d.release()
            self.assertTrue(d.settled(2))
        self.assertEqual(pactl.sets(), [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("audio list", warnings[0])

    def test_missing_pactl_disables_ducking_without_crashing(self):
        warnings = []

        def runner(*args):
            raise FileNotFoundError("pactl")

        d = make_ducker(runner, notify=warnings.append)
        self.addCleanup(d.shutdown)
        d.acquire()
        time.sleep(0.15)
        d.release()
        self.assertEqual(len(warnings), 1)
        self.assertIn("pactl", warnings[0])

    def test_stream_that_vanishes_mid_duck_is_tolerated(self):
        pactl = FakePactl()
        d = make_ducker(pactl)
        self.addCleanup(d.shutdown)
        d.acquire()
        time.sleep(0.1)
        del pactl.live[1641]
        d.release()
        self.assertTrue(d.settled(3))

    def test_crash_recovery_restores_volume_on_next_start(self):
        state = self.tmp / "duck.json"
        # Simulate a sidecar that died while ducked: the game is quiet, state file remains.
        state.write_text(json.dumps({"pid": 1, "streams": {"1641": [41943, 41943], "999": [1, 1]}}))
        pactl = FakePactl()
        pactl.live[1641] = [16777, 16777]
        d = make_ducker(pactl, state_path=state)
        self.assertEqual(d.restore_stale(), 1, "the gone stream (999) is ignored")
        self.assertEqual(pactl.live[1641], [41943, 41943])
        self.assertFalse(state.exists())
        self.assertEqual(d.restore_stale(), 0)

    def test_shutdown_while_ducked_restores_immediately(self):
        pactl = FakePactl()
        d = make_ducker(pactl, hold_ms=10_000)
        d.acquire()
        time.sleep(0.2)
        self.assertNotEqual(pactl.live[1641], [41943, 41943])
        d.shutdown()
        self.assertEqual(pactl.live[1641], [41943, 41943])


class FakeDucker:
    enabled = True
    level = 0.4

    def __init__(self):
        self.acquired = 0
        self.released = 0
        self.configured = []
        self.notify = None

    def acquire(self):
        self.acquired += 1

    def release(self):
        self.released += 1

    def configure(self, **kw):
        self.configured.append(kw)

    def shutdown(self):
        pass


class EngineAudioTests(KotodamaTestCase):
    def capture_player(self):
        out = self.tmp / "player-out.pcm"
        return Player({"stream": capture_player_cmd(out)}), out

    def test_live_audio_gets_makeup_gain_but_cache_stays_unscaled(self):
        tts = FakeTTS(chunks=3, chunk_bytes=480)
        player, out = self.capture_player()
        eng = self.make_engine(tts, player=player, null_player=False)
        self.assertAlmostEqual(eng.gain(), db_to_gain(9.0), places=4)
        eng.speak(protocol.parse_speak(speak_msg("Hello there.")))
        self.assertTrue(eng.wait_idle())
        raw = b"\x10\x00" * 240 * 3                       # what FakeTTS streamed (sample value 16)
        self.assertEqual(out.read_bytes(), Limiter().process(raw, eng.gain()))
        self.assertEqual(set(samples(out.read_bytes())), {int(16 * eng.gain() + 0.5)})
        cached = eng.cache.get(eng.last["key"])
        import wave
        with wave.open(str(cached)) as w:
            self.assertEqual(w.readframes(w.getnframes()), raw, "cache holds the unscaled audio")

    def test_cached_playback_uses_the_same_gain_and_follows_volume(self):
        tts = FakeTTS(chunks=2, chunk_bytes=480)
        player, out = self.capture_player()
        eng = self.make_engine(tts, player=player, null_player=False)
        eng.speak(protocol.parse_speak(speak_msg("Same line.")))
        self.assertTrue(eng.wait_idle())

        eng.handle({"t": "config", "volume": 300})
        self.assertEqual(eng.volume, 300)
        job = eng.speak(protocol.parse_speak(speak_msg("Same line.", seq=2)))
        self.assertTrue(eng.wait_idle())
        self.assertEqual(job.source, "cache")
        self.assertEqual(len(tts.calls), 1)
        values = set(samples(out.read_bytes()))
        self.assertEqual(values, {min(32767, int(16 * 3 * eng.makeup + 0.5))},
                         "300% volume applied to the cached audio")

    def test_volume_is_clamped(self):
        eng = self.make_engine()
        eng.handle({"t": "config", "volume": 9000})
        self.assertEqual(eng.volume, 400)
        eng.handle({"t": "config", "volume": -5})
        self.assertEqual(eng.volume, 0)
        self.assertEqual(eng.gain(), 0.0)

    def test_player_always_runs_at_full_volume(self):
        seen = []

        class SpyPlayer(Player):
            def start_stream(self, volume, rate):
                seen.append(volume)
                return super().start_stream(volume, rate)

        eng = self.make_engine(FakeTTS(chunks=1), player=SpyPlayer(null=True), null_player=True)
        eng.handle({"t": "config", "volume": 250})
        eng.speak(protocol.parse_speak(speak_msg("One.")))
        eng.wait_idle()
        eng.speak(protocol.parse_speak(speak_msg("One.", seq=2)))   # cached
        eng.wait_idle()
        self.assertEqual(seen, [100, 100], "gain is applied by the sidecar, never by the player")

    def test_ducks_for_each_line_and_always_balances(self):
        ducker = FakeDucker()
        player, _ = self.capture_player()
        eng = self.make_engine(FakeTTS(chunks=3, chunk_bytes=480), player=player, null_player=False, ducker=ducker)
        eng.speak(protocol.parse_speak(speak_msg("First.")))
        eng.wait_idle()
        eng.speak(protocol.parse_speak(speak_msg("First.", seq=2)))      # from cache
        eng.wait_idle()
        self.assertEqual((ducker.acquired, ducker.released), (2, 2))

    def test_interrupting_a_line_releases_its_duck(self):
        ducker = FakeDucker()
        player = Player({"stream": DISCARD_PLAYER})
        eng = self.make_engine(FakeTTS(chunks=20, delay=0.03), player=player, null_player=False, ducker=ducker)
        j1 = eng.speak(protocol.parse_speak(speak_msg("Long line.")))
        deadline = time.time() + 3
        while ducker.acquired == 0 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(ducker.acquired, 1)
        eng.handle({"t": "stop"})
        self.assertEqual(ducker.released, 1, "stop releases the duck immediately")
        self.assertTrue(eng.wait_idle(10))
        self.assertEqual((ducker.acquired, ducker.released), (1, 1), "no double release when the job ends")

    def test_failed_synthesis_never_ducks(self):
        ducker = FakeDucker()
        eng = self.make_engine(FakeTTS(fail="TTS HTTP 500: down"), ducker=ducker)
        eng.speak(protocol.parse_speak(speak_msg("Nope.")))
        eng.wait_idle()
        self.assertEqual((ducker.acquired, ducker.released), (0, 0))

    def test_config_message_reaches_the_ducker(self):
        ducker = FakeDucker()
        eng = self.make_engine(ducker=ducker)
        eng.handle({"t": "config", "duck": False, "duck_level": 25})
        self.assertEqual(ducker.configured[-1], {"enabled": False, "level": 25})
        eng.handle({"t": "config", "volume": 80})
        self.assertEqual(ducker.configured[-1], {"enabled": None, "level": None})


if __name__ == "__main__":
    unittest.main()
