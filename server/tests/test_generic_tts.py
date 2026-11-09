"""Adapter-driven providers: Replicate models, OpenAI-compatible servers,
audio decoding, the voice bank, and live switching from the engine."""

from __future__ import annotations

import http.server
import json
import struct
import threading
import time
import unittest
import urllib.parse
from array import array
from unittest import mock

from kotodama import tts_generic
from kotodama.adapters import (AdapterError, build_adapter, check_fields, draft_adapter, input_properties,
                               load_adapters, merged_specs, overlay, render)
from kotodama.casting import VoiceCatalog, cast, derive_labels
from kotodama.config import Config
from kotodama.decode import DecodeError, PCMDecoder, decode_all
from kotodama.tts_base import TTSError
from kotodama.tts_generic import GenericTTS, split_text
from kotodama.voice_bank import VoiceBank

from helpers import FakeTTS, KotodamaTestCase, speak_msg

TOKEN = "r8_test_token"


def pcm_of(values: list[int]) -> bytes:
    return array("h", values).tobytes()


def make_wav(samples: list[int], rate: int = 24000, channels: int = 1, width: int = 2,
             streaming: bool = False, extra_chunk: bool = False, fmt_tag: int = 1) -> bytes:
    if width == 2:
        data = array("h", samples).tobytes()
    elif width == 4 and fmt_tag == 3:
        data = struct.pack(f"<{len(samples)}f", *[s / 32767 for s in samples])
    elif width == 3:
        data = b"".join(int(s * 256).to_bytes(3, "little", signed=True) for s in samples)
    else:
        raise ValueError(width)
    fmt = struct.pack("<HHIIHH", fmt_tag, channels, rate, rate * channels * width, channels * width, width * 8)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    if extra_chunk:
        info = b"INFOtest"
        chunks += b"LIST" + struct.pack("<I", len(info)) + info
    size = 0xFFFFFFFF if streaming else len(data)
    chunks += b"data" + struct.pack("<I", size) + data
    return b"RIFF" + struct.pack("<I", 0xFFFFFFFF if streaming else 4 + len(chunks)) + b"WAVE" + chunks


def samples_of(pcm: bytes) -> list[int]:
    a = array("h")
    a.frombytes(pcm)
    return list(a)


def code_for(text: str) -> int:
    """The constant sample value the fake file server uses for a text."""
    return 100 + (ord(text[0]) % 64) * 10


# -- fake servers ---------------------------------------------------------------

class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, *a):
        pass


SCHEMAS = {
    "minimax/speech-2.8-turbo": {
        "properties": {
            "text": {"type": "string"}, "voice_id": {"type": "string"},
            "speed": {"type": "number", "minimum": 0.5, "maximum": 2},
            "pitch": {"type": "integer", "minimum": -12, "maximum": 12},
            "emotion": {"type": "string", "enum": ["auto", "happy", "sad"]},
            "audio_format": {"type": "string", "enum": ["mp3", "wav", "flac", "pcm"]},
            "sample_rate": {"type": "integer"}, "channel": {"type": "string"},
        },
        "required": ["text"], "components": {},
    },
    "resemble-ai/chatterbox-turbo": {
        "properties": {
            "text": {"type": "string", "maxLength": 500},
            "voice": {"allOf": [{"$ref": "#/components/schemas/voice"}]},
            "reference_audio": {"type": "string", "format": "uri"},
            "seed": {"type": "integer"},
            "temperature": {"type": "number", "minimum": 0.05, "maximum": 2.0},
        },
        "required": ["text"],
        "components": {"voice": {"type": "string", "enum": ["Abigail", "Marcus", "Olivia"]}},
    },
    "jaaari/kokoro-82m": {
        "properties": {"text": {"type": "string"}, "voice": {"type": "string"},
                       "speed": {"type": "number", "minimum": 0.1, "maximum": 5}},
        "required": ["text"], "components": {},
    },
    "acme/tts": {
        "properties": {"prompt": {"type": "string"}, "speed": {"type": "number", "minimum": 0.8, "maximum": 1.2}},
        "required": ["prompt"], "components": {},
    },
}


def model_json(ref: str) -> dict:
    s = SCHEMAS[ref]
    components = dict(s["components"])
    components["Input"] = {"type": "object", "properties": s["properties"], "required": s["required"]}
    return {"owner": ref.split("/")[0], "name": ref.split("/")[1],
            "latest_version": {"id": "ver-" + ref.replace("/", "-"),
                               "openapi_schema": {"components": {"schemas": components}}}}


class _API(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    log: list = []
    texts: dict = {}
    file_base = ""
    mode = "sync"            # sync | async | fail
    polls_needed = 2
    queue_status: list = []  # statuses to send for creates before succeeding (e.g. [429], [422])
    slow_text = ""           # predictions whose text starts with this take `slow_s`
    slow_s = 0.0
    counter = 0
    lock = threading.Lock()

    def log_message(self, *a):
        pass

    def _json(self, status: int, payload) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _record(self, body=None) -> None:
        type(self).log.append({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization"),
                               "prefer": self.headers.get("Prefer"), "body": body})

    def do_GET(self):
        cls = type(self)
        self._record()
        path = urllib.parse.urlparse(self.path).path
        if path == "/v1/account":
            return self._json(200, {"type": "user", "username": "tester"})
        if path.startswith("/v1/models/"):
            ref = path[len("/v1/models/"):]
            return self._json(200, model_json(ref)) if ref in SCHEMAS else self._json(404, {"detail": "Not found"})
        if path.startswith("/v1/predictions/"):
            pid = path.rsplit("/", 1)[-1]
            with cls.lock:
                entry = cls.texts[pid]
                entry["polls"] += 1
                done = entry["polls"] >= cls.polls_needed
            if cls.mode == "fail" and done:
                return self._json(200, {"id": pid, "status": "failed", "error": "CUDA out of memory"})
            if done:
                return self._json(200, {"id": pid, "status": "succeeded", "output": entry["url"],
                                        "metrics": {"predict_time": 0.5}})
            return self._json(200, {"id": pid, "status": "starting" if entry["polls"] == 1 else "processing"})
        if path.startswith("/files/"):          # a file on the API host itself
            return _serve_file(self, path)
        self._json(404, {"detail": "Not found"})

    def do_POST(self):
        cls = type(self)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else None
        self._record(body)
        path = urllib.parse.urlparse(self.path).path
        if path.endswith("/cancel"):
            return self._json(200, {"status": "canceled"})
        if cls.queue_status:
            status = cls.queue_status.pop(0)
            if status == 422:
                return self._json(422, {"detail": "Invalid input", "status": 422,
                                        "invalid_fields": [{"field": "input.voice_id", "description": "bad voice"}]})
            return self._json(status, {"detail": f"status {status}"})
        inp = body.get("input", {})
        text = next((v for k, v in inp.items() if k in ("text", "prompt") and isinstance(v, str)), "?")
        with cls.lock:
            cls.counter += 1
            pid = f"p{cls.counter}"
            base = cls.file_base if not text.startswith("ONAPI") else "API"
            cls.texts[pid] = {"text": text, "polls": 0, "url": None}
        url = (f"http://127.0.0.1:{self.server.server_port}/files/{pid}.wav" if base == "API"
               else f"{cls.file_base}/audio/{pid}.wav")
        cls.texts[pid]["url"] = url
        if cls.slow_text and text.startswith(cls.slow_text):
            time.sleep(cls.slow_s)
        if cls.mode in ("async", "fail"):
            return self._json(201, {"id": pid, "status": "starting", "urls": {"get": f"https://evil.example/{pid}"}})
        self._json(201, {"id": pid, "status": "succeeded", "output": url, "metrics": {"predict_time": 0.25}})


def _serve_file(handler: http.server.BaseHTTPRequestHandler, path: str) -> None:
    _Files.log.append({"path": path, "auth": handler.headers.get("Authorization")})
    pid = path.rsplit("/", 1)[-1].split(".")[0]
    entry = _API.texts.get(pid)
    if entry is None:
        handler.send_response(404)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return
    text = entry["text"]
    count = max(200 + len(text), int(_Files.min_seconds * _Files.rate))
    data = make_wav([code_for(text)] * count * _Files.channels, rate=_Files.rate, channels=_Files.channels)
    handler.send_response(200)
    handler.send_header("Content-Type", "audio/wav")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class _Files(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    log: list = []
    rate = 24000
    channels = 1
    min_seconds = 0.0

    def log_message(self, *a):
        pass

    def do_GET(self):
        _serve_file(self, urllib.parse.urlparse(self.path).path)


class _Compat(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    log: list = []
    reply = "pcm"            # pcm | wav

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).log.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        payload = (make_wav([7] * 1500, streaming=True) if type(self).reply == "wav" else pcm_of([7] * 1500))
        self.send_response(200)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for i in range(0, len(payload), 333):            # odd-sized chunks
            part = payload[i:i + 333]
            self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")


def _start(handler) -> _Server:
    srv = _Server(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return srv


class GenericCase(KotodamaTestCase):
    def setUp(self):
        super().setUp()
        _API.log, _API.texts, _API.mode, _API.queue_status = [], {}, "sync", []
        _API.slow_text, _API.slow_s, _API.counter, _API.polls_needed = "", 0.0, 0, 2
        _Files.log, _Files.rate, _Files.channels, _Files.min_seconds = [], 24000, 1, 0.0
        _Compat.log, _Compat.reply = [], "pcm"
        self.api = _start(_API)
        self.files = _start(_Files)
        self.compat = _start(_Compat)
        for srv in (self.api, self.files, self.compat):
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
        # A different host name than the API (127.0.0.1): the token must not go there.
        _API.file_base = f"http://localhost:{self.files.server_port}"
        self.notes: list[str] = []

    def cfg(self, models: dict | None = None, **replicate) -> Config:
        rep = {"base_url": f"http://127.0.0.1:{self.api.server_port}", "api_key": TOKEN, "wait_seconds": 5,
               "timeout": 10}
        rep.update(replicate)
        return self.make_config(replicate=rep, models=models or {},
                                voice_bank={"dir": str(self.tmp / "voices")})

    def provider(self, name: str, cfg: Config | None = None, bank: VoiceBank | None = None) -> GenericTTS:
        cfg = cfg or self.cfg()
        prov = GenericTTS(cfg, load_adapters(cfg)[name], bank=bank, notify=self.notes.append)
        self.addCleanup(prov.close)
        return prov

    def creates(self) -> list[dict]:
        return [e for e in _API.log if e["method"] == "POST" and not e["path"].endswith("/cancel")]

    def synth(self, prov: GenericTTS, text: str, profile: dict | None = None, ident: str = "Rok") -> bytes:
        req = prov.voice_request(profile or {"gender": "male", "age": "elderly", "pitch": "very low",
                                             "tone": "gruff", "pace": "slow"}, ident)
        return b"".join(prov.stream(text, req))


# -- decoding -------------------------------------------------------------------

class DecodeTests(unittest.TestCase):
    def test_wav_passthrough_in_any_chunking(self):
        samples = list(range(-500, 500, 3))
        wav = make_wav(samples, extra_chunk=True)
        self.assertEqual(samples_of(decode_all(wav, 24000)), samples)
        dec = PCMDecoder(24000)
        out = b"".join(dec.feed(wav[i:i + 1]) for i in range(len(wav))) + dec.finish()
        self.assertEqual(samples_of(out), samples, "byte-at-a-time feeding gives the same audio")

    def test_streaming_header_sizes_mean_until_the_end(self):
        samples = [1000, -1000] * 300
        self.assertEqual(samples_of(decode_all(make_wav(samples, streaming=True), 24000)), samples)

    def test_stereo_is_downmixed_and_rates_resampled(self):
        stereo = [1000, 3000] * 2400                      # L=1000 R=3000
        out = samples_of(decode_all(make_wav(stereo, channels=2), 24000))
        self.assertEqual(set(out), {2000})
        self.assertEqual(len(out), 2400)
        up = samples_of(decode_all(make_wav([500] * 1600, rate=16000), 24000))
        self.assertAlmostEqual(len(up), 2400, delta=3)
        down = samples_of(decode_all(make_wav([500] * 4800, rate=48000), 24000))
        self.assertAlmostEqual(len(down), 2400, delta=3)
        self.assertEqual(set(down), {500})

    def test_float_and_24_bit(self):
        self.assertEqual(samples_of(decode_all(make_wav([16384, -16384], width=4, fmt_tag=3), 24000)),
                         [16383, -16383])
        self.assertEqual(samples_of(decode_all(make_wav([1200, -1200], width=3), 24000)), [1200, -1200])

    def test_raw_pcm_and_errors(self):
        self.assertEqual(decode_all(pcm_of([5, 6, 7]), 24000, kind="pcm"), pcm_of([5, 6, 7]))
        with self.assertRaises(DecodeError):
            decode_all(b"this is not audio at all", 24000)
        with mock.patch("kotodama.decode.shutil.which", return_value=None):
            with self.assertRaisesRegex(DecodeError, "ffmpeg"):
                decode_all(b"ID3" + b"\x00" * 64, 24000)


# -- adapters ---------------------------------------------------------------------

class AdapterTests(unittest.TestCase):
    def test_presets_are_valid(self):
        problems: list[str] = []
        ads = load_adapters(Config({}), problems)
        self.assertEqual(problems, [])
        for name in ("minimax", "minimax-hd", "minimax-replicate", "minimax-replicate-hd", "chatterbox-turbo",
                     "chatterbox", "kokoro", "qwen3"):
            self.assertIn(name, ads)
        self.assertNotIn("kokoro-local", ads, "the self-hosted example ships disabled")
        self.assertEqual(len(ads["minimax"].voices), 62)
        self.assertEqual(ads["chatterbox-turbo"].voice_sources, ("schema", "bank"))

    def test_render_types_omission_partial_and_clamp(self):
        tpl = {"text": "{text}", "speed": "{speed}", "pitch": "{pitch}", "voice": "{voice}", "fixed": 3,
               "note": "say {style}", "gone": None, "nested": {"seed": "{seed}"}}
        out = render(tpl, {"text": "hi", "speed": 9.0, "pitch": -40, "voice": None, "style": "softly", "seed": 7},
                     {"speed": (0.5, 2.0), "pitch": (-12, 12)})
        self.assertEqual(out, {"text": "hi", "speed": 2.0, "pitch": -12, "fixed": 3, "note": "say softly",
                               "nested": {"seed": 7}})
        self.assertNotIn("note", render({"note": "say {style}"}, {"style": None}), "partial with a gap drops")
        self.assertEqual(overlay({"a": 1, "voice": "{voice}"}, {"voice": None, "ref": "{reference_audio}"}),
                         {"a": 1, "ref": "{reference_audio}"})

    def test_bad_adapters_are_rejected(self):
        ok = {"model": "a/b", "input": {"text": "{text}"}}
        build_adapter("fine", ok, {})
        for name, raw, why in [
            ("openai", ok, "reserved"), ("Bad Name", ok, "name"),
            ("x", {"model": "a/b", "input": {"text": "{txt}"}}, "placeholder"),
            ("x", {"model": "a/b", "input": {"words": "hello"}}, "{text}"),
            ("x", {"model": "ab", "input": {"text": "{text}"}}, "owner/name"),
            ("x", {"transport": "openai_compatible", "model": "k"}, "base_url"),
            ("x", {"model": "a/b", "input": {"text": "{text}"}, "voices": "@nope"}, "voice set"),
        ]:
            with self.subTest(why):
                with self.assertRaises(AdapterError):
                    build_adapter(name, raw, {})

    def test_user_config_overrides_and_adds_models(self):
        cfg = Config({"models": {
            "minimax-replicate": {"input": {"emotion": "{emotion}"}, "label": "Mine"},
            "kokoro-fast": {"preset": "kokoro", "controls": {"speed": {"normal": 1.2}}},
            "qwen3": {"enabled": False},
        }})
        specs = merged_specs(cfg)
        self.assertEqual(specs["minimax-replicate"]["input"]["emotion"], "{emotion}")
        self.assertEqual(specs["minimax-replicate"]["input"]["voice_id"], "{voice}", "other fields kept")
        self.assertEqual(specs["kokoro-fast"]["model"], "jaaari/kokoro-82m")
        self.assertNotIn("qwen3", specs)

    def test_schema_tools(self):
        props, required = input_properties(model_json("resemble-ai/chatterbox-turbo"))
        self.assertEqual(props["voice"]["enum"], ["Abigail", "Marcus", "Olivia"], "$ref/allOf resolved")
        draft = draft_adapter("resemble-ai/chatterbox-turbo", props, required, official=True)
        self.assertEqual(draft["input"]["text"], "{text}")
        self.assertEqual(draft["input"]["voice"], "{voice}")
        self.assertEqual(draft["input"]["seed"], "{seed}")
        self.assertEqual(draft["input_clone"], {"reference_audio": "{reference_audio}", "voice": None})
        self.assertEqual(draft["voices"], ["schema", "bank"])
        self.assertEqual(draft["max_chars"], 500)
        build_adapter("drafted", draft, {})                      # a draft is a valid adapter

        ads = load_adapters(Config({}))
        mm_props, _ = input_properties(model_json("minimax/speech-2.8-turbo"))
        self.assertEqual(check_fields(ads["minimax-replicate"], mm_props), [])
        renamed = dict(mm_props)
        renamed["voice"] = renamed.pop("voice_id")
        self.assertTrue(any("voice_id" in p for p in check_fields(ads["minimax-replicate"], renamed)))


class LabelAndCastingTests(unittest.TestCase):
    def test_derived_labels(self):
        self.assertNotEqual(derive_labels("English_ImposingManner").get("gender"), "male", "whole words only")
        self.assertEqual(derive_labels("bm_george"), {"gender": "male", "accent": "british"})
        self.assertEqual(derive_labels("English_Wiselady")["gender"], "female")
        self.assertEqual(derive_labels("old_man_gruff")["age"], "old")

    def test_preset_voice_sets_cast_sensibly(self):
        ads = load_adapters(Config({}))
        galka = {"gender": "male", "age": "elderly", "species": "Galka", "pitch": "very low",
                 "tone": "gruff, rumbling", "personality": "stoic"}
        taru_f = {"gender": "female", "age": "young", "species": "Tarutaru", "pitch": "very high",
                  "tone": "bubbly", "personality": "excitable"}
        mm = VoiceCatalog.static(ads["minimax"].voices)
        rok = mm.get(cast(mm, galka, "Rok"))
        self.assertEqual(rok.gender, "male")
        self.assertIn("deep", rok.words())
        kupipi = mm.get(cast(mm, taru_f, "Kupipi"))
        self.assertEqual((kupipi.gender, kupipi.age), ("female", "young"))
        ko = VoiceCatalog.static(ads["kokoro"].voices)
        self.assertTrue(cast(ko, galka, "Rok").startswith(("am_", "bm_")))
        self.assertTrue(cast(ko, taru_f, "Kupipi").startswith(("af_", "bf_")))


class SplitTests(unittest.TestCase):
    def test_first_sentence_alone_then_groups(self):
        text = "Hm. Welcome to Norg, traveler. The tides are strange today! Mind the reefs. Go on."
        chunks = split_text(text, 500)
        self.assertEqual(chunks[0], "Hm. Welcome to Norg, traveler.", "a tiny first sentence joins the next")
        self.assertEqual(" ".join(chunks), text)

    def test_max_chars_is_never_exceeded(self):
        long = ("word " * 150).strip() + "."
        text = f"Short opener here. {long} Another sentence, with a clause, ends here."
        chunks = split_text(text, 120)
        self.assertTrue(all(len(c) <= 120 for c in chunks), [len(c) for c in chunks])
        self.assertEqual(" ".join(chunks).split(), text.split())
        self.assertEqual(split_text("One line only", 500, mode="none"), ["One line only"])


# -- Replicate provider ---------------------------------------------------------------

class ReplicateProviderTests(GenericCase):
    def test_sync_prediction_request_shape_and_audio(self):
        prov = self.provider("minimax-replicate")
        pcm = self.synth(prov, "Hm.")
        (create,) = self.creates()
        self.assertEqual(create["path"], "/v1/models/minimax/speech-2.8-turbo/predictions")
        self.assertEqual(create["auth"], f"Bearer {TOKEN}")
        self.assertEqual(create["prefer"], "wait=5")
        inp = create["body"]["input"]
        self.assertEqual(inp["text"], "Hm.")
        self.assertEqual((inp["audio_format"], inp["sample_rate"], inp["emotion"]), ("wav", 24000, "auto"))
        self.assertEqual((inp["speed"], inp["pitch"]), (0.88, -3), "slow pace, very low pitch")
        self.assertIn(inp["voice_id"], {v.id for v in prov.adapter.voices})
        self.assertEqual(set(samples_of(pcm)), {code_for("Hm.")})

    def test_token_only_goes_to_the_api_host(self):
        prov = self.provider("minimax-replicate")
        self.synth(prov, "Elsewhere.")
        self.synth(prov, "ONAPI file on the api host.")
        self.assertEqual(len(_Files.log), 2)
        by_path = {e["path"]: e["auth"] for e in _Files.log}
        other = [a for p, a in by_path.items() if p.startswith("/audio/")]
        same = [a for p, a in by_path.items() if p.startswith("/files/")]
        self.assertEqual(other, [None], "replicate.delivery-style hosts get no credentials")
        self.assertEqual(same, [f"Bearer {TOKEN}"], "files on the API host itself need them")

    def test_async_prediction_is_polled_by_id_with_cold_boot_notice(self):
        _API.mode = "async"
        _API.polls_needed = 3
        with mock.patch.object(tts_generic, "POLL_EVERY", 0.01), \
                mock.patch.object(tts_generic, "COLD_NOTICE_AFTER", 0.0):
            pcm = self.synth(self.provider("minimax-replicate"), "Slow start.")
        polls = [e for e in _API.log if e["method"] == "GET" and e["path"].startswith("/v1/predictions/")]
        self.assertEqual(len(polls), 3)
        self.assertTrue(all(e["auth"] == f"Bearer {TOKEN}" for e in polls), "polled on our API host, by id")
        self.assertEqual(set(samples_of(pcm)), {code_for("Slow start.")})
        self.assertTrue(any("cold boot" in n for n in self.notes))

    def test_failed_and_refused_predictions(self):
        _API.mode, _API.polls_needed = "fail", 1
        with mock.patch.object(tts_generic, "POLL_EVERY", 0.01):
            with self.assertRaisesRegex(TTSError, "CUDA out of memory"):
                self.synth(self.provider("minimax-replicate"), "Boom.")
        _API.mode = "sync"
        _API.queue_status = [422]
        with self.assertRaisesRegex(TTSError, "input.voice_id: bad voice") as ctx:
            self.synth(self.provider("minimax-replicate"), "Bad.")
        self.assertEqual(ctx.exception.code, "input")
        _API.queue_status = [401]
        with self.assertRaises(TTSError) as ctx:
            self.synth(self.provider("minimax-replicate"), "Who.")
        self.assertEqual(ctx.exception.code, "auth")

    def test_rate_limit_is_retried_once(self):
        _API.queue_status = [429]
        with mock.patch.object(tts_generic, "RETRY_DELAY", 0.01):
            pcm = self.synth(self.provider("minimax-replicate"), "Again.")
        self.assertEqual(len(self.creates()), 2)
        self.assertTrue(pcm)

    def test_timeout_cancels_the_prediction(self):
        _API.mode, _API.polls_needed = "async", 10_000
        prov = self.provider("minimax-replicate", self.cfg(timeout=0.3))
        with mock.patch.object(tts_generic, "POLL_EVERY", 0.02):
            with self.assertRaises(TTSError) as ctx:
                self.synth(prov, "Never.")
        self.assertEqual(ctx.exception.code, "timeout")
        self.assertTrue(any(e["path"].endswith("/cancel") for e in _API.log))

    def test_community_model_resolves_its_version(self):
        prov = self.provider("kokoro")
        self.synth(prov, "Hello there.")
        (create,) = self.creates()
        self.assertEqual(create["path"], "/v1/predictions")
        self.assertEqual(create["body"]["version"], "ver-jaaari-kokoro-82m")
        self.synth(prov, "Hello again.")
        model_gets = [e for e in _API.log if e["path"] == "/v1/models/jaaari/kokoro-82m"]
        self.assertEqual(len(model_gets), 1, "the version is looked up once")

    def test_sentences_first_then_parallel_in_order(self):
        _API.slow_text, _API.slow_s = "Alpha", 0.3       # the first sentence finishes last
        prov = self.provider("minimax-replicate")
        text = "Alpha is the first sentence here. Bravo comes second in line."
        pcm = self.synth(prov, text)
        texts = [c["body"]["input"]["text"] for c in self.creates()]
        self.assertEqual(texts, ["Alpha is the first sentence here.", "Bravo comes second in line."])
        values = samples_of(pcm)
        a, b = code_for("Alpha"), code_for("Bravo")
        first_b = values.index(b)
        self.assertEqual(set(values[:first_b]), {a}, "audio comes out in sentence order")
        self.assertEqual(set(values[first_b:]), {b})

    def test_max_chars_limit_for_chatterbox_turbo(self):
        prov = self.provider("chatterbox-turbo")
        sentence = "This sentence is long enough, and it keeps going with more words, " * 12
        text = f"Opening line first. {sentence.strip()}. Closing line."
        self.synth(prov, text)
        sent = [c["body"]["input"]["text"] for c in self.creates()]
        self.assertGreater(len(sent), 2)
        self.assertTrue(all(len(s) <= 500 for s in sent), [len(s) for s in sent])
        self.assertEqual(" ".join(sent).split(), text.split())

    def test_resampled_and_downmixed_files(self):
        _Files.rate, _Files.channels = 16000, 2
        pcm = self.synth(self.provider("minimax-replicate"), "Converted.")
        self.assertEqual(set(samples_of(pcm)), {code_for("Converted.")})

    def test_schema_voices_and_ranges(self):
        cfg = self.cfg(models={"acme": {"model": "acme/tts", "input": {"prompt": "{text}", "speed": "{speed}"},
                                        "controls": {"speed": {"slow": 0.5}}}})
        prov = self.provider("acme", cfg)
        prov.ensure_catalog(wait=True)
        self.assertTrue(wait_for(lambda: prov._remote_state == "loaded"))
        self.synth(prov, "Clamp me.", {"pace": "slow"})
        self.assertEqual(self.creates()[-1]["body"]["input"]["speed"], 0.8, "clamped to the schema minimum")

        turbo = self.provider("chatterbox-turbo")
        turbo.ensure_catalog(wait=True)
        self.assertEqual({v.id for v in turbo.catalog.voices}, {"Abigail", "Marcus", "Olivia"})

    def test_cache_tag_tracks_voice_and_settings(self):
        prov = self.provider("minimax-replicate")
        base = {"gender": "male", "pitch": "low", "pace": "normal"}
        r1 = prov.voice_request(base, "Rok")
        r2 = prov.voice_request({**base, "pace": "fast"}, "Rok")
        r3 = prov.voice_request({**base, "voice": "English_Jovialman"}, "Rok")
        self.assertNotEqual(r1.cache_tag, r2.cache_tag)
        self.assertEqual(r3.voice, "English_Jovialman", "an explicit voice in the catalog wins")
        self.assertNotIn("\x00TEXT\x00x", r1.cache_tag)


class VoiceBankTests(GenericCase):
    def write_clip(self, name: str, seconds: float, meta: dict | None = None, value: int = 300) -> None:
        d = self.tmp / "voices"
        d.mkdir(exist_ok=True)
        (d / f"{name}.wav").write_bytes(make_wav([value] * int(seconds * 22050), rate=22050))
        if meta is not None:
            (d / f"{name}.json").write_text(json.dumps(meta), encoding="utf-8")

    def test_clips_are_normalised_labelled_and_validated(self):
        self.write_clip("old_man_gruff", 6.0)
        self.write_clip("narrator", 10.0, {"name": "Storyteller", "gender": "female", "age": "old",
                                           "reference_text": "Once upon a time."})
        self.write_clip("too_short", 3.0)
        bank = VoiceBank(self.tmp / "voices")
        self.assertTrue(bank.refresh())
        self.assertEqual(set(bank.clips), {"bank:old_man_gruff", "bank:narrator"})
        self.assertTrue(any("too_short" in p for p in bank.problems))
        gruff = bank.get("bank:old_man_gruff")
        self.assertEqual((gruff.voice.gender, gruff.voice.age), ("male", "old"))
        story = bank.get("bank:narrator")
        self.assertEqual(story.seconds, 7.5)
        self.assertIsNone(story.reference_text, "a trimmed clip no longer matches its transcript")
        self.assertLess(len(story.data_uri), 330_000)
        self.assertFalse(bank.refresh(), "unchanged folder: no reload")

    def test_clone_overlay_and_cache_tag_use_the_clip_hash(self):
        self.write_clip("old_man_gruff", 6.0)
        bank = VoiceBank(self.tmp / "voices")
        prov = self.provider("chatterbox-turbo", bank=bank)
        prof = {"gender": "male", "age": "elderly", "tone": "gruff"}
        req = prov.voice_request(prof, "Rok")
        self.assertEqual(req.voice, "bank:old_man_gruff")
        payload = req.options["payload"]
        self.assertTrue(payload["reference_audio"].startswith("data:audio/wav;base64,"))
        self.assertNotIn("voice", payload, "input_clone removes the preset voice")
        self.assertNotIn("base64", req.cache_tag, "the cache key never carries the audio")
        self.assertIn("bank:old_man_gruff:", req.cache_tag)
        self.synth(prov, "Cloned line.", prof)
        self.assertIn("reference_audio", self.creates()[-1]["body"]["input"])

        self.write_clip("old_man_gruff", 6.0, value=900)              # edit the clip
        req2 = prov.voice_request(prof, "Rok")
        self.assertNotEqual(req.cache_tag, req2.cache_tag, "a changed clip re-synthesizes")


# -- OpenAI-compatible servers ------------------------------------------------------------

class CompatTests(GenericCase):
    def compat_cfg(self, **extra) -> Config:
        spec = {"transport": "openai_compatible", "base_url": f"http://127.0.0.1:{self.compat.server_port}/v1",
                "model": "kokoro", "voices": "@kokoro_en", "response_format": "pcm",
                "extra": {"exaggeration": "{exaggeration}"},
                "controls": {"exaggeration": {"default": 0.5, "expressive": 0.8}}}
        spec.update(extra)
        return self.cfg(models={"local": spec})

    def test_streams_pcm_with_openai_shaped_body(self):
        prov = self.provider("local", self.compat_cfg())
        self.assertFalse(prov.needs_key)
        pcm = self.synth(prov, "Local voice.", {"gender": "female", "pace": "fast", "tone": "bubbly"}, "Kupipi")
        (req,) = _Compat.log
        self.assertEqual(req["path"], "/v1/audio/speech")
        body = req["body"]
        self.assertEqual((body["model"], body["input"], body["response_format"]), ("kokoro", "Local voice.", "pcm"))
        self.assertTrue(body["voice"].startswith(("af_", "bf_")))
        self.assertEqual((body["speed"], body["exaggeration"]), (1.12, 0.8))
        self.assertIsNone(req["auth"])
        self.assertEqual(samples_of(pcm), [7] * 1500)

    def test_streaming_wav_and_optional_key(self):
        _Compat.reply = "wav"
        with mock.patch.dict("os.environ", {"LOCAL_TTS_KEY": "sekret"}):
            prov = self.provider("local", self.compat_cfg(response_format="wav", api_key_env="LOCAL_TTS_KEY"))
            pcm = self.synth(prov, "Wav stream.")
        self.assertEqual(samples_of(pcm), [7] * 1500)
        self.assertEqual(_Compat.log[-1]["auth"], "Bearer sekret")


# -- engine ------------------------------------------------------------------------------

def wait_for(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


class EngineTests(GenericCase):
    def test_switch_to_a_replicate_model_and_back(self):
        eng = self.make_engine(FakeTTS(), cfg=self.cfg())
        eng.handle(speak_msg("First on OpenAI.", name="Rok", race=8))
        eng.wait_idle()
        eng.handle({"t": "config", "provider": "minimax-replicate"})
        self.assertEqual(eng.tts.name, "minimax-replicate")
        self.assertTrue(any("voice provider: minimax-replicate" in m for m in self.say_lines()))
        job = eng.speak(__import__("kotodama.protocol", fromlist=["x"]).parse_speak(
            speak_msg("Now on MiniMax.", name="Rok", race=8, seq=2)))
        self.assertTrue(eng.wait_idle())
        self.assertEqual(job.source, "tts", job.error)
        self.assertEqual(self.creates()[-1]["body"]["input"]["text"], "Now on MiniMax.")
        self.assertEqual(eng.cache.stats()["files"], 2, "both providers' audio is cached")
        today = eng.usage.today()
        self.assertEqual((today["p:minimax-replicate:requests"], today["p:minimax-replicate:predictions"]), (1, 1))
        self.assertIn("minimax", eng.usage.summary())
        eng.handle({"t": "config", "provider": "openai"})
        self.assertEqual(eng.tts.name, "openai")

    def test_provider_listing_and_key_checks(self):
        cfg = self.cfg(api_key=None, api_key_env="KOTODAMA_TEST_NO_SUCH_VAR")
        eng = self.make_engine(FakeTTS(), cfg=cfg)
        eng.handle({"t": "cmd", "name": "providers_info"})
        lines = self.say_lines()
        self.assertTrue(any("minimax-replicate: MiniMax Speech 2.8 Turbo on Replicate (no API key)" in m
                            for m in lines))
        eng.handle({"t": "config", "provider": "minimax-replicate"})
        self.assertEqual(eng.tts.name, "openai")
        self.assertTrue(any("minimax-replicate needs an API key (KOTODAMA_TEST_NO_SUCH_VAR)" in m
                            for m in self.say_lines()))
        eng.handle({"t": "config", "provider": "nonesuch"})
        self.assertTrue(any("unknown voice provider 'nonesuch'" in m and "kokoro" in m for m in self.say_lines()))


class CliTests(GenericCase):
    def run_cli(self, *argv: str, models: dict | None = None) -> tuple[int, str]:
        import contextlib
        import io
        from kotodama import cli
        cfg_path = self.tmp / "config.json"
        cfg_path.write_text(json.dumps({
            "cache": {"dir": str(self.tmp / "cache")},
            "library": {"file": str(self.tmp / "lib" / "npcs.json"), "packs_dir": str(self.tmp / "lib" / "packs"),
                        "overrides": str(self.tmp / "lib" / "overrides.json")},
            "voice_bank": {"dir": str(self.tmp / "voices")},
            "replicate": {"base_url": f"http://127.0.0.1:{self.api.server_port}", "api_key": TOKEN,
                          "wait_seconds": 5, "timeout": 10},
            "models": models or {},
            "duck": {"enabled": False},
        }), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["-c", str(cfg_path), *argv])
        return rc, out.getvalue()

    def test_voices_design_fills_the_bank_with_labelled_clips(self):
        _Files.min_seconds = 6.0
        rc, out = self.run_cli("voices", "design", "--only", "galka", "--variants", "2", "--yes")
        self.assertEqual(rc, 0, out)
        bodies = [c["body"]["input"] for c in self.creates()]
        self.assertEqual(len(bodies), 2)
        self.assertEqual({b["mode"] for b in bodies}, {"voice_design"})
        self.assertTrue(all("Galka" in b["voice_description"] for b in bodies))
        self.assertTrue(all("very low pitch" in b["voice_description"] for b in bodies))
        self.assertNotIn("warm and friendly", bodies[0]["voice_description"], "variant 1 is the archetype as-is")
        self.assertIn("warm and friendly", bodies[1]["voice_description"])
        meta = json.loads((self.tmp / "voices" / "design-galka-1.json").read_text(encoding="utf-8"))
        self.assertEqual((meta["gender"], meta["age"]), ("male", "middle_aged"), "an adult archetype")
        self.assertIn("deep", meta["descriptive"].split())
        self.assertTrue(meta["reference_text"].startswith("The airship"))
        bank = VoiceBank(self.tmp / "voices")
        bank.refresh()
        self.assertEqual(set(bank.clips), {"bank:design-galka-1", "bank:design-galka-2"})
        self.assertEqual(bank.get("bank:design-galka-1").voice.gender, "male")
        rc, out = self.run_cli("voices", "design", "--only", "galka", "--variants", "2", "--yes")
        self.assertIn("nothing to do", out)
        self.assertEqual(len(self.creates()), 2, "existing clips are not re-made")

    def test_replicate_schema_drafts_a_working_adapter(self):
        rc, out = self.run_cli("replicate", "schema", "resemble-ai/chatterbox-turbo")
        self.assertEqual(rc, 0, out)
        self.assertIn("voice", out)
        draft = json.loads(out[out.index("{"):out.rindex("}") + 1])
        (name, spec), = draft.items()
        self.assertEqual(name, "chatterbox-turbo")
        build_adapter(name, spec, {})

    def test_doctor_checks_adapters_against_live_schemas(self):
        from kotodama import cli
        results: list[tuple[str, bool, str]] = []
        models = {"minimax-replicate": {"input": {"voice": "{voice}"}}}    # a field the model doesn't have
        cfg = self.cfg(models=models)
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._doctor_models(cfg, False, lambda *r: results.append(r), "minimax-replicate")
        self.assertIn(("Replicate token", True, "account tester"), results)
        bad = [r for r in results if r[0] == "minimax-replicate"]
        self.assertTrue(bad and not bad[0][1] and "'voice' is not a field" in bad[0][2], results)
        self.assertIn("[ok] kokoro: jaaari/kokoro-82m matches its adapter", out.getvalue())


if __name__ == "__main__":
    unittest.main()
