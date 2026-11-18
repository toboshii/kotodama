"""ElevenLabs provider, voice catalog, auto-casting and provider switching."""

from __future__ import annotations

import http.server
import json
import threading
import time
import unittest
import urllib.parse

from kotodama import protocol
from kotodama.audio import Ducker
from kotodama.archetypes import Archetypes
from kotodama.cache import make_key
from kotodama.config import Config
from kotodama.engine import build_engine
from kotodama.infer import build_schema
from kotodama.tts_base import TTSError, VoiceRequest
from kotodama.tts_elevenlabs import ElevenLabsTTS
from kotodama.tts_openai import OpenAITTS
from kotodama.usage import Usage
from kotodama.casting import CastingMemory, VoiceCatalog, cast, rank

from helpers import FakeTTS, KotodamaTestCase, speak_msg

VOICES = [
    {"voice_id": "deep01", "name": "Brock", "category": "premade",
     "labels": {"gender": "male", "age": "old", "accent": "american", "use_case": "characters"},
     "description": "deep, gravelly"},
    {"voice_id": "male02", "name": "Daniel", "category": "premade",
     "labels": {"gender": "male", "age": "middle_aged", "accent": "british", "use_case": "news"},
     "description": "authoritative, formal"},
    {"voice_id": "male03", "name": "Liam", "category": "premade",
     "labels": {"gender": "male", "age": "young", "accent": "american", "use_case": "conversational"},
     "description": "friendly, energetic"},
    {"voice_id": "fem01", "name": "Lily", "category": "premade",
     "labels": {"gender": "female", "age": "young", "accent": "american", "use_case": "characters"},
     "description": "bright, childish, cute"},
    {"voice_id": "fem02", "name": "Alice", "category": "premade",
     "labels": {"gender": "female", "age": "middle_aged", "accent": "british", "use_case": "narration"},
     "description": "calm, warm"},
    {"voice_id": "fem03", "name": "Grandma Rose", "category": "cloned",
     "labels": {"gender": "female", "age": "old", "accent": "american", "use_case": "narrative_story"},
     "description": "soft, kindly"},
    {"voice_id": "nar01", "name": "George", "category": "premade",
     "labels": {"gender": "male", "age": "middle_aged", "accent": "british", "use_case": "narration"},
     "description": "warm storyteller"},
]

PINNED_OPENAI_KEYS = {   # computed with the pre-refactor code; must never change
    ("Ayame", 2): "a8b4917040a521d19ae0f96b592ff1d5b679bb5d25c9386d0e7a1c6d32dbefdd",
    ("Rok", 8): "162278d70cdcfcf738868cfc9b97cbe7da49e82fea1314207e5b3e7ca356bbdd",
}


class _Server(http.server.ThreadingHTTPServer):
    def handle_error(self, *a):
        pass


class _ELHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: list = []
    voice_gets: list = []
    reject_models: set = set()
    busy: int = 0              # number of 429s to send before succeeding
    fail: tuple | None = None  # (status, body) for every stream request

    def log_message(self, *a):
        pass

    def _json(self, status: int, payload) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        cls = type(self)
        cls.voice_gets.append({"path": url.path, "query": q, "key": self.headers.get("xi-api-key")})
        if q.get("next_page_token") == ["page2"]:
            self._json(200, {"voices": VOICES[4:], "has_more": False, "next_page_token": None})
        else:
            self._json(200, {"voices": VOICES[:4], "has_more": True, "next_page_token": "page2"})

    def do_POST(self):
        cls = type(self)
        url = urllib.parse.urlparse(self.path)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        cls.requests.append({"path": url.path, "query": urllib.parse.parse_qs(url.query),
                             "key": self.headers.get("xi-api-key"), "body": body})
        if cls.fail:
            return self._json(*cls.fail)
        if body["model_id"] in cls.reject_models:
            return self._json(422, {"detail": {"status": "invalid_model",
                                               "message": f"Model {body['model_id']} is not supported here"}})
        if cls.busy > 0:
            cls.busy -= 1
            return self._json(429, {"detail": {"status": "too_many_concurrent_requests",
                                               "message": "Too many concurrent requests"}})
        self.send_response(200)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for size in (1001, 2000, 777):
            self.wfile.write(f"{size:x}\r\n".encode() + b"\x02" * size + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")


class ELTestCase(KotodamaTestCase):
    def setUp(self):
        super().setUp()
        _ELHandler.requests, _ELHandler.voice_gets = [], []
        _ELHandler.reject_models, _ELHandler.busy, _ELHandler.fail = set(), 0, None
        self.httpd = _Server(("127.0.0.1", 0), _ELHandler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.notes: list[str] = []

    def el_config(self, **el) -> Config:
        settings = {"base_url": f"http://127.0.0.1:{self.httpd.server_port}", "api_key": "xi-test"}
        settings.update(el)
        return self.make_config(elevenlabs=settings)

    def el(self, **el) -> ElevenLabsTTS:
        tts = ElevenLabsTTS(self.el_config(**el), notify=self.notes.append)
        self.addCleanup(tts.close)
        return tts


class ElevenLabsClientTests(ELTestCase):
    def test_request_shape_settings_and_leading_tag(self):
        tts = self.el()
        req = tts.voice_request({"gender": "male", "age": "elderly", "pitch": "very low", "pace": "brisk",
                                 "tone": "cheerful and gruff"}, "Rok")
        chunks = list(tts.stream("Hm.", req))
        self.assertTrue(all(len(c) % 2 == 0 for c in chunks))
        self.assertEqual(sum(map(len, chunks)), 3778)
        r = _ELHandler.requests[-1]
        self.assertEqual(r["path"], f"/v1/text-to-speech/{req.voice}/stream")
        self.assertEqual(r["query"], {"output_format": ["pcm_24000"]})
        self.assertEqual(r["key"], "xi-test")
        self.assertEqual(r["body"]["model_id"], "eleven_v4_turbo")
        self.assertEqual(r["body"]["text"], "[excited] Hm.")
        vs = r["body"]["voice_settings"]
        self.assertEqual((vs["speed"], vs["stability"], vs["similarity_boost"], vs["style"]), (1.06, 0.5, 0.75, 0.0))
        self.assertEqual(req.voice, "deep01", "an old gruff deep male gets the deep gravelly voice")
        self.assertEqual(req.label, "Brock")

    def test_temperament_drives_stability_and_narrator_is_steady(self):
        tts = self.el()
        self.assertEqual(tts.voice_settings({"tone": "bubbly"}, "x")["stability"], 0.35)
        self.assertEqual(tts.voice_settings({"personality": "stoic and stern"}, "x")["stability"], 0.65)
        self.assertEqual(tts.voice_settings({}, "@narrator")["stability"], 0.6)
        self.assertEqual(tts.voice_settings({"pace": "slow"}, "x")["speed"], 0.88)

    def test_no_tags_on_models_without_them_or_when_disabled(self):
        for kw in ({"model": "eleven_flash_v2_5"}, {"audio_tags": False}):
            tts = self.el(**kw)
            req = tts.voice_request({"gender": "male", "tone": "cheerful"}, "Rok")
            self.assertIsNone(req.options["tag"], kw)
            list(tts.stream("Hm.", req))
            self.assertEqual(_ELHandler.requests[-1]["body"]["text"], "Hm.", kw)

    def test_cache_tag_changes_with_settings_and_tag(self):
        tts = self.el()
        base = tts.voice_request({"gender": "male", "pace": "normal"}, "Rok")
        faster = tts.voice_request({"gender": "male", "pace": "fast"}, "Rok")
        tagged = tts.voice_request({"gender": "male", "pace": "normal", "tone": "sarcastic"}, "Rok")
        self.assertEqual(len({base.cache_tag, faster.cache_tag, tagged.cache_tag}), 3)
        keys = {make_key("elevenlabs", tts.model, r.voice, r.cache_tag, "Hm.", "pcm") for r in (base, faster, tagged)}
        self.assertEqual(len(keys), 3)

    def test_rejected_model_falls_back_once_and_drops_the_tag(self):
        _ELHandler.reject_models = {"eleven_v4_turbo"}
        tts = self.el()
        req = tts.voice_request({"gender": "male", "tone": "cheerful"}, "Rok")
        self.assertEqual(sum(map(len, tts.stream("Hm.", req))), 3778)
        first, second = _ELHandler.requests
        self.assertEqual((first["body"]["model_id"], second["body"]["model_id"]), ("eleven_v4_turbo", "eleven_flash_v2_5"))
        self.assertEqual(second["body"]["text"], "Hm.", "flash doesn't take audio tags")
        self.assertTrue(tts.fell_back)
        self.assertEqual(tts.model, "eleven_flash_v2_5")
        self.assertEqual(len(self.notes), 1)
        list(tts.stream("Again.", tts.voice_request({"gender": "male"}, "Rok")))
        self.assertEqual(_ELHandler.requests[-1]["body"]["model_id"], "eleven_flash_v2_5")
        self.assertEqual(len(_ELHandler.requests), 3, "no second rejection round-trip")
        self.assertIn("fallback from eleven_v4_turbo", tts.status_text())

    def test_busy_is_retried_once(self):
        _ELHandler.busy = 1
        tts = self.el()
        req = tts.voice_request({"gender": "female"}, "Ayame")
        self.assertEqual(sum(map(len, tts.stream("Hi.", req))), 3778)
        self.assertEqual(len(_ELHandler.requests), 2)
        _ELHandler.busy = 5
        with self.assertRaises(TTSError) as cm:
            list(tts.stream("Hi.", req))
        self.assertEqual(cm.exception.code, "concurrency")

    def test_error_mapping(self):
        tts = self.el()
        req = tts.voice_request({"gender": "female"}, "Ayame")
        cases = [
            ((401, {"detail": {"status": "quota_exceeded", "message": "You have 3 credits left"}}), "quota"),
            ((401, {"detail": {"status": "invalid_api_key", "message": "Invalid API key"}}), "auth"),
            ((422, {"detail": [{"loc": ["body", "voice_settings", "speed"], "msg": "too fast", "type": "x"}]}), None),
        ]
        for fail, code in cases:
            _ELHandler.fail = fail
            with self.assertRaises(TTSError) as cm:
                list(tts.stream("Hi.", req))
            self.assertEqual(cm.exception.code, code, fail)
        self.assertIn("speed", str(cm.exception))

    def test_deleted_voice_is_dropped_and_recast(self):
        tts = self.el()
        req = tts.voice_request({"gender": "female", "age": "young", "pitch": "very high"}, "Kupipi")
        self.assertEqual(req.voice, "fem01")
        _ELHandler.fail = (404, {"detail": {"status": "voice_not_found", "message": "voice not found"}})
        with self.assertRaises(TTSError) as cm:
            list(tts.stream("Hi.", req))
        self.assertEqual(cm.exception.code, "voice")
        self.assertIsNone(tts.catalog.get("fem01"))
        again = tts.voice_request({"gender": "female", "age": "young", "pitch": "very high"}, "Kupipi")
        self.assertNotEqual(again.voice, "fem01")

    def test_missing_key_and_missing_voice(self):
        tts = self.el(api_key=None, api_key_env="KOTODAMA_NO_SUCH_ENV")
        with self.assertRaises(TTSError) as cm:
            list(tts.stream("Hi.", VoiceRequest("x")))
        self.assertEqual(cm.exception.code, "auth")
        with self.assertRaises(TTSError) as cm:
            list(self.el().stream("Hi.", VoiceRequest("")))
        self.assertEqual(cm.exception.code, "voice")

    def test_output_format_must_be_pcm(self):
        with self.assertRaises(ValueError):
            self.el(output_format="mp3_44100_128")
        self.assertEqual(self.el(output_format="pcm_16000").sample_rate, 16000)


class CatalogTests(ELTestCase):
    def test_sync_pages_through_all_voices_and_persists(self):
        tts = self.el()
        self.assertEqual(tts.catalog.sync(), 7)
        self.assertEqual(len(_ELHandler.voice_gets), 2)
        self.assertEqual(_ELHandler.voice_gets[0]["query"]["page_size"], ["100"])
        self.assertEqual(_ELHandler.voice_gets[1]["query"]["next_page_token"], ["page2"])
        self.assertEqual(_ELHandler.voice_gets[0]["key"], "xi-test")
        fresh = self.el()                                     # loads from disk, no request
        self.assertEqual(len(fresh.catalog.voices), 7)
        self.assertEqual(len(_ELHandler.voice_gets), 2)

    def test_exclude_list(self):
        tts = self.el(voices={"exclude": ["Brock", "fem03"]})
        tts.catalog.sync()
        self.assertEqual({v.id for v in tts.catalog.voices}, {"male02", "male03", "fem01", "fem02", "nar01"})

    def test_first_use_fetches_and_old_lists_refresh_in_background(self):
        tts = self.el()
        tts.voice_request({"gender": "male"}, "Rok")              # empty catalog: fetched on demand
        self.assertEqual(len(_ELHandler.voice_gets), 2)
        tts.catalog.fetched_at = time.time() - 3 * 86400          # pretend it's three days old
        tts.ensure_catalog()
        deadline = time.time() + 3
        while len(_ELHandler.voice_gets) < 4 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(_ELHandler.voice_gets), 4)

    def test_find_by_name_or_id(self):
        cat = VoiceCatalog(self.tmp / "v.json", fetch=lambda: VOICES)
        cat.sync()
        self.assertEqual(cat.find("george").id, "nar01")
        self.assertEqual(cat.find("Grandma_Rose").id, "fem03")
        self.assertEqual(cat.find("nar01").name, "George")
        self.assertEqual(cat.find("Gra").id, "fem03", "a unique prefix is enough")
        self.assertIsNone(cat.find("L"), "ambiguous prefix (Liam, Lily)")
        self.assertIsNone(cat.find("Nobody"))


class CastingTests(KotodamaTestCase):
    def setUp(self):
        super().setUp()
        self.arch = Archetypes.load()
        self.cat = VoiceCatalog(self.tmp / "v.json", fetch=lambda: VOICES)
        self.cat.sync()

    def archetype(self, key: str) -> dict:
        a = self.arch.items[key]
        return {f: a.get(f) for f in ("gender", "age", "species", "personality", "tone", "accent", "pitch", "pace")}

    def test_archetypes_land_on_fitting_voices(self):
        self.assertEqual(cast(self.cat, self.archetype("galka"), "Rok"), "deep01")
        self.assertEqual(cast(self.cat, self.archetype("taru_f"), "Kupipi"), "fem01")
        self.assertEqual(cast(self.cat, self.archetype("elvaan_m"), "Trion"), "male02",
                         "proud, formal, aristocratic -> the British authoritative voice")
        self.assertIn(cast(self.cat, self.archetype("narrator"), "@narrator"), {"nar01", "fem02"})

    def test_llm_style_profile_details_matter(self):
        kindly_grandma = {"gender": "female", "age": "elderly", "personality": "kindly", "tone": "soft and warm"}
        self.assertEqual(cast(self.cat, kindly_grandma, "Old Woman"), "fem03")

    def test_gender_is_never_crossed(self):
        for name in ("A", "B", "C", "D", "E", "F", "G", "H"):
            v = cast(self.cat, {"gender": "female"}, name)
            self.assertIn(v, {"fem01", "fem02", "fem03"})
            v = cast(self.cat, {"gender": "male"}, name)
            self.assertIn(v, {"deep01", "male02", "male03", "nar01"})

    def test_gender_beats_an_otherwise_perfect_description(self):
        gruff_man = {"voice_id": "m1", "name": "Gruff", "labels": {"gender": "male", "age": "old"},
                     "description": "deep gravelly raspy gruff rough booming"}
        plain_woman = {"voice_id": "f1", "name": "Plain", "labels": {"gender": "female", "age": "young"},
                       "description": ""}
        cat = VoiceCatalog(self.tmp / "g.json", fetch=lambda: [gruff_man, plain_woman])
        cat.sync()
        profile = {"gender": "female", "age": "elderly", "pitch": "very low", "tone": "gruff and gravelly"}
        self.assertEqual(cast(cat, profile, "Old Witch"), "f1")

    def test_an_account_without_the_right_gender_still_gets_a_voice(self):
        cat = VoiceCatalog(self.tmp / "m.json", fetch=lambda: [v for v in VOICES if v["labels"]["gender"] == "male"])
        cat.sync()
        self.assertIn(cast(cat, {"gender": "female", "age": "young"}, "Kupipi"),
                      {"deep01", "male02", "male03", "nar01"})

    def test_stable_and_varied(self):
        profile = {"gender": "male", "age": "adult"}
        first = [cast(self.cat, profile, f"Guard {i}") for i in range(30)]
        self.assertEqual(first, [cast(self.cat, profile, f"Guard {i}") for i in range(30)], "repeatable")
        self.assertGreater(len(set(first)), 1, "near-equal fits are spread across NPCs")

    def test_clearly_worse_fits_are_never_picked_for_variety(self):
        ranked = rank(self.cat, self.archetype("galka"), "Rok")
        best = ranked[0][0]
        for name in (f"Galka {i}" for i in range(40)):
            chosen = cast(self.cat, self.archetype("galka"), name)
            score = next(s for s, v in ranked if v.id == chosen)
            self.assertGreaterEqual(score, best - 1.5)

    def test_casting_memory_survives_new_voices_but_not_profile_changes(self):
        mem = CastingMemory(self.tmp / "mem.json")
        profile = {"gender": "male", "age": "elderly", "pitch": "very low", "tone": "gruff"}
        self.assertEqual(cast(self.cat, profile, "Rok", mem), "deep01")
        better = dict(VOICES[0], voice_id="deep99", name="Even Deeper",
                      description="deep, gravelly, raspy, gruff, rough, booming")
        cat2 = VoiceCatalog(self.tmp / "v2.json", fetch=lambda: VOICES + [better])
        cat2.sync()
        self.assertEqual(cast(cat2, profile, "Rok", mem), "deep01", "existing NPCs keep their voice")
        self.assertEqual(cast(cat2, profile, "Newcomer", mem), "deep99", "new NPCs get the new voice")
        changed = {**profile, "age": "young", "pitch": "medium", "tone": "friendly and energetic"}
        self.assertEqual(cast(cat2, changed, "Rok", mem), "male03", "a changed profile re-casts")
        self.assertEqual(CastingMemory(self.tmp / "mem.json").get("rok", changed), "male03", "memory is persisted")

    def test_explicit_voice_wins_and_foreign_ids_are_ignored(self):
        cfg = self.make_config(elevenlabs={"api_key": "x", "base_url": "http://127.0.0.1:9"})
        tts = ElevenLabsTTS(cfg)
        self.addCleanup(tts.close)
        tts.catalog.voices = self.cat.snapshot()
        tts.catalog.fetched_at = time.time()
        self.assertEqual(tts.voice_request({"voice": "nar01", "gender": "female"}, "X").voice, "nar01")
        foreign = tts.voice_request({"voice": "someone-elses-clone", "gender": "female", "age": "young",
                                     "pitch": "very high"}, "Kupipi")
        self.assertEqual(foreign.voice, "fem01")
        self.assertEqual(tts.resolve_voice("george"), "nar01")


class FakeElevenLabs(FakeTTS):
    name = "elevenlabs"
    model = "fake-el"
    dynamic_voices = True

    def __init__(self, api_key="xi", **kw):
        super().__init__(**kw)
        self.api_key = api_key
        self.forgotten = []

    def ensure_catalog(self, wait=True):
        pass

    def voice_request(self, profile, ident):
        voice = profile.get("voice") or "auto-" + ident
        return VoiceRequest(voice, {"voice_settings": {}}, json.dumps({"s": profile.get("pace")}), voice)

    def resolve_voice(self, name):
        return {"george": "nar01"}.get(name.lower())

    def forget_casting(self, ident):
        self.forgotten.append(ident)

    def credits_per_char(self):
        return 0.5

    def status_text(self):
        return "elevenlabs/fake-el, 7 voices"


class _NoNetOpenAI(OpenAITTS):
    def stream(self, text, request):
        yield b"\x01\x00" * 100


class EngineProviderTests(KotodamaTestCase):
    def two_provider_engine(self, el=None, **cfg):
        oa, el = FakeTTS(), el or FakeElevenLabs()
        factory = {"openai": oa, "elevenlabs": el}.__getitem__
        eng = self.make_engine(oa, cfg=self.make_config(**cfg) if cfg else None, provider_factory=factory)
        return eng, oa, el

    def test_openai_cache_keys_are_unchanged_by_the_refactor(self):
        cfg = self.make_config(tts={"api_key": "sk-test"})
        eng = self.make_engine(_NoNetOpenAI(cfg), cfg=cfg)
        for (name, race), expected in PINNED_OPENAI_KEYS.items():
            eng.speak(protocol.parse_speak(speak_msg("Welcome to Norg.", name=name, race=race, zone=230)))
            self.assertTrue(eng.wait_idle())
            self.assertEqual(eng.last["key"], expected, f"{name}: existing cached audio would be orphaned")

    def test_live_switch_and_back_keeps_each_providers_cache(self):
        eng, oa, el = self.two_provider_engine()
        eng.handle(speak_msg("Hello.", name="Ayame"))
        eng.wait_idle()
        eng.handle({"t": "config", "provider": "elevenlabs"})
        self.assertEqual(eng.tts.name, "elevenlabs")
        self.assertTrue(any("voice provider: elevenlabs" in m for m in self.say_lines()))
        eng.handle(speak_msg("Hello.", name="Ayame", seq=2))
        eng.wait_idle()
        self.assertEqual((len(oa.calls), len(el.calls)), (1, 1))
        self.assertEqual(el.calls[0]["voice"], "auto-ayame")
        eng.handle({"t": "config", "provider": ""})              # back to the configured default
        self.assertEqual(eng.tts.name, "openai")
        job = eng.speak(protocol.parse_speak(speak_msg("Hello.", name="Ayame", seq=3)))
        eng.wait_idle()
        self.assertEqual((job.source, len(oa.calls)), ("cache", 1), "OpenAI audio still cached")
        self.assertEqual(eng.usage.today()["el_requests"], 1)
        self.assertEqual(eng.usage.today()["el_credits"], 3.0)

    def test_switch_without_key_is_refused(self):
        eng, _, _ = self.two_provider_engine(el=FakeElevenLabs(api_key=None))
        eng.handle({"t": "config", "provider": "elevenlabs"})
        self.assertEqual(eng.tts.name, "openai")
        self.assertTrue(any("ELEVENLABS_API_KEY" in m for m in self.say_lines()))

    def test_unknown_provider_is_refused(self):
        eng, _, _ = self.two_provider_engine()
        eng.handle({"t": "config", "provider": "bogus"})
        self.assertEqual(eng.tts.name, "openai")
        self.assertTrue(any("unknown voice provider" in m for m in self.say_lines()))

    def test_line_in_flight_finishes_on_its_own_provider(self):
        """The switch lands between choosing the voice and streaming: the line
        must still be synthesized by the provider that chose its voice."""
        class GatedTTS(FakeTTS):
            def __init__(self):
                super().__init__()
                self.entered, self.gate = threading.Event(), threading.Event()

            def voice_request(self, profile, ident):
                self.entered.set()
                self.gate.wait(5)
                return super().voice_request(profile, ident)

        oa, el = GatedTTS(), FakeElevenLabs()
        eng = self.make_engine(oa, provider_factory={"openai": oa, "elevenlabs": el}.__getitem__)
        job = eng.speak(protocol.parse_speak(speak_msg("A line.")))
        self.assertTrue(oa.entered.wait(5))
        eng.handle({"t": "config", "provider": "elevenlabs", "interrupt": False})
        oa.gate.set()
        eng.wait_idle()
        self.assertEqual((job.source, len(oa.calls), len(el.calls)), ("tts", 1, 0))

    def test_voice_set_resolves_account_voice_names(self):
        eng, _, el = self.two_provider_engine()
        eng.handle({"t": "config", "provider": "elevenlabs"})
        eng.handle({"t": "cmd", "name": "voice_set", "npc": "Rok", "params": {"voice": "George"}})
        self.assertEqual(eng.lib.override("rok"), {"voice": {"elevenlabs": "nar01"}})
        eng.handle({"t": "cmd", "name": "voice_set", "npc": "Rok", "params": {"voice": "Nobody"}})
        self.assertTrue(any("no elevenlabs voice called 'Nobody'" in m for m in self.say_lines()))
        eng.handle({"t": "cmd", "name": "voice_show", "npc": "Rok"})
        self.assertTrue(any("elevenlabs voice nar01" in m for m in self.say_lines()))
        eng.handle({"t": "cmd", "name": "voice_reset", "npc": "Rok"})
        self.assertIn("Rok", el.forgotten, "reset also clears the casting memory")

    def test_inference_schema_follows_the_provider(self):
        cfg = self.make_config(tts={"api_key": "sk-test"}, inference={"provider": "openai"})
        oa, el = FakeTTS(), FakeElevenLabs()
        eng = build_engine(cfg, tts=oa, reply=self.replies.append, null_player=True,
                           ducker=Ducker(enabled=False),
                           provider_factory={"openai": oa, "elevenlabs": el}.__getitem__)
        self.addCleanup(eng.close)
        self.assertIn("voice", eng.inference.inferer.schema["properties"])
        eng.handle({"t": "config", "provider": "elevenlabs"})
        self.assertNotIn("voice", eng.inference.inferer.schema["properties"],
                         "ElevenLabs voices are cast from the account, not chosen by the LLM")
        self.assertEqual(eng.inference.tts_provider, "elevenlabs")

    def test_schema_builder_flag(self):
        arch = Archetypes.load()
        self.assertIn("voice", build_schema(arch, "openai")["properties"])
        s = build_schema(arch, "elevenlabs", include_voice=False)
        self.assertNotIn("voice", s["properties"])
        self.assertEqual(set(s["required"]), set(s["properties"]))

    def test_usage_summary_reports_credits(self):
        u = Usage(self.tmp / "u.json")
        u.add_tts("elevenlabs", 120, 4.0, 60.0)
        u.add_tts("openai", 40, 2.0)
        text = u.summary()
        self.assertIn("2 synthesized", text)
        self.assertIn("ElevenLabs 120 chars (~60 credits est.)", text)
        self.assertIn("OpenAI ~$", text)

    def test_usage_summary_prices_credits(self):
        u = Usage(self.tmp / "u.json", el_usd_per_1k_credits=0.08)
        u.add_tts("elevenlabs", 20000, 60.0, 20000.0)
        self.assertIn("ElevenLabs 20000 chars (~20000 credits, ~$1.600 est.)", u.summary())

    def test_usage_summary_prices_flash_at_half(self):
        u = Usage(self.tmp / "u.json", el_usd_per_1k_credits=0.08)
        u.add_tts("elevenlabs", 20000, 60.0, 10000.0)   # 0.5 credits per char
        self.assertIn("(~10000 credits, ~$0.800 est.)", u.summary())

    def test_price_comes_from_config(self):
        cases = [({}, "~$0.008 est."),                                  # default 0.08
                 ({"price": {"per_1k_credits": 0.2}}, "~$0.020 est."),
                 ({"price": {"per_1k_credits": None}}, "credits est.)"),
                 ({"price": None}, "credits est.)"),
                 ({"price": {"per_1k_credits": "cheap"}}, "credits est.)")]
        for el, expected in cases:
            with self.subTest(el=el):
                cfg = self.make_config(elevenlabs=el)
                eng = build_engine(cfg, tts=FakeTTS(), null_player=True, ducker=Ducker(enabled=False))
                self.addCleanup(eng.close)
                eng.usage._days = {}
                eng.usage.add_tts("elevenlabs", 100, 1.0, 100.0)
                text = eng.usage.summary()
                self.assertIn(expected, text)
                if "$" not in expected:
                    self.assertNotIn("~$", text)


class EndToEndTests(ELTestCase):
    def test_real_provider_through_the_engine(self):
        cfg = self.make_config(tts={"provider": "elevenlabs"},
                               elevenlabs={"base_url": f"http://127.0.0.1:{self.httpd.server_port}",
                                           "api_key": "xi-test"})
        eng = build_engine(cfg, reply=self.replies.append, null_player=True, ducker=Ducker(enabled=False))
        self.addCleanup(eng.close)
        self.assertEqual(eng.tts.name, "elevenlabs")
        job = eng.speak(protocol.parse_speak(speak_msg("Hm.", name="Rok", race=8)))
        self.assertTrue(eng.wait_idle(10))
        self.assertEqual(job.source, "tts", job.error)
        self.assertEqual(_ELHandler.requests[0]["path"], "/v1/text-to-speech/deep01/stream", "Galka cast to Brock")
        again = eng.speak(protocol.parse_speak(speak_msg("Hm.", name="Rok", race=8, seq=2)))
        self.assertTrue(eng.wait_idle(10))
        self.assertEqual((again.source, len(_ELHandler.requests)), ("cache", 1))
        eng.handle({"t": "cmd", "name": "status"})
        self.assertTrue(any("elevenlabs/eleven_v4_turbo, 7 voices" in m for m in self.say_lines()))


if __name__ == "__main__":
    unittest.main()
