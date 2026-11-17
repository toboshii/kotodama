"""MiniMax's own streaming API (minimax / minimax-hd providers)."""

from __future__ import annotations

import contextlib
import http.server
import io
import json
import threading
import time
import unittest
import urllib.parse
from array import array
from unittest import mock

from kotodama import protocol, tts_generic
from kotodama.adapters import load_adapters
from kotodama.cache import make_key
from kotodama.config import Config
from kotodama.tts_base import TTSError
from kotodama.tts_generic import GenericTTS
from kotodama.tts_minimax import iter_events

from helpers import FakeTTS, KotodamaTestCase, speak_msg

KEY = "mm-test-key"
# PCM slices the fake server streams, deliberately odd-sized in bytes.
SLICES = [array("h", [100 + i] * n).tobytes()[:-1 if i == 1 else None] for i, n in enumerate((351, 450, 278))]
AUDIO = b"".join(SLICES)
EXPECTED = AUDIO[:len(AUDIO) - len(AUDIO) % 2]

# Keys computed with the Replicate adapters while they were still named
# "minimax" / "minimax-hd"; the renamed ones must keep finding that audio.
PINNED_REPLICATE_KEYS = {
    ("minimax-replicate", "Rok"): "a18376070dbc13494c819d151b67b89271c318ae4d87c454dd3f2830bfe008de",
    ("minimax-replicate", "Kupipi"): "5e4f2f877623deec20b2e04af4029e7dc73b63c370736c59eb17965232ed9509",
    ("minimax-replicate-hd", "Rok"): "eaae00c603cb5f1e55f93ce664f8fcfddf9bf5ed9554527c24b3e1a3b785f6ca",
    ("minimax-replicate-hd", "Kupipi"): "93c9d87776e926410ba6492b878b3566b35943a632b701d93d19d2073845591f",
}
PROFILES = {
    "Rok": {"gender": "male", "age": "adult", "pitch": "very low", "tone": "deep, gravelly", "pace": "slow"},
    "Kupipi": {"gender": "female", "age": "young", "pitch": "very high", "tone": "bubbly", "pace": "brisk"},
}

VOICES = {
    "system_voice": [
        {"voice_id": "English_ManWithDeepVoice", "voice_name": "Server Name", "description": ["deep"]},
        {"voice_id": "English_GruffOldSailor", "voice_name": "Gruff Old Sailor",
         "description": ["A gruff, deep old man"]},
        {"voice_id": "Chinese (Mandarin)_Reliable_Executive", "voice_name": "Reliable Executive", "description": []},
    ],
    "voice_cloning": [{"voice_id": "my_clone_rok", "description": ["deep male voice, gravelly"],
                       "created_time": "2026-11-01"}],
    "voice_generation": [],
    "base_resp": {"status_code": 0, "status_msg": "success"},
}


def event(audio: bytes = b"", status: int = 1, code: int = 0, extra: dict | None = None) -> dict:
    ev = {"data": {"audio": audio.hex(), "status": status}, "trace_id": "t",
          "base_resp": {"status_code": code, "status_msg": "ok" if not code else f"error {code}"}}
    if extra:
        ev["extra_info"] = extra
    return ev


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, *a):
        pass


class _MM(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    log: list = []
    framing = "sse"            # sse | bare | json
    aggregate_final = False    # final event repeats the whole line (exclude_aggregated_audio ignored)
    errors: list = []          # base_resp codes for successive requests, before any audio
    fail_after_audio = 0       # base_resp code sent after the first slice
    gate: threading.Event | None = None

    def log_message(self, *a):
        pass

    def _send_json(self, payload, status=200):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _write(self, data: bytes) -> None:
        for i in range(0, len(data), 97):                  # events split across network reads
            part = data[i:i + 97]
            self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
            self.wfile.flush()

    def do_POST(self):
        cls = type(self)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        cls.log.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if urllib.parse.urlparse(self.path).path == "/v1/get_voice":
            return self._send_json(VOICES)
        if cls.errors:
            return self._send_json(event(code=cls.errors.pop(0)))
        if cls.framing == "json":                          # a non-streamed answer, pretty-printed
            data = json.dumps(event(AUDIO, status=2, extra={"usage_characters": 42}), indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        events = [event(s) for s in SLICES]
        final = event(AUDIO if cls.aggregate_final else b"", status=2, extra={"usage_characters": 42})
        for i, ev in enumerate(events + [final]):
            line = json.dumps(ev).encode()
            self._write((b"data: " + line + b"\n\n") if cls.framing == "sse" else (line + b"\n"))
            if i == 0 and cls.fail_after_audio:
                self._write(b"data: " + json.dumps(event(code=cls.fail_after_audio)).encode() + b"\n\n")
                break
            if i == 0 and cls.gate is not None:
                cls.gate.wait(3)
        self.wfile.write(b"0\r\n\r\n")


class MiniMaxCase(KotodamaTestCase):
    def setUp(self):
        super().setUp()
        _MM.log, _MM.framing, _MM.aggregate_final, _MM.errors = [], "sse", False, []
        _MM.fail_after_audio, _MM.gate = 0, None
        self.srv = _Server(("127.0.0.1", 0), _MM)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.addCleanup(self.join_voice_fetches)     # runs first, while the server is up

    @staticmethod
    def join_voice_fetches() -> None:
        # A provider with curated voices fetches the account's list on a
        # background thread. Unfinished, its request could land in the next
        # test's _MM.log (seen on Windows as a second get_voice).
        for t in threading.enumerate():
            if t.name.startswith("voices-"):
                t.join(5)

    def cfg(self, models: dict | None = None, **minimax) -> Config:
        mm = {"base_url": f"http://127.0.0.1:{self.srv.server_port}", "api_key": KEY}
        mm.update(minimax)
        return self.make_config(minimax=mm, models=models or {},
                                replicate={"api_key_env": "KOTODAMA_TEST_NO_SUCH_VAR"})

    def provider(self, name: str = "minimax", cfg: Config | None = None) -> GenericTTS:
        cfg = cfg or self.cfg()
        prov = GenericTTS(cfg, load_adapters(cfg)[name])
        self.addCleanup(prov.close)
        return prov

    def synth(self, prov: GenericTTS, text: str = "Hm.", who: str = "Rok") -> bytes:
        return b"".join(prov.stream(text, prov.voice_request(PROFILES[who], who)))

    def tts_posts(self) -> list[dict]:
        return [e for e in _MM.log if e["path"].startswith("/v1/t2a_v2")]


class EventParserTests(unittest.TestCase):
    def test_framings(self):
        sse = [b'data: {"a": 1}\r\n', b"\r\n: keep-alive\n", b"event: x\ndata: [DONE]\n", b'data: {"b"', b": 2}\n"]
        self.assertEqual(list(iter_events(sse)), [{"a": 1}, {"b": 2}])
        self.assertEqual(list(iter_events([b'{"a": 1}\n{"b"', b': 2}'])), [{"a": 1}, {"b": 2}])
        self.assertEqual(list(iter_events([b'{\n  "a": {\n', b'    "b": 1\n  }\n}'])), [{"a": {"b": 1}}])
        with self.assertRaises(TTSError):
            list(iter_events([b'{"a": ']))


class MiniMaxProviderTests(MiniMaxCase):
    def test_request_shape_and_audio(self):
        prov = self.provider()
        self.assertEqual(self.synth(prov), EXPECTED)
        (post,) = self.tts_posts()
        self.assertEqual(post["path"], "/v1/t2a_v2")
        self.assertEqual(post["auth"], f"Bearer {KEY}")
        body = post["body"]
        self.assertEqual((body["model"], body["text"], body["stream"]), ("speech-2.8-turbo", "Hm.", True))
        self.assertEqual(body["stream_options"], {"exclude_aggregated_audio": True})
        self.assertEqual(body["audio_setting"], {"sample_rate": 24000, "format": "pcm", "channel": 1})
        vs = body["voice_setting"]
        self.assertEqual((vs["speed"], vs["pitch"], vs["vol"]), (0.88, -3, 1))
        self.assertIn(vs["voice_id"], {v.id for v in prov.catalog.voices})
        self.assertNotIn("emotion", vs, "emotion stays automatic")

    def test_audio_is_yielded_while_the_stream_is_open(self):
        _MM.gate = threading.Event()
        prov = self.provider()
        gen = prov.stream("Streaming.", prov.voice_request(PROFILES["Rok"], "Rok"))
        started = time.monotonic()
        first = next(gen)
        waited = time.monotonic() - started
        _MM.gate.set()
        rest = b"".join(gen)
        self.assertLess(waited, 2.0, "the first audio arrived before the server finished the response")
        self.assertEqual(first + rest, EXPECTED)

    def test_every_framing_and_an_aggregated_final_event(self):
        for framing in ("sse", "bare", "json"):
            for aggregate in (False, True):
                with self.subTest(framing=framing, aggregate=aggregate):
                    _MM.framing, _MM.aggregate_final = framing, aggregate
                    self.assertEqual(self.synth(self.provider()), EXPECTED, "no repeated audio")

    def test_errors_inside_a_200(self):
        for code, kind in ((1004, "auth"), (1008, "quota"), (2013, "input")):
            with self.subTest(code=code):
                _MM.errors = [code]
                with self.assertRaises(TTSError) as ctx:
                    self.synth(self.provider())
                self.assertEqual(ctx.exception.code, kind)

    def test_rate_limit_is_retried_only_before_audio(self):
        _MM.errors = [1002]
        with mock.patch.object(tts_generic, "LIVE_RETRY_DELAY", 0.01):
            self.assertEqual(self.synth(self.provider()), EXPECTED)
            self.assertEqual(len(self.tts_posts()), 2)
            _MM.log.clear()
            _MM.fail_after_audio = 1002
            with self.assertRaises(TTSError):
                self.synth(self.provider())
        self.assertEqual(len(self.tts_posts()), 1, "a retry after audio played would repeat the line's start")

    def test_unknown_voice_is_dropped_and_recast(self):
        prov = self.provider()
        first = prov.voice_request(PROFILES["Rok"], "Rok").voice
        _MM.errors = [2054]
        with self.assertRaises(TTSError) as ctx:
            self.synth(prov)
        self.assertEqual(ctx.exception.code, "voice")
        self.assertIsNone(prov.catalog.get(first))
        self.assertNotEqual(prov.voice_request(PROFILES["Rok"], "Rok").voice, first)

    def test_long_lines_are_split_under_the_limit(self):
        with mock.patch.object(tts_generic, "MINIMAX_MAX_CHARS", 60):
            text = "One sentence that is fairly long here. Another one follows it now. And a third ends it."
            pcm = self.synth(self.provider(), text)
        texts = [p["body"]["text"] for p in self.tts_posts()]
        self.assertGreater(len(texts), 1)
        self.assertTrue(all(len(t) <= 60 for t in texts))
        self.assertEqual(" ".join(texts), text)
        self.assertEqual(pcm, EXPECTED * len(texts))

    def test_account_voices_join_the_catalog_and_are_cached(self):
        prov = self.provider()
        prov.load_remote_voices()
        ids = {v.id for v in prov.catalog.voices}
        self.assertIn("English_GruffOldSailor", ids)
        self.assertIn("my_clone_rok", ids, "the account's cloned voices can be cast")
        self.assertNotIn("Chinese (Mandarin)_Reliable_Executive", ids)
        self.assertEqual(prov.catalog.get("English_ManWithDeepVoice").name, "Man With Deep Voice",
                         "curated labels win")
        sailor = prov.catalog.get("English_GruffOldSailor")
        self.assertEqual((sailor.gender, sailor.age), ("male", "old"))
        self.assertEqual(prov.catalog.get("my_clone_rok").gender, "male")
        gets = [e for e in _MM.log if e["path"].startswith("/v1/get_voice")]
        self.assertEqual(gets[0]["body"], {"voice_type": "all"})
        # A later run starts from the cached list, without the network.
        again = self.provider()
        self.assertIn("my_clone_rok", {v.id for v in again.catalog.voices})
        self.assertEqual(len([e for e in _MM.log if e["path"].startswith("/v1/get_voice")]), 1)

    def test_group_id_only_when_configured(self):
        prov = self.provider(cfg=self.cfg(group_id="1234"))
        self.synth(prov)
        prov.load_remote_voices()
        paths = [e["path"] for e in _MM.log]
        self.assertIn("/v1/t2a_v2?GroupId=1234", paths)
        self.assertIn("/v1/get_voice?GroupId=1234", paths)


class CacheAndEngineTests(MiniMaxCase):
    def test_renamed_replicate_adapters_keep_their_cache_keys(self):
        cfg = self.cfg()
        ads = load_adapters(cfg)
        for name in ("minimax-replicate", "minimax-replicate-hd"):
            prov = GenericTTS(cfg, ads[name])
            self.addCleanup(prov.close)
            for who, profile in PROFILES.items():
                req = prov.voice_request(profile, who)
                key = make_key(prov.cache_name, prov.model, req.voice, req.cache_tag, "Hm.", prov.response_format)
                self.assertEqual(key, PINNED_REPLICATE_KEYS[(name, who)], (name, who))
        direct = self.provider()
        req = direct.voice_request(PROFILES["Rok"], "Rok")
        key = make_key(direct.cache_name, direct.model, req.voice, req.cache_tag, "Hm.", direct.response_format)
        self.assertNotIn(key, PINNED_REPLICATE_KEYS.values(), "the direct API caches separately")

    def test_engine_keys_the_cache_by_cache_name(self):
        keys = []
        for name, cache_name in (("renamed", "original"), ("original", None)):
            tts = FakeTTS()
            tts.name = name
            if cache_name:
                tts.cache_name = cache_name
            eng = self.make_engine(tts)
            eng.handle(speak_msg("Hm.", name="Rok", race=8))
            self.assertTrue(eng.wait_idle())
            keys.append(eng.last["key"])
        self.assertEqual(keys[0], keys[1], "a renamed provider finds audio cached under its old name")

    def test_engine_switches_streams_caches_and_counts_billing(self):
        eng = self.make_engine(FakeTTS(), cfg=self.cfg())
        eng.handle({"t": "config", "provider": "minimax"})
        self.assertEqual(eng.tts.name, "minimax")
        job = eng.speak(protocol.parse_speak(speak_msg("Hm.", name="Rok", race=8)))
        self.assertTrue(eng.wait_idle())
        self.assertEqual(job.source, "tts", job.error)
        self.assertIsNotNone(job.ttfb_ms)
        self.assertEqual(eng.cache.stats()["files"], 1)
        today = eng.usage.today()
        self.assertEqual((today["p:minimax:requests"], today["p:minimax:billed_chars"]), (1, 42))
        self.assertIn("42 billed", eng.usage.summary())
        again = eng.speak(protocol.parse_speak(speak_msg("Hm.", name="Rok", race=8, seq=2)))
        self.assertTrue(eng.wait_idle())
        self.assertEqual(again.source, "cache")
        self.assertEqual(len(self.tts_posts()), 1)

    def test_missing_key_is_refused_with_the_right_hint(self):
        eng = self.make_engine(FakeTTS(), cfg=self.cfg(api_key=None, api_key_env="KOTODAMA_TEST_NO_MM_KEY"))
        eng.handle({"t": "config", "provider": "minimax"})
        self.assertEqual(eng.tts.name, "openai")
        self.assertTrue(any("minimax needs an API key (KOTODAMA_TEST_NO_MM_KEY)" in m for m in self.say_lines()))

    def test_doctor_checks_key_and_flags_replicate_shaped_overrides(self):
        from kotodama import cli
        results: list = []
        cfg = self.cfg(models={"minimax": {"input": {"emotion": "{emotion}"}}})
        with contextlib.redirect_stdout(io.StringIO()):
            cli._doctor_models(cfg, lambda *r: results.append(r), "minimax")
        self.assertIn(("MiniMax key", True, "3 system voices, 1 of your own"), results)
        bad = [r for r in results if r[0] == "minimax"]
        self.assertTrue(bad and not bad[0][1] and "models.minimax-replicate" in bad[0][2], results)


if __name__ == "__main__":
    unittest.main()
