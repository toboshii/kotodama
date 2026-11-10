"""Gemini 3.8 Flash TTS through Google's Interactions API (gemini / gemini-lite)."""

from __future__ import annotations

import base64
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
from kotodama.casting import cast
from kotodama.config import Config
from kotodama.decode import wav_bytes
from kotodama.tts_base import TTSError
from kotodama.tts_generic import GenericTTS

from helpers import FakeTTS, KotodamaTestCase, speak_msg

KEY = "gm-test-key"
SLICES = [array("h", [200 + i] * n).tobytes()[:-1 if i == 1 else None] for i, n in enumerate((333, 412, 290))]
AUDIO = b"".join(SLICES)
EXPECTED = AUDIO[:len(AUDIO) - len(AUDIO) % 2]
USAGE = {"total_input_tokens": 12, "total_output_tokens": 75, "total_tokens": 87}

PROFILES = {
    "Rok": {"gender": "male", "age": "adult", "species": "Galka", "pitch": "very low",
            "tone": "deep, gravelly and rumbling", "personality": "stoic", "pace": "slow"},
    "Kupipi": {"gender": "female", "age": "young", "species": "Tarutaru", "pitch": "very high",
               "tone": "bright and bubbly", "personality": "excitable", "pace": "brisk"},
    "Plain": {"gender": "female", "pace": "normal"},
}

PAGE1 = [
    {"id": "Zephyr", "display_name": "Zephyr", "language_code": "en-US", "gender": "MALE", "pitch": "MEDIUM",
     "description": "Bright"},                                   # disagrees with the fallback set on purpose
    {"id": "Kore", "display_name": "Kore", "language_code": "en-US", "gender": "FEMALE", "pitch": "MEDIUM",
     "description": "Firm"},
    {"id": "voice_custom1", "display_name": "Lilting Sprite", "type": "prompted", "gender": "FEMALE",
     "pitch": "HIGH", "description": "a bright, bubbly young woman"},
]
PAGE2 = [
    {"id": "voices/aurelian", "display_name": "Aurelian", "language_code": "en-GB", "accent": "British",
     "gender": "MALE", "pitch": "LOW", "description": "deep, gravelly older man"},
    {"id": "voices/amelie", "display_name": "Amelie", "language_code": "fr-FR", "gender": "FEMALE",
     "pitch": "MEDIUM", "description": "warm"},
]


def ev(event_type: str, **fields) -> dict:
    return {"event_type": event_type, "event_id": "e", **fields}


def audio_delta(pcm: bytes, rate: int = 24000) -> dict:
    return ev("step.delta", index=0, delta={"type": "audio", "mime_type": "audio/l16", "sample_rate": rate,
                                             "data": base64.b64encode(pcm).decode()})


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, *a):
        pass


class _Gemini(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    log: list = []
    framing = "sse"           # sse | bare | unary
    http_errors: list = []    # (status, google error object) for successive requests
    error_after_audio: dict | None = None
    failed_status = False
    delta_rate = 24000
    gate: threading.Event | None = None

    def log_message(self, *a):
        pass

    def _json(self, status: int, payload) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _record(self, body=None):
        type(self).log.append({"method": self.command, "path": self.path, "key": self.headers.get("x-goog-api-key"),
                               "auth": self.headers.get("Authorization"), "body": body})

    def do_GET(self):
        self._record()
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        if url.path != "/v1beta/voices":
            return self._json(404, {"error": {"code": 404, "message": "not found", "status": "NOT_FOUND"}})
        if q.get("page_token") == ["p2"]:
            return self._json(200, {"voices": PAGE2})
        self._json(200, {"voices": PAGE1, "next_page_token": "p2"})

    def _write(self, data: bytes) -> None:
        for i in range(0, len(data), 101):
            part = data[i:i + 101]
            self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
            self.wfile.flush()

    def do_POST(self):
        cls = type(self)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self._record(body)
        if cls.http_errors:
            status, err = cls.http_errors.pop(0)
            return self._json(status, {"error": err})
        if cls.framing == "unary":
            wav = base64.b64encode(wav_bytes(EXPECTED, 24000)).decode()
            return self._json(200, {"id": "i1", "status": "completed", "usage": USAGE, "steps": [
                {"type": "model_output", "content": [{"type": "audio", "mime_type": "audio/wav", "data": wav}]}]})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        if cls.delta_rate != 24000:
            slices = [array("h", [500] * 1600).tobytes()]
        else:
            slices = SLICES
        events = [ev("interaction.created", interaction={"id": "i1", "status": "in_progress"}),
                  ev("step.start", index=0, step={"type": "model_output"}),
                  ev("step.delta", index=0, delta={"type": "text", "text": "ignored"})]
        events += [audio_delta(s, cls.delta_rate) for s in slices]
        events += [ev("step.stop", index=0),
                   ev("interaction.completed", interaction={"id": "i1", "status": "failed" if cls.failed_status
                                                            else "completed", "usage": USAGE,
                                                            "errors": [{"message": "synthesis failed"}]})]
        sent_audio = 0
        for e in events:
            line = json.dumps(e).encode()
            self._write((b"data: " + line + b"\n\n") if cls.framing == "sse" else (line + b"\n"))
            if e["event_type"] == "step.delta" and e["delta"]["type"] == "audio":
                sent_audio += 1
                if sent_audio == 1 and cls.error_after_audio:
                    self._write(b"data: " + json.dumps(ev("error", error=cls.error_after_audio)).encode() + b"\n\n")
                    break
                if sent_audio == 1 and cls.gate is not None:
                    cls.gate.wait(3)
        self.wfile.write(b"0\r\n\r\n")


class GeminiCase(KotodamaTestCase):
    def setUp(self):
        super().setUp()
        _Gemini.log, _Gemini.framing, _Gemini.http_errors = [], "sse", []
        _Gemini.error_after_audio, _Gemini.failed_status, _Gemini.delta_rate, _Gemini.gate = None, False, 24000, None
        self.srv = _Server(("127.0.0.1", 0), _Gemini)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def cfg(self, models: dict | None = None, **gemini) -> Config:
        g = {"base_url": f"http://127.0.0.1:{self.srv.server_port}", "api_key": KEY}
        g.update(gemini)
        return self.make_config(gemini=g, models=models or {})

    def provider(self, name: str = "gemini", cfg: Config | None = None) -> GenericTTS:
        cfg = cfg or self.cfg()
        prov = GenericTTS(cfg, load_adapters(cfg)[name])
        self.addCleanup(prov.close)
        return prov

    def synth(self, prov: GenericTTS, text: str = "Hm.", who: str = "Rok") -> bytes:
        return b"".join(prov.stream(text, prov.voice_request(PROFILES[who], who)))

    def posts(self) -> list[dict]:
        return [e for e in _Gemini.log if e["method"] == "POST"]


class RequestTests(GeminiCase):
    def test_request_shape_and_audio(self):
        prov = self.provider()
        self.assertEqual(self.synth(prov), EXPECTED)
        (post,) = self.posts()
        self.assertEqual(post["path"], "/v1beta/interactions", "nothing (least of all the key) in the URL")
        self.assertEqual((post["key"], post["auth"]), (KEY, None))
        body = post["body"]
        self.assertEqual((body["model"], body["stream"]), ("gemini-3.8-flash-tts", True))
        self.assertIs(body["store"], False, "a stored interaction starts ~600 ms later")
        self.assertEqual(body["response_format"], {"type": "audio", "mime_type": "audio/l16", "sample_rate": 24000})
        (item,) = body["input"]
        self.assertEqual(item["type"], "user_input")
        (content,) = item["content"]
        self.assertEqual((content["type"], content["text"]), ("text", "Hm."))
        (note,) = content["annotations"]
        self.assertEqual(note["type"], "speech_metadata")
        self.assertIn("gravelly", note["style"])
        self.assertIn("slow", note["style"])
        for trait in ("male", "Galka", "adult"):
            self.assertNotIn(trait, note["style"], "immutable traits come from the voice, not the style")
        (speaker,) = body["generation_config"]["speech_config"]
        self.assertIn(speaker["voice"], {v.id for v in prov.catalog.voices})
        req = prov.voice_request(PROFILES["Rok"], "Rok")
        self.assertNotIn("store", req.cache_tag, "the cache key is about the audio; store doesn't change it")

    def test_plain_profile_sends_no_style(self):
        self.synth(self.provider(), who="Plain")
        self.assertNotIn("annotations", self.posts()[0]["body"]["input"][0]["content"][0])

    def test_lite_model(self):
        self.synth(self.provider("gemini-lite"))
        self.assertEqual(self.posts()[0]["body"]["model"], "gemini-3.8-flash-lite-tts")

    def test_audio_is_yielded_while_the_stream_is_open(self):
        _Gemini.gate = threading.Event()
        prov = self.provider()
        gen = prov.stream("Streaming.", prov.voice_request(PROFILES["Rok"], "Rok"))
        started = time.monotonic()
        first = next(gen)
        waited = time.monotonic() - started
        _Gemini.gate.set()
        self.assertLess(waited, 2.0, "audio arrived before the server finished the response")
        self.assertEqual(first + b"".join(gen), EXPECTED)

    def test_framings_and_resampling(self):
        for framing in ("sse", "bare", "unary"):
            with self.subTest(framing=framing):
                _Gemini.framing = framing
                self.assertEqual(self.synth(self.provider()), EXPECTED)
        _Gemini.framing, _Gemini.delta_rate = "sse", 16000
        out = array("h")
        out.frombytes(self.synth(self.provider()))
        self.assertAlmostEqual(len(out), 2400, delta=3, msg="16 kHz deltas are resampled to 24 kHz")
        self.assertEqual(set(out), {500})


class ErrorTests(GeminiCase):
    def check(self, status: int, err: dict, code: str) -> None:
        _Gemini.http_errors = [(status, err)]
        with self.assertRaises(TTSError) as ctx:
            self.synth(self.provider())
        self.assertEqual(ctx.exception.code, code, ctx.exception)

    def test_http_errors(self):
        self.check(403, {"code": 403, "message": "API key not valid. Please pass a valid API key.",
                         "status": "PERMISSION_DENIED"}, "auth")
        self.check(429, {"code": 429, "message": "You exceeded your current quota, please check your plan and "
                         "billing details.", "status": "RESOURCE_EXHAUSTED"}, "quota")
        self.check(404, {"code": 404, "message": "models/gemini-9 is not found", "status": "NOT_FOUND"}, "model")
        self.check(400, {"code": 400, "message": "Request contains an invalid argument.",
                         "status": "INVALID_ARGUMENT"}, "input")

    def test_rate_limit_retried_only_before_audio(self):
        _Gemini.http_errors = [(429, {"code": 429, "message": "Resource has been exhausted (e.g. check quota).",
                                      "status": "RESOURCE_EXHAUSTED"})]
        with mock.patch.object(tts_generic, "LIVE_RETRY_DELAY", 0.01):
            self.assertEqual(self.synth(self.provider()), EXPECTED)
            self.assertEqual(len(self.posts()), 2)
            _Gemini.log.clear()
            _Gemini.error_after_audio = {"code": "rate_limited", "message": "RESOURCE_EXHAUSTED: rate limit"}
            with self.assertRaises(TTSError) as ctx:
                self.synth(self.provider())
        self.assertEqual(ctx.exception.code, "rate")
        self.assertEqual(len(self.posts()), 1, "no retry once audio has played")

    def test_unknown_voice_is_dropped_and_recast(self):
        prov = self.provider()
        first = prov.voice_request(PROFILES["Rok"], "Rok").voice
        _Gemini.http_errors = [(400, {"code": 400, "message": f"Voice {first} not found", "status": "INVALID_ARGUMENT"})]
        with self.assertRaises(TTSError) as ctx:
            self.synth(prov)
        self.assertEqual(ctx.exception.code, "voice")
        self.assertIsNone(prov.catalog.get(first))
        self.assertNotEqual(prov.voice_request(PROFILES["Rok"], "Rok").voice, first)

    def test_failed_interaction_raises(self):
        _Gemini.failed_status = True
        with self.assertRaisesRegex(TTSError, "synthesis failed"):
            self.synth(self.provider())


class VoiceTests(GeminiCase):
    def test_library_is_paged_filtered_labelled_and_cached(self):
        prov = self.provider()
        prov.load_remote_voices()
        gets = [e for e in _Gemini.log if e["method"] == "GET"]
        self.assertEqual(len(gets), 2, "both pages fetched")
        self.assertTrue(all("key" not in urllib.parse.urlparse(g["path"]).query for g in gets))
        self.assertTrue(all(g["key"] == KEY for g in gets))
        ids = {v.id for v in prov.catalog.voices}
        self.assertIn("voices/aurelian", ids)
        self.assertIn("voice_custom1", ids, "your own designed voices are cast from")
        self.assertNotIn("voices/amelie", ids, "other languages are left out")
        self.assertIn("Puck", ids, "the fallback set fills in what the list didn't mention")
        self.assertEqual(prov.catalog.get("Zephyr").gender, "male", "Google's labels beat the fallback set")
        aurelian = prov.catalog.get("voices/aurelian")
        self.assertEqual((aurelian.gender, aurelian.accent), ("male", "british"))
        self.assertIn("low", aurelian.words())
        self.assertEqual(prov.catalog.get("voice_custom1").category, "custom")
        again = self.provider()
        self.assertIn("voice_custom1", {v.id for v in again.catalog.voices}, "a later run starts from the cache")
        self.assertEqual(len([e for e in _Gemini.log if e["method"] == "GET"]), 2)

    def test_casting_from_the_library(self):
        prov = self.provider()
        prov.load_remote_voices()
        rok = prov.catalog.get(cast(prov.catalog, PROFILES["Rok"], "Rok"))
        self.assertEqual(rok.gender, "male")
        self.assertTrue({"low", "deep", "gravelly"} & rok.words(), rok)
        kupipi = prov.catalog.get(cast(prov.catalog, PROFILES["Kupipi"], "Kupipi"))
        self.assertEqual(kupipi.gender, "female")


class EngineTests(GeminiCase):
    def test_engine_switches_streams_caches_and_counts_tokens(self):
        eng = self.make_engine(FakeTTS(), cfg=self.cfg())
        eng.handle({"t": "config", "provider": "gemini"})
        self.assertEqual(eng.tts.name, "gemini")
        job = eng.speak(protocol.parse_speak(speak_msg("Hm.", name="Rok", race=8)))
        self.assertTrue(eng.wait_idle())
        self.assertEqual(job.source, "tts", job.error)
        today = eng.usage.today()
        self.assertEqual((today["p:gemini:output_tokens"], today["p:gemini:input_tokens"]), (75, 12))
        self.assertIn("75 audio tokens", eng.usage.summary())
        again = eng.speak(protocol.parse_speak(speak_msg("Hm.", name="Rok", race=8, seq=2)))
        self.assertTrue(eng.wait_idle())
        self.assertEqual(again.source, "cache")
        self.assertEqual(len(self.posts()), 1)

    def test_missing_key_and_listing(self):
        eng = self.make_engine(FakeTTS(), cfg=self.cfg(api_key=None, api_key_env="KOTODAMA_TEST_NO_GEMINI_KEY"))
        eng.handle({"t": "cmd", "name": "providers_info"})
        self.assertTrue(any("gemini: Gemini 3.8 Flash TTS (Google API, streaming) (no API key)" in m
                            for m in self.say_lines()))
        eng.handle({"t": "config", "provider": "gemini"})
        self.assertEqual(eng.tts.name, "openai")
        self.assertTrue(any("gemini needs an API key (KOTODAMA_TEST_NO_GEMINI_KEY)" in m for m in self.say_lines()))

    def test_doctor(self):
        from kotodama import cli
        results: list = []
        cfg = self.cfg(models={"gemini": {"input": {"voice_setting": {"emotion": "{emotion}"}}}})
        with contextlib.redirect_stdout(io.StringIO()):
            cli._doctor_models(cfg, lambda *r: results.append(r), "gemini")
        self.assertIn(("Gemini key", True, "3 voices in your languages, 1 of your own"), results)
        bad = [r for r in results if r[0] == "gemini"]
        self.assertTrue(bad and not bad[0][1] and "voice_setting" in bad[0][2], results)


if __name__ == "__main__":
    unittest.main()
