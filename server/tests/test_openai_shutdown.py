"""OpenAI's speech shutdown (2027-01-06): Inworld is the default, and OpenAI
warns wherever it's listed or chosen."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import unittest
from datetime import date
from unittest import mock

from kotodama import cli
from kotodama.audio import Ducker, PactlBackend
from kotodama.config import DEFAULT_PROVIDER, DEFAULTS
from kotodama.engine import build_engine, make_provider, provider_catalog
from kotodama.adapters import load_adapters
from kotodama.tts_openai import SPEECH_SHUTDOWN, OpenAITTS, shutdown_label, shutdown_notice

from helpers import FakeTTS, KotodamaTestCase

BEFORE = date(2026, 11, 16)
AFTER = date(2027, 1, 6)
NO_KEY = {"api_key": None, "api_key_env": "KOTODAMA_TEST_NO_OPENAI_KEY"}
NO_INWORLD = {"api_key": None, "api_key_env": "KOTODAMA_TEST_NO_INWORLD_KEY"}


class _FakeServer:
    """Stands in for UDPServer so cmd_serve returns at once."""

    def __init__(self, cfg, engine):
        pass

    def serve_forever(self):
        pass

    def shutdown(self):
        pass


class NoticeTests(unittest.TestCase):
    def test_only_openai_itself_is_affected(self):
        self.assertIsNotNone(shutdown_notice("https://api.openai.com", BEFORE))
        self.assertIsNotNone(shutdown_notice("https://API.OpenAI.com/", BEFORE))
        self.assertIsNone(shutdown_notice("http://127.0.0.1:8880/v1", BEFORE))
        self.assertIsNone(shutdown_notice("https://api.openai.com.example.net", BEFORE))
        self.assertIsNone(shutdown_notice(None, BEFORE))

    def test_wording_follows_the_date(self):
        before = shutdown_notice("https://api.openai.com", BEFORE)
        after = shutdown_notice("https://api.openai.com", AFTER)
        self.assertIn("shut down on 2027-01-06", before)
        self.assertNotIn("were", before)
        self.assertIn("were shut down on 2027-01-06", after)
        self.assertIn("//koto provider inworld", before)
        self.assertEqual(shutdown_label("https://api.openai.com", BEFORE), "shuts down 2027-01-06")
        self.assertEqual(shutdown_label("https://api.openai.com", AFTER), "shut down 2027-01-06")
        self.assertEqual(shutdown_label("http://localhost:8880", BEFORE), "")
        self.assertEqual(SPEECH_SHUTDOWN, AFTER)


class ProviderErrorTests(KotodamaTestCase):
    def provider(self, today: date, base_url: str = "https://api.openai.com") -> OpenAITTS:
        prov = OpenAITTS(self.make_config(tts={"api_key": "sk-test", "base_url": base_url}))
        prov.today = lambda: today
        self.addCleanup(prov.close)
        return prov

    @staticmethod
    def body(code: str, message: str) -> bytes:
        return json.dumps({"error": {"code": code, "message": message}}).encode()

    def test_not_found_after_the_shutdown_says_why(self):
        err = self.provider(AFTER)._error(404, self.body("model_not_found", "no such model"))
        self.assertIn("no such model", str(err))
        self.assertIn("were shut down on 2027-01-06", str(err))
        self.assertEqual(err.code, "model")
        gone = self.provider(AFTER)._error(410, b"")
        self.assertIn("shut down", str(gone))
        coded = self.provider(AFTER)._error(400, self.body("model_not_found", "gone"))
        self.assertIn("shut down", str(coded))

    def test_other_errors_and_earlier_dates_are_unchanged(self):
        self.assertNotIn("shut down", str(self.provider(BEFORE)._error(404, self.body("x", "nope"))))
        auth = self.provider(AFTER)._error(401, self.body("invalid_api_key", "bad key"))
        self.assertNotIn("shut down", str(auth))
        self.assertEqual(auth.code, "auth")
        local = self.provider(AFTER, "http://127.0.0.1:8880")._error(404, self.body("x", "nope"))
        self.assertNotIn("shut down", str(local))


class DefaultProviderTests(KotodamaTestCase):
    def test_inworld_is_the_default(self):
        self.assertEqual(DEFAULT_PROVIDER, "inworld")
        self.assertEqual(DEFAULTS["tts"]["provider"], "inworld")
        names: list[str] = []

        def factory(name):
            names.append(name)
            return FakeTTS()

        engine = build_engine(self.make_config(), reply=self.replies.append, null_player=True,
                              ducker=Ducker(enabled=False), provider_factory=factory)
        self.addCleanup(engine.close)
        self.assertEqual(names, ["inworld"])
        self.assertEqual(engine.default_provider, FakeTTS.name)

    def test_providers_command_marks_inworld(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.cmd_providers(self.make_config(), argparse.Namespace())
        lines = out.getvalue().splitlines()
        self.assertTrue(any(line.startswith("* inworld ") for line in lines), lines)
        openai = [line for line in lines if line.startswith("  openai ")]
        self.assertEqual(len(openai), 1, lines)
        self.assertIn("2027-01-06)", openai[0])

    def test_catalog_marks_openai_only_for_openai_itself(self):
        cfg = self.make_config()
        desc = provider_catalog(cfg, load_adapters(cfg))["openai"][0]
        self.assertIn("2027-01-06", desc)
        local = self.make_config(tts={"base_url": "http://127.0.0.1:8880"})
        self.assertNotIn("2027-01-06", provider_catalog(local, load_adapters(local))["openai"][0])


class EngineNoticeTests(KotodamaTestCase):
    def test_switching_to_openai_says_the_notice(self):
        start = FakeTTS()
        start.name = "inworld"
        openai = OpenAITTS(self.make_config(tts={"api_key": "sk-test"}))
        openai.warm = lambda: None            # no network in tests
        openai.today = lambda: BEFORE
        self.addCleanup(openai.close)
        eng = self.make_engine(start, cfg=self.make_config(tts={"api_key": "sk-test"}),
                               provider_factory={"openai": openai}.__getitem__)
        eng.handle({"t": "config", "provider": "openai"})
        self.assertIs(eng.tts, openai)
        lines = self.say_lines()
        self.assertTrue(any(m.startswith("voice provider: openai/") for m in lines), lines)
        self.assertTrue(any("shut down on 2027-01-06" in m for m in lines), lines)

    def test_other_providers_say_nothing_extra(self):
        eng = self.make_engine(FakeTTS(), provider_factory=lambda name: FakeTTS() if name else None)
        fake = FakeTTS()
        fake.name = "elevenlabs"
        eng.providers["elevenlabs"] = fake
        eng.handle({"t": "config", "provider": "elevenlabs"})
        self.assertIs(eng.tts, fake)
        self.assertFalse(any("2027-01-06" in m for m in self.say_lines()))


class ServeWarningTests(KotodamaTestCase):
    def serve_logs(self, cfg, tts) -> list[str]:
        engine = self.make_engine(tts, cfg=cfg)
        with mock.patch.object(cli, "UDPServer", _FakeServer), self.assertLogs(level="WARNING") as logs:
            cli.cmd_serve(cfg, argparse.Namespace(), stdin=io.StringIO(), engine=engine)
        return [r.getMessage() for r in logs.records]

    def test_missing_key_names_the_providers_that_are_ready(self):
        cfg = self.make_config(tts={"api_key": "sk-test"}, inworld=NO_INWORLD)
        inworld = make_provider(cfg, "inworld")
        self.addCleanup(inworld.close)
        msgs = self.serve_logs(cfg, inworld)
        missing = [m for m in msgs if m.startswith("no inworld API key")]
        self.assertEqual(len(missing), 1, msgs)
        self.assertIn("KOTODAMA_TEST_NO_INWORLD_KEY", missing[0])
        self.assertIn("ready to use: openai", missing[0])

    def test_openai_at_startup_logs_the_notice(self):
        cfg = self.make_config(tts={"api_key": "sk-test"}, inworld=NO_INWORLD)
        openai = OpenAITTS(cfg)
        openai.today = lambda: BEFORE
        self.addCleanup(openai.close)
        msgs = self.serve_logs(cfg, openai)
        self.assertTrue(any("shut down on 2027-01-06" in m for m in msgs), msgs)
        self.assertFalse(any(m.startswith("no openai API key") for m in msgs), msgs)


class DoctorTests(KotodamaTestCase):
    def doctor(self, cfg, tts) -> str:
        engine = self.make_engine(tts, cfg=cfg)
        out = io.StringIO()

        def no_pactl(*_a, **_k):
            raise FileNotFoundError("pactl")

        with mock.patch.object(cli, "build_engine", lambda *_a, **_k: engine), \
                mock.patch.object(cli, "make_backend", lambda *_a, **_k: PactlBackend(no_pactl)), contextlib.redirect_stdout(out):
            cli.cmd_doctor(cfg, argparse.Namespace(env_file=None, live=False, provider=None))
        return out.getvalue()

    def config(self, **over):
        return self.make_config(listen={"host": "127.0.0.1", "port": 0},
                                elevenlabs={"api_key": None, "api_key_env": "KOTODAMA_TEST_NO_EL_KEY"},
                                **over)

    def test_keyless_default_fails_the_key_check(self):
        cfg = self.config(tts=NO_KEY, inworld=NO_INWORLD)
        inworld = make_provider(cfg, "inworld")
        self.addCleanup(inworld.close)
        out = self.doctor(cfg, inworld)
        self.assertIn("[FAIL] inworld API key: missing (KOTODAMA_TEST_NO_INWORLD_KEY)", out)
        self.assertNotIn("2027-01-06", out)      # no OpenAI key, OpenAI not active

    def test_openai_with_a_key_passes_and_warns(self):
        cfg = self.config(tts={"api_key": "sk-test"}, inworld=NO_INWORLD)
        openai = OpenAITTS(cfg)
        self.addCleanup(openai.close)
        out = self.doctor(cfg, openai)
        self.assertIn("[ok] openai API key: set", out)
        self.assertTrue(any(line.startswith("[warn] OpenAI's speech models") and "2027-01-06" in line
                            for line in out.splitlines()), out)


if __name__ == "__main__":
    unittest.main()
