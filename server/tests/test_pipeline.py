"""Cache, TTS client, inference, engine pipeline and the UDP front end."""

from __future__ import annotations

import http.server
import json
import os
import socket
import threading
import time
import types
import unittest
import wave

from kotodama import protocol
from kotodama.archetypes import Archetypes
from kotodama.cache import AudioCache, make_key
from kotodama.config import Config
from kotodama.infer import (InferenceError, InferenceManager, build_schema, parse_result)
from kotodama.infer_anthropic import AnthropicInferer
from kotodama.net import UDPServer
from kotodama.player import Player
from kotodama.tts_base import VoiceRequest
from kotodama.tts_openai import OpenAITTS, TTSError
from kotodama.usage import Usage

from helpers import FakeTTS, KotodamaTestCase, speak_msg


def wait_for(cond, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


class CacheTests(KotodamaTestCase):
    def test_key_depends_on_everything_that_changes_audio(self):
        base = ("openai", "m", "onyx", "instr", "text", "pcm")
        k = make_key(*base)
        self.assertEqual(k, make_key(*base))
        for i, changed in enumerate(["x", "x", "echo", "other", "other text", "wav"]):
            args = list(base)
            args[i] = changed
            self.assertNotEqual(k, make_key(*args), f"field {i}")

    def test_writer_commit_get_and_abort(self):
        cache = AudioCache(self.tmp / "c", max_mb=10)
        self.addCleanup(cache.close)
        w = cache.begin("ab" * 32, {"npc": "X", "zone": 1, "text": "hi"}, 24000)
        w.write(b"\x01\x00" * 2400)
        self.assertAlmostEqual(w.commit(), 0.1, places=3)
        path = cache.get("ab" * 32)
        with wave.open(str(path)) as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()),
                             (1, 2, 24000, 2400))
        w2 = cache.begin("cd" * 32, {}, 24000)
        w2.write(b"\x00\x00")
        w2.abort()
        self.assertIsNone(cache.get("cd" * 32))
        self.assertEqual(list(cache.dir.glob("**/*.part")), [])
        self.assertEqual(cache.stats()["files"], 1)

    def test_prune_evicts_least_recently_played(self):
        cache = AudioCache(self.tmp / "c", max_mb=10)   # roomy: nothing is pruned while filling
        self.addCleanup(cache.close)
        keys = []
        for i in range(4):
            key = f"{i:02x}" * 32
            keys.append(key)
            w = cache.begin(key, {"text": str(i)}, 24000)
            w.write(b"\x01\x00" * 40_000)   # 80 KB each
            w.commit()
            time.sleep(0.01)
        cache.get(keys[0])                    # refresh the oldest
        cache.max_bytes = 250_000             # now shrink the budget
        cache.prune()
        remaining = {k for k in keys if cache.path_for(k).is_file()}
        self.assertIn(keys[0], remaining)
        self.assertNotIn(keys[1], remaining)
        self.assertLessEqual(cache.stats()["bytes"], 250_000)

    def test_claim_dedupes_inflight(self):
        cache = AudioCache(self.tmp / "c")
        self.addCleanup(cache.close)
        self.assertTrue(cache.claim("k"))
        self.assertFalse(cache.claim("k"))
        threading.Timer(0.05, cache.release, ["k"]).start()
        self.assertTrue(cache.wait_inflight("k", 2))
        self.assertTrue(cache.claim("k"))


class _Server(http.server.ThreadingHTTPServer):
    def handle_error(self, *a):  # clients that hang up mid-stream are expected in these tests
        pass


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Fake"
    requests: list = []
    status = 200

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if type(self).status != 200:
            data = json.dumps({"error": {"message": "bad voice"}}).encode()
            self.send_response(type(self).status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for size in (1001, 2000, 777):   # odd sizes exercise 16-bit alignment
            self.wfile.write(f"{size:x}\r\n".encode() + b"\x01" * size + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")


class TTSClientTests(KotodamaTestCase):
    def setUp(self):
        super().setUp()
        _Handler.requests = []
        _Handler.status = 200
        self.httpd = _Server(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.cfg = Config({"tts": {"base_url": f"http://127.0.0.1:{self.httpd.server_port}", "api_key": "sk-test"}})

    def new_tts(self, cfg=None):
        tts = OpenAITTS(cfg or self.cfg)
        self.addCleanup(tts.close)
        return tts

    def test_streams_aligned_pcm_with_expected_request(self):
        tts = self.new_tts()
        chunks = list(tts.stream("Hello", VoiceRequest("onyx", {"instructions": "Voice: gruff."})))
        self.assertTrue(all(len(c) % 2 == 0 for c in chunks))
        # 1001 + 2000 + 777 bytes arrive in odd-sized chunks; every yielded chunk is still sample-aligned.
        self.assertEqual(sum(len(c) for c in chunks), 1001 + 2000 + 777)
        req = _Handler.requests[0]
        self.assertEqual(req["path"], "/v1/audio/speech")
        self.assertEqual(req["auth"], "Bearer sk-test")
        self.assertEqual(req["body"], {"model": "gpt-4o-mini-tts", "input": "Hello", "voice": "onyx",
                                       "instructions": "Voice: gruff.", "response_format": "pcm"})

    def test_connection_is_reused(self):
        tts = self.new_tts()
        list(tts.stream("a", VoiceRequest("onyx", {"instructions": ""})))
        self.assertEqual(len(tts.http._pool), 1)
        conn = tts.http._pool[0][0]
        list(tts.stream("b", VoiceRequest("onyx", {"instructions": ""})))
        self.assertIs(tts.http._pool[0][0], conn)

    def test_early_close_drops_connection(self):
        tts = self.new_tts()
        gen = tts.stream("a", VoiceRequest("onyx", {"instructions": ""}))
        next(gen)
        gen.close()
        self.assertEqual(tts.http._pool, [])

    def test_error_status_raises_with_message(self):
        _Handler.status = 400
        with self.assertRaises(TTSError) as cm:
            list(self.new_tts().stream("a", VoiceRequest("nope", {"instructions": ""})))
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("bad voice", str(cm.exception))

    def test_missing_key_and_non_gpt4o_models(self):
        with self.assertRaises(TTSError):
            list(OpenAITTS(Config({"tts": {"api_key": None, "api_key_env": "KOTODAMA_NO_SUCH_ENV"}}))
                 .stream("a", VoiceRequest("onyx", {"instructions": ""})))
        cfg = Config({"tts": {"model": "tts-1", "base_url": self.cfg.get("tts.base_url"), "api_key": "k"}})
        list(self.new_tts(cfg).stream("a", VoiceRequest("onyx", {"instructions": "ignored"})))
        self.assertNotIn("instructions", _Handler.requests[-1]["body"])


class FakeAnthropic:
    """Records the request; returns a canned response."""

    def __init__(self, text=None, stop_reason="end_turn"):
        self.calls = []
        self.text, self.stop_reason = text, stop_reason
        self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=self._create_beta))
        self.messages = types.SimpleNamespace(create=self._create)

    def _response(self):
        content = [types.SimpleNamespace(type="thinking", thinking="")]
        if self.text is not None:
            content.append(types.SimpleNamespace(type="text", text=self.text))
        return types.SimpleNamespace(stop_reason=self.stop_reason, content=content)

    def _create_beta(self, **kw):
        self.calls.append(("beta", kw))
        return self._response()

    def _create(self, **kw):
        self.calls.append(("plain", kw))
        return self._response()


GOOD = {"gender": "female", "age": "elderly", "species": "Hume", "personality": "kindly",
        "tone": "warm", "accent": "", "pitch": "medium", "pace": "slow", "voice": "sage",
        "direction": "Speaks softly."}


class InferenceTests(KotodamaTestCase):
    def setUp(self):
        super().setUp()
        self.arch = Archetypes.load()

    def test_schema_is_strict_and_matches_vocab(self):
        s = build_schema(self.arch, "openai")
        self.assertFalse(s["additionalProperties"])
        self.assertEqual(set(s["required"]), set(s["properties"]))
        self.assertEqual(s["properties"]["pitch"]["enum"], self.arch.vocab["pitch"])
        self.assertIn("onyx", s["properties"]["voice"]["enum"])

    def test_parse_result(self):
        p = parse_result(json.dumps(GOOD), self.arch, "openai")
        self.assertEqual(p["voice"], {"openai": "sage"})
        self.assertEqual(p["age"], "elderly")
        for bad in ("not json", "[]", "{}", json.dumps({"age": "ancient"})):
            with self.assertRaises(InferenceError):
                parse_result(bad, self.arch, "openai")

    def test_anthropic_default_model_is_haiku_with_plain_request(self):
        client = FakeAnthropic(json.dumps(GOOD))
        cfg = Config({"inference": {"provider": "anthropic"}})
        inf = AnthropicInferer(cfg, self.arch, build_schema(self.arch, "openai"), "openai", client=client)
        out = inf.infer({"name": "Ayame"})
        self.assertEqual(out["voice"], {"openai": "sage"})
        kind, kw = client.calls[0]
        self.assertEqual(kind, "plain", "Haiku has no server-side fallback")
        self.assertEqual(kw["model"], "claude-haiku-5-5")
        self.assertNotIn("fallbacks", kw)
        self.assertNotIn("betas", kw)
        self.assertEqual(kw["output_config"]["effort"], "low")
        self.assertEqual(kw["output_config"]["format"]["type"], "json_schema")
        for forbidden in ("temperature", "top_p", "top_k", "thinking", "budget_tokens"):
            self.assertNotIn(forbidden, kw)
        self.assertIn("Ayame", kw["messages"][0]["content"])

    def test_anthropic_opus_opts_in_to_server_side_fallback(self):
        client = FakeAnthropic(json.dumps(GOOD))
        cfg = Config({"inference": {"provider": "anthropic", "model": "claude-opus-5-5"}})
        AnthropicInferer(cfg, self.arch, {}, "openai", client=client).infer({"name": "X"})
        kind, kw = client.calls[0]
        self.assertEqual(kind, "beta")
        self.assertEqual(kw["model"], "claude-opus-5-5")
        self.assertEqual(kw["betas"], ["server-side-fallback-2026-07-01"])
        self.assertEqual(kw["fallbacks"], "default")

    def test_anthropic_refusal_and_truncation_raise(self):
        for reason in ("refusal", "max_tokens"):
            inf = AnthropicInferer(Config(), self.arch, {}, "openai",
                                   client=FakeAnthropic(json.dumps(GOOD), stop_reason=reason))
            with self.assertRaises(InferenceError):
                inf.infer({"name": "X"})
        with self.assertRaises(InferenceError):
            AnthropicInferer(Config(), self.arch, {}, "openai", client=FakeAnthropic(None)).infer({})

    def test_manager_casts_once_throttles_and_backs_off(self):
        lib = self.make_engine().lib
        usage = Usage(self.tmp / "u.json")
        calls = []

        class Inferer:
            model = "fake-llm"

            def infer(self, ctx):
                calls.append(ctx)
                if ctx["name"] == "Broken":
                    raise InferenceError("boom")
                return self.arch.sanitize({**GOOD, "voice": {"openai": "sage"}})

            arch = self.arch

        cfg = Config({"inference": {"max_per_minute": 3, "wait_ms": 2000}})
        mgr = InferenceManager(cfg, self.arch, lib, usage, Inferer())
        from kotodama.protocol import Speaker
        kw = dict(zone=1, zone_name="Z", player="Adventurer", archetype_key="unknown", base_profile={"tone": "x"})

        f1 = mgr.request(Speaker(name="Nanaa", race=7), samples=["Hello Adventurer!"], **kw)
        f1b = mgr.request(Speaker(name="Nanaa", race=7), samples=["again"], **kw)
        self.assertTrue(mgr.wait(f1))
        self.assertIs(f1, f1b if f1b is not None else f1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["sample_lines"], ["Hello {player}!"], "player name scrubbed before leaving the machine")
        self.assertTrue(lib.has_any("Nanaa"))
        self.assertEqual(lib.local["nanaa"]["meta"]["source"], "llm:fake-llm")
        self.assertEqual(lib.local["nanaa"]["meta"]["sample_lines"], ["Hello {player}!"])
        self.assertIsNone(mgr.request(Speaker(name="Nanaa"), samples=None, **kw), "already known")

        fb = mgr.request(Speaker(name="Broken"), samples=None, **kw)
        self.assertFalse(mgr.wait(fb))
        self.assertFalse(lib.has_any("Broken"))
        self.assertIsNone(mgr.request(Speaker(name="Broken"), samples=None, **kw), "failure backoff")

        mgr.request(Speaker(name="Third"), samples=None, **kw)
        wait_for(lambda: len(calls) == 3)
        self.assertIsNone(mgr.request(Speaker(name="Fourth"), samples=None, **kw), "rate limited")

    def _ahkk_inferer(self):
        """Casts from the lines like a sensible LLM would: Mithra child if it
        hears a meow, otherwise the name-only guess Haiku actually made."""
        arch = self.arch

        class Inferer:
            model = "fake-llm"
            calls: list = []

            def infer(self, ctx):
                self.calls.append(ctx)
                if "meow" in " ".join(ctx.get("sample_lines") or []).lower():
                    return arch.sanitize({"gender": "female", "age": "child", "species": "Mithra"})
                return arch.sanitize({"gender": "male", "age": "adult", "species": "Tarutaru"})

        inf = Inferer()
        inf.calls = []
        return inf

    def test_unknown_race_is_cast_from_its_first_line_not_on_target(self):
        # Ahkk Jharcham: race 0 ("Precomposed NPC"), unmapped model. Targeting
        # her used to start a name-only casting that her lines then joined.
        inf = self._ahkk_inferer()
        eng = self.make_engine(inferer=inf)
        hint = {"t": "hint", "reason": "target", "zone": 50, "zone_name": "Aht Urhgan Whitegate"}
        eng.handle({**hint, "npc": "Ahkk Jharcham", "race": 0, "race_name": "Precomposed NPC", "model": 999})
        eng.handle({**hint, "npc": "Rok", "race": 8, "race_name": "Galka"})
        self.assertTrue(wait_for(lambda: not eng.inference._inflight and inf.calls))
        self.assertEqual([c["name"] for c in inf.calls], ["Rok"], "a known race is still cast on target")

        msg = speak_msg("Mew mew mew mew mew meooow Tulutululu, Meow!", name="Ahkk Jharcham", race=0, zone=50,
                        zone_name="Aht Urhgan Whitegate")
        msg["speaker"]["race_name"] = "Precomposed NPC"
        eng.handle(msg)
        self.assertTrue(eng.wait_idle())
        ahkk = [c for c in inf.calls if c["name"] == "Ahkk Jharcham"]
        self.assertEqual(len(ahkk), 1)
        self.assertEqual(ahkk[0]["sample_lines"], ["Mew mew mew mew mew meooow Tulutululu, Meow!"])
        self.assertEqual(eng.lib.local["ahkk jharcham"]["profile"]["species"], "Mithra")
        self.assertIn("Mithra", eng.tts.calls[0]["instructions"], "the first line already uses the new casting")

    # -- how long a first line waits for its casting ---------------------------

    def _slow_engine(self):
        """Casting takes longer than wait_ms but well within unknown_wait_ms."""
        arch = self.arch

        class Inferer:
            model = "fake-llm"

            def infer(self, ctx):
                time.sleep(0.4)
                return arch.sanitize({"gender": "male", "age": "middle-aged", "pitch": "low",
                                      "tone": "gruff", "personality": "blunt tradesman"})

        cfg = self.make_config(inference={"wait_ms": 50, "unknown_wait_ms": 3000})
        return self.make_engine(cfg=cfg, inferer=Inferer())

    def _first_lines(self, eng, name, race, race_name):
        for seq, text in enumerate(("Hah! Yah!", "Looks like someone's come a long way."), 1):
            msg = speak_msg(text, name=name, race=race, seq=seq, zone=235, zone_name="Bastok Markets")
            msg["speaker"]["race_name"] = race_name
            eng.handle(msg)
            self.assertTrue(eng.wait_idle())
            self.assertTrue(wait_for(lambda: not eng.inference._inflight), "casting lands before the next line")
        return eng.tts.calls

    def test_a_raceless_npc_waits_for_their_casting(self):
        # Degenhard: race 0, so the only stand-in is the genderless "unknown"
        # voice, and a casting that missed the first line changed his voice.
        eng = self._slow_engine()
        first, second = self._first_lines(eng, "Degenhard", 0, "Precomposed NPC")
        self.assertIn("gruff", first["instructions"], "the first line already has the casting")
        self.assertEqual(first["instructions"], second["instructions"])

    def test_an_npc_with_a_race_does_not_wait_as_long(self):
        eng = self._slow_engine()
        with self.assertLogs("kotodama.engine", "INFO") as logs:
            first, second = self._first_lines(eng, "Rok", 8, "Galka")
        self.assertIn("Galka", first["instructions"])
        self.assertNotIn("gruff", first["instructions"], "spoken with the race's stand-in")
        self.assertIn("gruff", second["instructions"])
        self.assertTrue(any("Rok speaks before their casting is ready" in m for m in logs.output))

    def test_reinfer_after_restart_reuses_stored_lines(self):
        inf = self._ahkk_inferer()
        eng = self.make_engine(inferer=inf)       # fresh sidecar: has not heard her this session
        eng.lib.put("Ahkk Jharcham", self.arch.sanitize({"gender": "male", "species": "Tarutaru"}),
                    source="llm:old", game_race="Precomposed NPC", model_id=182, zone=50,
                    sample="Meow meow, what a beautiful day")
        eng.handle({"t": "cmd", "name": "voice_reinfer", "npc": "Ahkk Jharcham"})
        self.assertTrue(wait_for(lambda: eng.lib.local.get("ahkk jharcham", {}).get("meta", {}).get("source")
                                 == "llm:fake-llm"))
        self.assertEqual(inf.calls[0]["sample_lines"], ["Meow meow, what a beautiful day"])
        self.assertEqual(eng.lib.local["ahkk jharcham"]["profile"]["species"], "Mithra")

    # -- a "???" who is later introduced ---------------------------------------

    UNNAMED = "??? (243:10191)"

    def _race_inferer(self):
        """Casts from the race the game reports, and guesses male without one
        (what Haiku did for Chamama's first line)."""
        arch = self.arch

        class Inferer:
            model = "fake-llm"

            def __init__(self):
                self.calls = []

            def infer(self, ctx):
                self.calls.append(ctx)
                gender = "female" if ctx.get("game_race") == "Tarutaru Female" else "male"
                return arch.sanitize({"gender": gender, "age": "adult", "species": "Tarutaru",
                                      "tone": "sharp and haughty", "personality": "pushy"})

        return Inferer()

    def _line(self, text, name, seq, race=None, was=None):
        msg = speak_msg(text, name=name, race=race, seq=seq, zone=243, zone_name="Ru'Lude Gardens")
        msg["speaker"]["race_name"] = "Tarutaru Female" if race == 6 else None
        if was:
            msg["speaker"]["was"] = was
        return msg

    def test_an_introduced_unnamed_character_keeps_their_voice(self):
        inf = self._race_inferer()
        eng = self.make_engine(inferer=inf)
        eng.handle(self._line("Hmph!", self.UNNAMED, 1, race=6))
        self.assertTrue(eng.wait_idle())
        eng.handle(self._line("That's Chamama!", "Chamama", 2, race=6, was=self.UNNAMED))
        self.assertTrue(eng.wait_idle())
        self.assertEqual([c["name"] for c in inf.calls], [self.UNNAMED], "Chamama isn't cast again")
        self.assertEqual(eng.lib.local["chamama"]["meta"]["source"], f"same-as:{self.UNNAMED}")
        before, after = eng.tts.calls
        self.assertEqual((after["voice"], after["instructions"]), (before["voice"], before["instructions"]))

        eng.handle({"t": "cmd", "name": "voice_reinfer", "npc": "Chamama"})
        self.assertTrue(wait_for(lambda: len(inf.calls) == 2), "a carried-over voice can still be recast")

    def test_not_when_the_game_says_otherwise(self):
        inf = self._race_inferer()
        eng = self.make_engine(inferer=inf)
        eng.handle(self._line("Hmph!", self.UNNAMED, 1))                  # no actor found: cast male
        self.assertTrue(eng.wait_idle())
        eng.handle(self._line("That's Chamama!", "Chamama", 2, race=6, was=self.UNNAMED))
        self.assertTrue(eng.wait_idle())
        self.assertEqual([c["name"] for c in inf.calls], [self.UNNAMED, "Chamama"])
        self.assertEqual(eng.lib.local["chamama"]["profile"]["gender"], "female", "correct beats consistent")

    def test_not_over_a_voice_they_already_have(self):
        inf = self._race_inferer()
        eng = self.make_engine(inferer=inf)
        eng.lib.put("Chamama", self.arch.sanitize({"gender": "female", "tone": "warm"}), source="curated")
        eng.handle(self._line("Hmph!", self.UNNAMED, 1, race=6))
        self.assertTrue(eng.wait_idle())
        eng.handle(self._line("That's Chamama!", "Chamama", 2, race=6, was=self.UNNAMED))
        self.assertTrue(eng.wait_idle())
        self.assertEqual(eng.lib.local["chamama"]["meta"]["source"], "curated")
        self.assertEqual(eng.lib.local["chamama"]["profile"]["tone"], "warm")

    def test_a_pack_outranks_a_carried_over_voice(self):
        eng = self.make_engine()
        eng.lib.put("Chamama", self.arch.sanitize({"tone": "carried"}), source=f"same-as:{self.UNNAMED}")
        eng.lib.packs["chamama"] = {"key": "Chamama", "profile": {"tone": "from a pack"}, "meta": {"pack": "p"}}
        self.assertEqual(eng.lib.lookup("chamama")[0]["tone"], "from a pack")

    def test_disabled_manager_is_inert(self):
        eng = self.make_engine()
        from kotodama.protocol import Speaker
        self.assertFalse(eng.inference.enabled)
        self.assertIsNone(eng.inference.request(Speaker(name="X"), zone=1, zone_name=None, samples=None,
                                                player=None, archetype_key="unknown", base_profile={}))


class EnginePipelineTests(KotodamaTestCase):
    def test_miss_synthesizes_and_caches_then_hit_skips_tts(self):
        tts = FakeTTS()
        eng = self.make_engine(tts)
        job = eng.speak(protocol.parse_speak(speak_msg("Welcome to Southern San d'Oria.")))
        self.assertTrue(eng.wait_idle())
        self.assertEqual((job.source, len(tts.calls), tts.completed), ("tts", 1, 1))
        self.assertEqual(eng.cache.stats()["files"], 1)

        job2 = eng.speak(protocol.parse_speak(speak_msg("Welcome to Southern San d'Oria.", seq=2)))
        self.assertTrue(eng.wait_idle())
        self.assertEqual((job2.source, len(tts.calls)), ("cache", 1), "cache hit must not call the API")
        self.assertEqual(eng.usage.today()["cache_hits"], 1)

    def test_voice_follows_race_and_overrides_change_cache_key(self):
        tts = FakeTTS()
        eng = self.make_engine(tts)
        eng.handle(speak_msg("Hm.", name="Rok", race=8))
        eng.wait_idle()
        self.assertIn(tts.calls[0]["voice"], ("onyx", "cedar", "ash"))
        self.assertIn("Galka", tts.calls[0]["instructions"])

        eng.handle({"t": "cmd", "name": "voice_set", "npc": "Rok", "params": {"tone": "cheerful", "voice": "shimmer"}})
        eng.handle(speak_msg("Hm.", name="Rok", race=8, seq=2))
        eng.wait_idle()
        self.assertEqual(len(tts.calls), 2, "new profile -> new audio")
        self.assertEqual(tts.calls[1]["voice"], "shimmer")
        self.assertIn("cheerful", tts.calls[1]["instructions"])

    def test_player_name_is_not_sent_to_library_files(self):
        eng = self.make_engine()
        eng.handle(speak_msg("Greetings, Adventurer.", name="Ayame"))
        eng.wait_idle()
        eng.lib.put("Ayame", {"tone": "x"}, source="llm:m", sample="Greetings, {player}.")
        eng.lib.flush()
        self.assertNotIn("Adventurer", eng.lib.file.read_text(encoding="utf-8"))

    def test_tts_error_is_reported_once_and_nothing_cached(self):
        eng = self.make_engine(FakeTTS(fail="TTS HTTP 401: bad key"))
        for i in range(3):
            eng.handle(speak_msg(f"Line {i}", seq=i))
            eng.wait_idle()
        errors = [m for m in self.say_lines() if "bad key" in m]
        self.assertEqual(len(errors), 1, "repeated identical errors are rate limited")
        self.assertEqual(eng.cache.stats()["files"], 0)
        self.assertEqual(list(eng.cache.dir.glob("**/*.part")), [])

    def test_interrupt_stops_playback_but_download_still_completes_into_cache(self):
        tts = FakeTTS(chunks=10, delay=0.03)
        eng = self.make_engine(tts, player=Player({"stream": ["sh", "-c", "cat > /dev/null"],
                                                   }), null_player=False)
        j1 = eng.speak(protocol.parse_speak(speak_msg("First long line.", seq=1)))
        self.assertTrue(wait_for(lambda: j1.handle is not None and j1.ttfb_ms is not None))
        j2 = eng.speak(protocol.parse_speak(speak_msg("Second line.", seq=2)))
        self.assertTrue(j1.cancel.is_set(), "new line interrupts the previous one")
        self.assertTrue(wait_for(lambda: j1.handle.proc.poll() is not None, 2), "player killed")
        self.assertTrue(eng.wait_idle(10))
        self.assertEqual(tts.completed, 2, "cut-off line was still downloaded in full")
        self.assertEqual(eng.cache.stats()["files"], 2)
        j2.stop_playback()

    def test_without_finish_on_stop_cut_line_is_not_cached(self):
        tts = FakeTTS(chunks=20, delay=0.02)
        cfg = self.make_config(cache={"finish_download_on_stop": False})
        eng = self.make_engine(tts, cfg=cfg)
        j1 = eng.speak(protocol.parse_speak(speak_msg("Cut me off.")))
        self.assertTrue(wait_for(lambda: j1.ttfb_ms is not None))
        eng.handle({"t": "stop"})
        self.assertTrue(eng.wait_idle(5))
        self.assertEqual(eng.cache.stats()["files"], 0)
        self.assertEqual(tts.completed, 0)

    def test_queue_mode_waits_for_previous_playback(self):
        order = []
        tts = FakeTTS(chunks=2)
        eng = self.make_engine(tts)
        eng.interrupt = False
        j1 = eng.speak(protocol.parse_speak(speak_msg("One.", seq=1)))
        j2 = eng.speak(protocol.parse_speak(speak_msg("Two.", seq=2)))
        self.assertIs(j2.prev_done, j1.playback_done)
        self.assertFalse(j1.cancel.is_set())
        self.assertTrue(eng.wait_idle())

    def test_replay(self):
        eng = self.make_engine()
        eng.handle({"t": "replay"})
        self.assertIn("nothing to replay yet", self.say_lines())
        eng.handle(speak_msg("Remember this."))
        eng.wait_idle()
        eng.handle({"t": "replay"})
        self.assertTrue(eng.wait_idle())
        self.assertEqual(eng.last_job.source, "replay")

    def test_commands(self):
        eng = self.make_engine()
        eng.handle(speak_msg("Hello.", name="Ayame"))
        eng.wait_idle()
        eng.handle({"t": "cmd", "name": "voice_show", "npc": "Ayame"})
        self.assertTrue(any("archetype:hume_f" in m for m in self.say_lines()))
        eng.handle({"t": "cmd", "name": "voice_set", "npc": "Ayame", "params": {"age": "ancient"}})
        self.assertTrue(any("age must be one of" in m for m in self.say_lines()))
        eng.handle({"t": "cmd", "name": "voice_set", "npc": "Ayame", "params": {"bogus": "x"}})
        self.assertTrue(any("unknown field" in m for m in self.say_lines()))
        eng.handle({"t": "cmd", "name": "voice_set", "npc": "Ayame", "params": {"age": "elderly"}})
        self.assertEqual(eng.lib.override("ayame"), {"age": "elderly"})
        eng.handle({"t": "cmd", "name": "voice_promote", "npc": "Ayame"})
        self.assertEqual(eng.lib.local["ayame"]["meta"]["source"], "curated")
        eng.handle({"t": "cmd", "name": "voice_reset", "npc": "Ayame"})
        self.assertIsNone(eng.lib.override("ayame"))
        eng.handle({"t": "cmd", "name": "voice_reinfer", "npc": "Ayame"})
        self.assertTrue(any("casting is off" in m or "voice casting is off" in m for m in self.say_lines()))
        eng.handle({"t": "cmd", "name": "status"})
        self.assertTrue(any(m.startswith("cache:") for m in self.say_lines()))

    def test_hello_config_ping(self):
        eng = self.make_engine()
        eng.handle({"t": "hello", "player": "Adventurer"})
        eng.handle({"t": "ping"})
        self.assertEqual([m["t"] for m in self.replies], ["pong", "pong"])
        self.assertEqual(eng.player_name, "Adventurer")
        self.assertEqual(eng.tts.warmed, 1)
        eng.handle({"t": "config", "volume": 250, "interrupt": False})
        self.assertEqual((eng.volume, eng.interrupt), (250, False), "volume can be boosted past 100")


class UDPTests(KotodamaTestCase):
    def test_end_to_end_over_udp(self):
        tts = FakeTTS()
        cfg = self.make_config(listen={"host": "127.0.0.1", "port": 0})
        eng = self.make_engine(tts, cfg=cfg)
        server = UDPServer(cfg, eng)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)

        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(3)
        self.addCleanup(client.close)
        client.connect(("127.0.0.1", server.port))

        client.send(protocol.encode({"t": "hello", "player": "Adventurer"}))
        self.assertEqual(protocol.decode(client.recv(65535)), {"t": "pong"})

        client.send(b"garbage")                                   # ignored, server survives
        client.send(protocol.encode(speak_msg("Over the wire.")))
        self.assertTrue(wait_for(lambda: len(tts.calls) == 1))
        self.assertTrue(eng.wait_idle())
        client.send(protocol.encode({"t": "cmd", "name": "status"}))
        replies = [protocol.decode(client.recv(65535)) for _ in range(3)]
        self.assertTrue(all(r["t"] == "log" for r in replies))

    def test_refuses_non_loopback(self):
        eng = self.make_engine()
        with self.assertRaises(ValueError):
            UDPServer(Config({"listen": {"host": "0.0.0.0", "port": 0}}), eng)


if __name__ == "__main__":
    unittest.main()
