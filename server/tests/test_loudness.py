"""Loudness meter, per-voice normalization, first-line settling, leveler."""

from __future__ import annotations

import array
import contextlib
import io
import math
import sqlite3
import time
import unittest
import wave

from kotodama import protocol
from kotodama.cache import AudioCache
from kotodama.cli import _analyze_cache
from kotodama.loudness import (SILENT, LevelerSettings, LineGain, LoudnessMeter, NormalizeSettings,
                               measure_wav, typical_level, voice_gain_db)
from kotodama.player import Player

from helpers import FakeTTS, KotodamaTestCase, speak_msg

RATE = 24000


def tone(amp: float, seconds: float, freq: float = 997.0, rate: int = RATE) -> bytes:
    return array.array("h", (int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate))
                             for i in range(int(rate * seconds)))).tobytes()


def silence(seconds: float, rate: int = RATE) -> bytes:
    return b"\x00\x00" * int(rate * seconds)


def chunks(pcm: bytes, size: int = 4096):
    for i in range(0, len(pcm), size):
        yield pcm[i:i + size]


def level_of(pcm: bytes, rate: int = RATE) -> float | None:
    m = LoudnessMeter(rate)
    m.feed(pcm)
    return m.integrated()


def lufs_to_amp(lufs: float) -> float:
    """Peak amplitude of a 997 Hz sine measuring `lufs` (0 dBFS peak = -3.01 LUFS)."""
    return 10 ** ((lufs + 3.01) / 20)


class MeterTests(unittest.TestCase):
    def test_reference_tone_at_any_rate(self):
        """BS.1770: a 997 Hz sine peaking at -20 dBFS reads -23.0 LUFS."""
        for rate in (16000, 24000, 44100, 48000):
            self.assertAlmostEqual(level_of(tone(0.1, 3, rate=rate), rate), -23.0, delta=0.1, msg=rate)

    def test_level_tracks_amplitude(self):
        self.assertAlmostEqual(level_of(tone(0.2, 3)) - level_of(tone(0.1, 3)), 6.02, delta=0.05)

    def test_chunking_does_not_change_the_result(self):
        pcm = tone(0.1, 2) + tone(0.03, 2, freq=300)
        whole = level_of(pcm)
        m = LoudnessMeter(RATE)
        for c in chunks(pcm, 1234):
            m.feed(c)
        self.assertAlmostEqual(m.integrated(), whole, places=9)
        self.assertEqual(m.frames, len(pcm) // 2)

    def test_pauses_are_gated_out(self):
        speech = tone(0.1, 2)
        paused = speech + silence(4) + speech
        # (the few 400 ms blocks straddling an edge are partly loud, as in BS.1770)
        self.assertAlmostEqual(level_of(paused), level_of(speech + speech), delta=0.5)
        self.assertLess(level_of(speech + silence(4)) - level_of(speech + silence(40)), 0.01,
                        "however long the pause")

    def test_quiet_passages_are_gated_out(self):
        """Mumbled asides 15 LU under the line don't drag its level down."""
        loud = tone(0.1, 3)
        # (ungated it would read about 2.7 dB lower; edge blocks cost a little)
        self.assertAlmostEqual(level_of(loud + tone(0.1 / 5.6, 3)), level_of(loud), delta=0.4)

    def test_silence_and_dc_read_as_nothing(self):
        self.assertIsNone(level_of(silence(2)))
        self.assertIsNone(level_of(b"\x10\x00" * RATE * 2), "DC is filtered out, as by the ear")
        self.assertIsNone(LoudnessMeter(RATE).integrated())

    def test_recent_follows_a_change(self):
        m = LoudnessMeter(RATE)
        m.feed(tone(0.1, 3))
        m.feed(tone(0.025, 3))
        self.assertAlmostEqual(m.recent(3.0), level_of(tone(0.025, 3)), delta=0.3)

    def test_measure_wav(self):
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "a.wav"
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1), w.setsampwidth(2), w.setframerate(RATE)
                w.writeframes(tone(0.1, 2))
            level, seconds = measure_wav(path)
            self.assertAlmostEqual(level, -23.0, delta=0.1)
            self.assertAlmostEqual(seconds, 2.0)
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1), w.setsampwidth(2), w.setframerate(RATE)
                w.writeframes(silence(1))
            self.assertEqual(measure_wav(path)[0], SILENT)


class PolicyTests(unittest.TestCase):
    def test_typical_level_ignores_outliers(self):
        self.assertEqual(typical_level([-20.0] * 8 + [-40.0, -5.0]), -20.0)
        self.assertEqual(typical_level([SILENT, -21.0]), -21.0, "silent lines say nothing about the voice")
        self.assertIsNone(typical_level([]))
        self.assertIsNone(typical_level([SILENT]))

    def test_voice_gain_is_clamped(self):
        s = NormalizeSettings(target_lufs=-13, max_boost_db=15, max_cut_db=10)
        self.assertAlmostEqual(voice_gain_db(s, -21.0, 9.0), 8.0)
        self.assertEqual(voice_gain_db(s, -40.0, 9.0), 15.0)
        self.assertEqual(voice_gain_db(s, 0.0, 9.0), -10.0)
        self.assertEqual(voice_gain_db(s, None, 9.0), 9.0, "unmeasured: the old fixed makeup")

    def test_steady_gain_never_moves(self):
        lg = LineGain(NormalizeSettings(), RATE, 7.5)
        gains = {lg.process(c) for c in chunks(tone(0.1, 2) + tone(0.01, 2) + tone(0.4, 2))}
        self.assertEqual(gains, {7.5}, "a known voice's lines play at one gain: dynamics untouched")

    def test_first_line_settles_slowly_within_range(self):
        s = NormalizeSettings(target_lufs=-13, adapt_rate_db_s=2.0, adapt_range_db=6.0)
        lg = LineGain(s, RATE, 9.0, adapt=True)
        pcm = tone(lufs_to_amp(-33.0), 6)          # wants +20, clamped to 15, range allows 15
        t, trace = 0.0, []
        for c in chunks(pcm):
            dt = len(c) / 2 / RATE
            before = lg.db
            after = lg.process(c)
            t += dt
            trace.append((t, after))
            self.assertLessEqual(abs(after - before), 2.0 * dt + 1e-9, "never faster than 2 dB/s")
        self.assertTrue(all(g == 9.0 for t, g in trace if t < 0.99), "waits for a second of audio")
        self.assertAlmostEqual(trace[-1][1], 15.0, delta=1e-6)

    def test_first_line_stays_within_its_range(self):
        s = NormalizeSettings(target_lufs=-13, max_boost_db=30, adapt_range_db=6.0, adapt_rate_db_s=100)
        lg = LineGain(s, RATE, 0.0, adapt=True)
        for c in chunks(tone(lufs_to_amp(-40.0), 3)):
            lg.process(c)
        self.assertAlmostEqual(lg.db, 6.0)

    def test_settle_jumps_after_look_ahead(self):
        s = NormalizeSettings(target_lufs=-13)
        lg = LineGain(s, RATE, 9.0, adapt=True)
        lg.meter.feed(tone(lufs_to_amp(-20.0), 1.2))
        lg.settle()
        self.assertAlmostEqual(lg.db, 7.0, delta=0.1)

    def leveled(self, pcm: bytes, **kw) -> list[tuple[float, float]]:
        lg = LineGain(NormalizeSettings(leveler=True), RATE, 0.0, leveler=LevelerSettings(**kw))
        t, out = 0.0, []
        for c in chunks(pcm):
            out.append((t, lg.process(c)))
            t += len(c) / 2 / RATE
        return out

    def test_leveler_lifts_a_sagging_line(self):
        trace = self.leveled(tone(0.1, 2) + tone(0.1 / 4, 8))   # sags 12 dB
        self.assertTrue(all(g == 0.0 for t, g in trace if t < 2.0), "nothing while it holds its level")
        self.assertAlmostEqual(trace[-1][1], 6.0, delta=1e-6, msg="capped at max_db")
        for (t0, g0), (t1, g1) in zip(trace, trace[1:]):
            self.assertLessEqual(abs(g1 - g0), 2.0 * (t1 - t0) + 1e-6)

    def test_leveler_ignores_small_sag_and_never_cuts(self):
        small = self.leveled(tone(0.1, 2) + tone(0.1 * 10 ** (-1.5 / 20), 6))   # 1.5 dB: inside the deadband
        self.assertEqual({g for _, g in small}, {0.0})
        louder = self.leveled(tone(0.05, 2) + tone(0.2, 6))
        self.assertEqual({g for _, g in louder}, {0.0}, "boost only: a shout stays a shout")

    def test_leveler_holds_through_pauses(self):
        trace = self.leveled(tone(0.1, 2) + tone(0.025, 3) + silence(3) + tone(0.025, 1))
        during_pause = [g for t, g in trace if 5.5 < t < 8.0]
        self.assertGreater(during_pause[0], 0.0)
        self.assertEqual(len(set(during_pause)), 1, "frozen while nothing is said")

    def test_leveler_scope(self):
        s = NormalizeSettings(leveler=True, leveler_providers=("gemini",))
        self.assertIsNotNone(s.leveler_for("gemini"))
        self.assertIsNone(s.leveler_for("inworld"))
        self.assertIsNotNone(NormalizeSettings(leveler=True).leveler_for("inworld"), "empty list: everyone")
        self.assertIsNone(NormalizeSettings(leveler=False).leveler_for("gemini"))
        self.assertIsNone(NormalizeSettings(enabled=False, leveler=True).leveler_for("gemini"))


class ToneTTS(FakeTTS):
    """Streams a 997 Hz tone whose level depends on the voice; '(whisper)'
    in the text drops it 12 dB."""

    def __init__(self, levels: dict[str, float], seconds: float = 3.0, **kw):
        super().__init__(**kw)
        self.levels, self.seconds = levels, seconds

    def stream(self, text, request):
        with self.lock:
            self.calls.append({"text": text, "voice": request.voice})
        amp = lufs_to_amp(self.levels.get(request.voice, -20.0))
        if "(whisper)" in text:
            amp /= 4
        yield from chunks(tone(amp, self.seconds))


class EngineLoudnessTests(KotodamaTestCase):
    def engine(self, levels, cfg=None, **kw):
        out = self.tmp / "player-out.pcm"
        eng = self.make_engine(ToneTTS(levels, **kw), cfg=cfg,
                               player=Player({"stream": ["sh", "-c", f"cat > {out}"]}), null_player=False)
        return eng, out

    def say(self, eng, npc, voice, text, seq=[0]):
        seq[0] += 1
        eng.handle({"t": "cmd", "name": "voice_set", "npc": npc, "params": {"voice": voice}})
        job = eng.speak(protocol.parse_speak(speak_msg(text, name=npc, seq=seq[0])))
        self.assertTrue(eng.wait_idle())
        return job

    def heard(self, out) -> float:
        return level_of(out.read_bytes())

    def test_voices_end_up_at_the_target(self):
        eng, out = self.engine({"onyx": -27.0, "nova": -16.0})
        for i in range(3):
            self.say(eng, "Quiet", "onyx", f"Line {i}.")
            self.say(eng, "Loud", "nova", f"Line {i}.")
        self.say(eng, "Quiet", "onyx", "Once more.")
        quiet = self.heard(out)
        self.say(eng, "Loud", "nova", "Once more.")
        loud = self.heard(out)
        self.assertAlmostEqual(quiet, -13.0, delta=0.5)
        self.assertAlmostEqual(loud, -13.0, delta=0.5)

    def test_a_whisper_stays_a_whisper(self):
        eng, out = self.engine({"onyx": -22.0})
        for i in range(4):
            self.say(eng, "Zeid", "onyx", f"Normal {i}.")
        self.say(eng, "Zeid", "onyx", "Normal again.")
        normal = self.heard(out)
        self.say(eng, "Zeid", "onyx", "Psst. (whisper)")
        whisper = self.heard(out)
        self.assertAlmostEqual(normal - whisper, 12.04, delta=0.2, msg="its 12 dB drop survives")

    def test_style_instructions_are_the_same_voice(self):
        from kotodama.engine import Engine
        from kotodama.tts_base import VoiceRequest
        tts = FakeTTS()
        whisper = Engine._voice_ids(tts, VoiceRequest("onyx", {}, "Tone: hushed whisper"))
        shout = Engine._voice_ids(tts, VoiceRequest("onyx", {}, "Tone: furious shouting"))
        self.assertEqual(whisper, shout, "style changes are dynamics to keep, not a new voice to level")
        self.assertNotEqual(whisper, Engine._voice_ids(tts, VoiceRequest("nova", {}, "")))

    def test_lines_record_their_voice_and_level(self):
        eng, _ = self.engine({"onyx": -25.0})
        job = self.say(eng, "Zeid", "onyx", "Hello.")
        row = eng.cache._db.execute("SELECT voice, lufs FROM audio WHERE key=?", (job.key,)).fetchone()
        self.assertEqual(row[0], "openai:fake-tts:onyx")
        self.assertAlmostEqual(row[1], -25.0, delta=0.1)

    def test_first_line_of_a_new_voice_starts_from_its_model(self):
        eng, out = self.engine({"onyx": -25.0, "nova": -25.0})
        for i in range(3):
            self.say(eng, "Zeid", "onyx", f"Line {i}.")
        job = self.say(eng, "Ayame", "nova", "First words.")
        self.assertEqual(job.loudness, "model")
        self.assertAlmostEqual(self.heard(out), -13.0, delta=0.5, msg="the model's level was a good guess")

    def test_first_line_ever_settles_toward_the_target(self):
        eng, out = self.engine({"onyx": -30.0}, seconds=6.0)
        job = self.say(eng, "Zeid", "onyx", "Hello.")
        self.assertEqual(job.loudness, "none")
        pcm = out.read_bytes()
        start, end = level_of(pcm[: RATE * 2]), level_of(pcm[-RATE * 2:])
        self.assertAlmostEqual(start, -30.0 + 9.0, delta=0.3, msg="starts at the fixed makeup")
        self.assertGreater(end, start + 5.0, "then rises toward the target")
        second = self.say(eng, "Zeid", "onyx", "Again.")
        self.assertEqual(second.loudness, "voice")
        self.assertAlmostEqual(self.heard(out), -15.0, delta=0.3, msg="steady from line two (+15 dB cap)")

    def test_look_ahead_measures_before_playing(self):
        cfg = self.make_config(audio={"normalize": {"lookahead_ms": 500}})
        eng, out = self.engine({"onyx": -20.0}, cfg=cfg, seconds=3.0)
        self.say(eng, "Zeid", "onyx", "Hello.")
        pcm = out.read_bytes()
        self.assertAlmostEqual(level_of(pcm[: RATE]), -13.0, delta=1.0, msg="right from the first chunk")
        self.assertEqual(len(pcm), RATE * 3 * 2, "nothing held back is lost")

    def test_short_line_shorter_than_look_ahead_still_plays(self):
        cfg = self.make_config(audio={"normalize": {"lookahead_ms": 5000}})
        eng, out = self.engine({"onyx": -20.0}, cfg=cfg, seconds=1.0)
        self.say(eng, "Zeid", "onyx", "Hi.")
        self.assertEqual(len(out.read_bytes()), RATE * 2)

    def test_cached_lines_use_the_voice_gain(self):
        eng, out = self.engine({"onyx": -24.0})
        for i in range(3):
            self.say(eng, "Zeid", "onyx", f"Line {i}.")
        job = self.say(eng, "Zeid", "onyx", "Line 0.")
        self.assertEqual(job.source, "cache")
        self.assertAlmostEqual(self.heard(out), -13.0, delta=0.3)
        eng.replay()
        self.assertTrue(eng.wait_idle())
        self.assertEqual(eng.last_job.loudness, "voice")
        self.assertAlmostEqual(self.heard(out), -13.0, delta=0.3)

    def test_a_cut_off_line_is_measured_in_full(self):
        """Stopped early, the rest still downloads into the cache, and all of
        it is measured, not just the part that played."""
        class Slow(FakeTTS):
            def stream(self, text, request):
                yield tone(lufs_to_amp(-30.0), 1.0)
                time.sleep(0.3)
                yield from chunks(tone(lufs_to_amp(-20.0), 3.0))

        eng = self.make_engine(Slow())
        job = eng.speak(protocol.parse_speak(speak_msg("Long line.")))
        time.sleep(0.15)
        eng.stop_all()
        self.assertTrue(eng.wait_idle())
        self.assertGreater(eng.cache.loudness(job.key), -22.0)

    def test_old_cache_lines_are_measured_when_played(self):
        eng, out = self.engine({"onyx": -24.0})
        job = self.say(eng, "Zeid", "onyx", "Old line.")
        eng.cache._db.execute("UPDATE audio SET voice=NULL, lufs=NULL")   # as cached before this feature
        again = self.say(eng, "Zeid", "onyx", "Old line.")
        self.assertEqual(again.source, "cache")
        row = eng.cache._db.execute("SELECT voice, lufs FROM audio WHERE key=?", (job.key,)).fetchone()
        self.assertEqual(row[0], "openai:fake-tts:onyx")
        self.assertAlmostEqual(row[1], -24.0, delta=0.1)

    def test_old_lines_stand_in_for_an_npcs_voice(self):
        eng, out = self.engine({"onyx": -24.0, "nova": -24.0})
        for i in range(3):
            self.say(eng, "Zeid", "onyx", f"Line {i}.")
        eng.cache._db.execute("UPDATE audio SET voice=NULL")
        job = self.say(eng, "Zeid", "onyx", "New line.")
        self.assertEqual(job.loudness, "npc")
        self.assertAlmostEqual(self.heard(out), -13.0, delta=0.3)

    def test_turning_it_off_restores_fixed_makeup(self):
        eng, out = self.engine({"onyx": -24.0})
        for i in range(3):
            self.say(eng, "Zeid", "onyx", f"Line {i}.")
        eng.handle({"t": "config", "normalize": False})
        job = self.say(eng, "Zeid", "onyx", "Off now.")
        self.assertEqual(job.loudness, "off")
        self.assertAlmostEqual(self.heard(out), -24.0 + 9.0, delta=0.2)
        eng.handle({"t": "config", "normalize": ""})
        self.assertTrue(eng.norm.enabled, "'' is the config file's choice")

    def test_config_file_can_turn_it_off(self):
        cfg = self.make_config(audio={"normalize": {"enabled": False}})
        eng, _ = self.engine({}, cfg=cfg)
        self.assertFalse(eng.norm.enabled)
        eng.handle({"t": "config", "normalize": True})
        self.assertTrue(eng.norm.enabled)
        eng.handle({"t": "config", "normalize": ""})
        self.assertFalse(eng.norm.enabled)

    def test_volume_still_scales_on_top(self):
        eng, out = self.engine({"onyx": -24.0})
        for i in range(3):
            self.say(eng, "Zeid", "onyx", f"Line {i}.")
        eng.handle({"t": "config", "volume": 50})
        self.say(eng, "Zeid", "onyx", "Line 0.")
        self.assertAlmostEqual(self.heard(out), -13.0 - 6.02, delta=0.3)

    def test_leveler_from_config(self):
        cfg = self.make_config(audio={"normalize": {"leveler": {"enabled": True, "max_db": 4, "providers": ["OpenAI"]}}})
        eng, _ = self.engine({}, cfg=cfg)
        self.assertEqual(eng.norm.leveler_providers, ("openai",))
        self.assertEqual(eng.norm.leveler_for("openai").max_db, 4.0)

    def test_status_and_voice_show_report_loudness(self):
        eng, _ = self.engine({"onyx": -24.0})
        for i in range(2):
            self.say(eng, "Zeid", "onyx", f"Line {i}.")
        self.replies.clear()
        eng.handle({"t": "cmd", "name": "voice_show", "npc": "Zeid"})
        eng.handle({"t": "cmd", "name": "status"})
        text = "\n".join(self.say_lines())
        self.assertIn("loudness: -24.0 LUFS over 2 line(s) -> +11.0 dB", text)
        self.assertIn("voices normalized to -13 LUFS", text)


class CacheMigrationTests(KotodamaTestCase):
    def test_old_index_gains_the_columns(self):
        d = self.tmp / "cache"
        d.mkdir()
        db = sqlite3.connect(str(d / "index.sqlite"))
        db.execute("CREATE TABLE audio (key TEXT PRIMARY KEY, path TEXT NOT NULL, bytes INTEGER NOT NULL,"
                   " npc TEXT, zone INTEGER, text TEXT, seconds REAL, created REAL NOT NULL,"
                   " last_played REAL NOT NULL, plays INTEGER NOT NULL DEFAULT 0)")
        db.execute("INSERT INTO audio VALUES ('k', 'p', 1, 'Zeid', 1, 't', 2.0, 1, 1, 1)")
        db.commit()
        db.close()
        cache = AudioCache(d)
        self.addCleanup(cache.close)
        self.assertEqual(cache.to_measure(), [("k", "p")])
        cache.annotate("k", lufs=-20.0, voice="a:b:c")
        self.assertEqual(cache.voice_levels("a:b:c", None, 10), ([-20.0], "voice"))
        cache.annotate("k", lufs=-30.0, voice="x:y:z")
        self.assertEqual(cache.voice_levels("a:b:c", None, 10), ([-20.0], "voice"), "fills gaps only")
        AudioCache(d).close()   # opening again is harmless

    def test_voice_levels_short_lines_and_limit(self):
        cache = AudioCache(self.tmp / "c")
        self.addCleanup(cache.close)
        for i, (secs, lufs) in enumerate([(2.0, -20.0), (2.0, -21.0), (2.0, -22.0), (2.0, -23.0), (0.5, -40.0)]):
            cache._db.execute("INSERT INTO audio (key, path, bytes, npc, seconds, created, last_played, voice, lufs)"
                              " VALUES (?, 'p', 1, 'Zeid', ?, ?, 0, 'v', ?)", (str(i), secs, i, lufs))
        self.assertEqual(cache.voice_levels("v", None, 3)[0], [-23.0, -22.0, -21.0], "newest first, no short lines")
        self.assertEqual(cache.model_levels("v")[:1], [-23.0], "short lines left out here too")
        self.assertEqual(cache.model_levels("w"), [])


class AnalyzeCommandTests(KotodamaTestCase):
    def test_analyze_measures_unmeasured_lines(self):
        eng = self.make_engine(ToneTTS({"onyx": -21.0}))
        eng.handle({"t": "cmd", "name": "voice_set", "npc": "Zeid", "params": {"voice": "onyx"}})
        eng.speak(protocol.parse_speak(speak_msg("A line.", name="Zeid")))
        self.assertTrue(eng.wait_idle())
        eng.cache._db.execute("UPDATE audio SET lufs=NULL")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _analyze_cache(eng.cache, False)
        self.assertIn("measured 1 lines", buf.getvalue())
        self.assertAlmostEqual(eng.cache.loudness(eng.last["key"]), -21.0, delta=0.1)
        with contextlib.redirect_stdout(io.StringIO()) as again:
            _analyze_cache(eng.cache, False)
        self.assertIn("already measured", again.getvalue())


if __name__ == "__main__":
    unittest.main()
