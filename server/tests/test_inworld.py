"""Inworld TTS-2 through its OpenAI-compatible endpoint (inworld / inworld-flash)."""

from __future__ import annotations

import base64
import contextlib
import http.server
import io
import json
import re
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
from kotodama.delivery import steering_text
from kotodama.tts_base import TTSError
from kotodama.tts_generic import GenericTTS

from helpers import FakeTTS, KotodamaTestCase, speak_msg

KEY = "aW53b3JsZC10ZXN0LWtleQ=="          # the portal hands out a Base64 credential
AUDIO = array("h", [300 + i % 50 for i in range(2400)]).tobytes()

PROFILES = {
    "Rok": {"gender": "male", "age": "adult", "species": "Galka", "pitch": "very low",
            "tone": "deep, gravelly and rumbling", "personality": "stoic", "pace": "slow"},
    "Kupipi": {"gender": "female", "age": "young", "species": "Tarutaru", "pitch": "very high",
               "tone": "bright and bubbly", "personality": "excitable", "pace": "brisk"},
    "Plain": {"gender": "female", "pace": "normal"},
}

PAGE1 = [
    {"voiceId": "Hades", "displayName": "Hades", "languageCode": "en-US", "source": "SYSTEM", "gender": "male",
     "ageGroup": "elderly", "tags": ["deep", "gravelly"], "description": "A commanding, rumbling voice"},
    {"voiceId": "Wendy", "displayName": "Wendy", "languageCode": "en-GB", "source": "SYSTEM", "gender": "female",
     "ageGroup": "middle_aged", "description": "Posh"},                # disagrees with the fallback set on purpose
    {"voiceId": "ws1__grumbler", "displayName": "Grumbler", "languageCode": "ja-JP", "source": "IVC",
     "gender": "male", "description": "my own gruff clone"},
]
PAGE2 = [
    {"voiceId": "Reginald", "displayName": "Reginald", "langCode": "EN_GB", "source": "SYSTEM", "gender": "male",
     "ageGroup": "middle_aged", "tags": ["refined"]},
    {"voiceId": "Amelie", "displayName": "Amelie", "languageCode": "fr-FR", "source": "SYSTEM", "gender": "female"},
    {"voiceId": "Kiki", "displayName": "Kiki", "languageCode": "en-US", "source": "SYSTEM", "gender": "female",
     "ageGroup": "young", "tags": ["bubbly", "high"], "description": "a bright, bubbly young voice"},
]


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, *a):
        pass


class _Inworld(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    log: list = []
    http_errors: list = []    # (status, OpenAI-shaped error object) for successive requests
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
        type(self).log.append({"method": self.command, "path": self.path,
                               "auth": self.headers.get("Authorization"), "body": body})

    def do_GET(self):
        self._record()
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        if url.path != "/voices/v1/voices":
            return self._json(404, {"error": {"message": "not found", "type": "invalid_request_error"}})
        if q.get("pageToken") == ["p2"]:
            return self._json(200, {"voices": PAGE2, "totalSize": 6, "nextPageToken": ""})
        self._json(200, {"voices": PAGE1, "totalSize": 6, "nextPageToken": "p2"})

    def do_POST(self):
        cls = type(self)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self._record(body)
        if self.path != "/v1/audio/speech":
            return self._json(404, {"error": {"message": "no such route", "type": "invalid_request_error"}})
        if cls.http_errors:
            status, err = cls.http_errors.pop(0)
            return self._json(status, {"error": err})
        self.send_response(200)
        self.send_header("Content-Type", "audio/pcm")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for n, i in enumerate(range(0, len(AUDIO), 777)):          # odd-sized chunks
            part = AUDIO[i:i + 777]
            self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
            self.wfile.flush()
            if n == 0 and cls.gate is not None:
                cls.gate.wait(3)
        self.wfile.write(b"0\r\n\r\n")


class InworldCase(KotodamaTestCase):
    def setUp(self):
        super().setUp()
        _Inworld.log, _Inworld.http_errors, _Inworld.gate = [], [], None
        self.srv = _Server(("127.0.0.1", 0), _Inworld)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def cfg(self, models: dict | None = None, **inworld) -> Config:
        iw = {"base_url": f"http://127.0.0.1:{self.srv.server_port}", "api_key": KEY}
        iw.update(inworld)
        return self.make_config(inworld=iw, models=models or {})

    def provider(self, name: str = "inworld", cfg: Config | None = None) -> GenericTTS:
        cfg = cfg or self.cfg()
        prov = GenericTTS(cfg, load_adapters(cfg)[name])
        self.addCleanup(prov.close)
        return prov

    def synth(self, prov: GenericTTS, text: str = "Hm.", who: str = "Rok") -> bytes:
        return b"".join(prov.stream(text, prov.voice_request(PROFILES[who], who)))

    def posts(self) -> list[dict]:
        return [e for e in _Inworld.log if e["method"] == "POST"]

    def gets(self) -> list[dict]:
        return [e for e in _Inworld.log if e["method"] == "GET"]


class SteeringTests(unittest.TestCase):
    def test_plain_lower_case_words_without_traits(self):
        self.assertEqual(steering_text(PROFILES["Rok"]),
                         "speak in a deep gravelly and rumbling tone with a stoic manner at a slow pace")
        text = steering_text({**PROFILES["Kupipi"], "direction": "Gasps between words!"})
        self.assertRegex(text, r"^[a-z0-9' -]+$")
        self.assertTrue(text.endswith("and gasps between words"), text)
        self.assertIsNone(steering_text(PROFILES["Plain"]))
        self.assertLessEqual(len(steering_text({"tone": "very " * 80})), 200)


class RequestTests(InworldCase):
    def test_request_shape_and_audio(self):
        prov = self.provider()
        self.assertTrue(prov.needs_key)
        self.assertEqual(self.synth(prov), AUDIO)
        (post,) = self.posts()
        self.assertEqual(post["path"], "/v1/audio/speech", "nothing (least of all the key) in the URL")
        self.assertEqual(post["auth"], f"Basic {KEY}")
        body = post["body"]
        self.assertEqual((body["model"], body["input"], body["response_format"]), ("inworld-tts-2", "Hm.", "pcm"))
        self.assertIn(body["voice"], {v.id for v in prov.catalog.voices})
        self.assertEqual(body["speed"], 0.88)
        self.assertNotIn("stream_format", body)
        steer = body["instructions"]
        self.assertRegex(steer, r"^[a-z0-9' -]+$", "Inworld steers best from plain lower-case words")
        self.assertIn("gravelly", steer)
        self.assertIn("slow", steer)
        for trait in ("male", "galka", "adult"):
            self.assertNotIn(trait, steer, "immutable traits come from the voice, not the steering")

    def test_speed_is_kept_in_range(self):
        cfg = self.cfg(models={"inworld": {"controls": {"speed": {"slow": 0.3}}}})
        self.synth(self.provider(cfg=cfg))
        self.assertEqual(self.posts()[0]["body"]["speed"], 0.5, "Inworld refuses speeds outside 0.5-1.5")

    def test_brackets_in_game_text_never_steer(self):
        self.synth(self.provider(), "Bring me a [Mithkabob], quickly.")
        self.assertEqual(self.posts()[0]["body"]["input"], "Bring me a (Mithkabob), quickly.")

    def test_flash_and_plain_profiles_send_no_instructions(self):
        self.synth(self.provider("inworld-flash"))
        self.synth(self.provider(), who="Plain")
        flash, plain = (p["body"] for p in self.posts())
        self.assertEqual(flash["model"], "inworld-tts-2-flash")
        self.assertNotIn("instructions", flash, "Flash ignores steering")
        self.assertNotIn("instructions", plain)

    def test_a_raw_key_and_secret_is_encoded(self):
        self.synth(self.provider(cfg=self.cfg(api_key="my-key:my-secret")))
        self.assertEqual(self.posts()[0]["auth"], "Basic " + base64.b64encode(b"my-key:my-secret").decode())

    def test_audio_is_yielded_while_the_stream_is_open(self):
        _Inworld.gate = threading.Event()
        prov = self.provider()
        gen = prov.stream("Streaming.", prov.voice_request(PROFILES["Rok"], "Rok"))
        started = time.monotonic()
        first = next(gen)
        waited = time.monotonic() - started
        _Inworld.gate.set()
        self.assertLess(waited, 2.0, "audio arrived before the server finished the response")
        self.assertEqual(first + b"".join(gen), AUDIO)

    def test_long_text_is_split_under_the_limit(self):
        text = " ".join(["This sentence is here to make the line long enough to need two requests."] * 60)
        self.synth(self.provider(), text)
        inputs = [p["body"]["input"] for p in self.posts()]
        self.assertGreater(len(inputs), 1)
        self.assertTrue(all(len(i) <= 4000 for i in inputs))
        self.assertEqual(" ".join(inputs), text)


class ErrorTests(InworldCase):
    def check(self, status: int, err: dict, code: str | None) -> TTSError:
        _Inworld.http_errors = [(status, err)]
        with self.assertRaises(TTSError) as ctx:
            self.synth(self.provider())
        self.assertEqual(ctx.exception.code, code, ctx.exception)
        return ctx.exception

    def test_http_errors(self):
        self.check(401, {"message": "Invalid API key", "type": "authentication_error"}, "auth")
        exc = self.check(403, {"message": "Invalid credentials", "type": "permission_error"}, "auth")
        self.assertIn("Base64", str(exc))
        self.check(402, {"message": "No credits remain", "type": "insufficient_quota"}, "quota")
        exc = self.check(400, {"message": "must be between 0.5 and 1.5", "type": "invalid_request_error",
                               "param": "speed"}, "input")
        self.assertIn("speed", str(exc))
        self.check(500, {"message": "boom", "type": "api_error"}, None)

    def test_rate_limit_is_retried_once(self):
        limit = (429, {"message": "Rate limit exceeded", "type": "rate_limit_error"})
        with mock.patch.object(tts_generic, "LIVE_RETRY_DELAY", 0.01):
            _Inworld.http_errors = [limit]
            self.assertEqual(self.synth(self.provider()), AUDIO)
            self.assertEqual(len(self.posts()), 2)
            _Inworld.log.clear()
            _Inworld.http_errors = [limit, limit]
            with self.assertRaises(TTSError) as ctx:
                self.synth(self.provider())
        self.assertEqual(ctx.exception.code, "rate")
        self.assertEqual(len(self.posts()), 2, "one retry, not more")

    def test_unknown_voice_is_dropped_and_recast(self):
        prov = self.provider()
        first = prov.voice_request(PROFILES["Rok"], "Rok").voice
        _Inworld.http_errors = [(404, {"message": f"Voice '{first}' not found", "type": "invalid_request_error"})]
        with self.assertRaises(TTSError) as ctx:
            self.synth(prov)
        self.assertEqual(ctx.exception.code, "voice")
        self.assertIsNone(prov.catalog.get(first))
        self.assertNotEqual(prov.voice_request(PROFILES["Rok"], "Rok").voice, first)


class VoiceTests(InworldCase):
    def test_library_is_paged_filtered_labelled_and_cached(self):
        prov = self.provider()
        prov.load_remote_voices()
        gets = self.gets()
        self.assertEqual(len(gets), 2, "both pages fetched")
        self.assertTrue(all(g["auth"] == f"Basic {KEY}" for g in gets))
        for g in gets:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(g["path"]).query)
            self.assertLessEqual(set(query), {"pageSize", "pageToken"}, "the key never goes in the URL")
        ids = {v.id for v in prov.catalog.voices}
        self.assertIn("ws1__grumbler", ids, "your own cloned voices are cast from, in any language")
        self.assertNotIn("Amelie", ids, "other languages are left out")
        self.assertIn("Pixie", ids, "the fallback set fills in what the list didn't mention")
        self.assertEqual(prov.catalog.get("Wendy").age, "middle_aged", "Inworld's labels beat the fallback set")
        hades = prov.catalog.get("Hades")
        self.assertEqual((hades.gender, hades.age), ("male", "old"))
        self.assertIn("gravelly", hades.words())
        self.assertEqual(prov.catalog.get("Reginald").accent, "british")
        self.assertEqual(prov.catalog.get("ws1__grumbler").category, "custom")
        again = self.provider()
        self.assertIn("ws1__grumbler", {v.id for v in again.catalog.voices}, "a later run starts from the cache")
        self.assertEqual(len(self.gets()), 2)

    def test_casting_from_the_library(self):
        prov = self.provider()
        prov.load_remote_voices()
        rok = prov.catalog.get(cast(prov.catalog, PROFILES["Rok"], "Rok"))
        self.assertEqual(rok.gender, "male")
        self.assertTrue({"low", "deep", "gravelly"} & rok.words(), rok)
        kupipi = prov.catalog.get(cast(prov.catalog, PROFILES["Kupipi"], "Kupipi"))
        self.assertEqual(kupipi.gender, "female")
        self.assertTrue(kupipi.age == "young" or {"high", "bright", "bubbly"} & kupipi.words(), kupipi)


class EngineTests(InworldCase):
    def test_engine_switches_streams_caches_and_estimates_cost(self):
        eng = self.make_engine(FakeTTS(), cfg=self.cfg())
        eng.handle({"t": "config", "provider": "inworld"})
        self.assertEqual(eng.tts.name, "inworld")
        job = eng.speak(protocol.parse_speak(speak_msg("Hm.", name="Rok", race=8)))
        self.assertTrue(eng.wait_idle())
        self.assertEqual(job.source, "tts", job.error)
        today = eng.usage.today()
        self.assertAlmostEqual(today["p:inworld:cost"], 0.025 * 3 / 1000)
        self.assertIn("inworld 3 chars", eng.usage.summary())
        again = eng.speak(protocol.parse_speak(speak_msg("Hm.", name="Rok", race=8, seq=2)))
        self.assertTrue(eng.wait_idle())
        self.assertEqual(again.source, "cache")
        self.assertEqual(len(self.posts()), 1)

    def test_missing_key_and_listing(self):
        eng = self.make_engine(FakeTTS(), cfg=self.cfg(api_key=None, api_key_env="KOTODAMA_TEST_NO_INWORLD_KEY"))
        eng.handle({"t": "cmd", "name": "providers_info"})
        self.assertTrue(any("inworld: Inworld TTS-2 (Inworld API, streaming) (no API key)" in m
                            for m in self.say_lines()))
        eng.handle({"t": "config", "provider": "inworld"})
        self.assertEqual(eng.tts.name, "openai")
        self.assertTrue(any("inworld needs an API key (KOTODAMA_TEST_NO_INWORLD_KEY)" in m for m in self.say_lines()))

    def test_doctor(self):
        from kotodama import cli
        results: list = []
        cfg = self.cfg(models={"inworld": {"extra": {"temperature": 0.7}}})
        with contextlib.redirect_stdout(io.StringIO()):
            cli._doctor_models(cfg, False, lambda *r: results.append(r), "inworld")
        self.assertIn(("Inworld key", True, "4 voices in your languages, 1 of your own"), results)
        bad = [r for r in results if r[0] == "inworld"]
        self.assertTrue(bad and not bad[0][1] and "temperature" in bad[0][2], results)
        self.assertTrue(all(KEY.rstrip("=") not in g["path"] for g in self.gets()))


if __name__ == "__main__":
    unittest.main()
