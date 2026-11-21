"""A casting provider that can't start (its SDK or API key is missing) turns
voice casting off and says why, instead of stopping the sidecar."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

from kotodama import cli, infer

from helpers import FakeTTS, KotodamaTestCase

# Put in sys.modules, this makes `import anthropic` fail whether or not the
# SDK is installed where the tests run.
NO_SDK = {"anthropic": None}


class OtherTTS(FakeTTS):
    name = "elevenlabs"


class CastingUnavailableTests(KotodamaTestCase):
    def engine_without_sdk(self, **kwargs):
        cfg = self.make_config(inference={"provider": "anthropic"})
        with mock.patch.dict(sys.modules, NO_SDK), self.assertLogs("kotodama.infer", "WARNING") as logs:
            eng = self.make_engine(cfg=cfg, **kwargs)
        self.assertEqual(len(logs.output), 1, logs.output)
        self.assertIn("voice casting is off", logs.output[0])
        return eng

    def test_missing_sdk_turns_casting_off(self):
        eng = self.engine_without_sdk()
        self.assertFalse(eng.inference.enabled)
        self.assertIn("no anthropic SDK", eng.inference.unavailable)
        self.assertIn("server/.venv", eng.inference.unavailable)

    def test_missing_key_turns_casting_off_too(self):
        cfg = self.make_config(inference={"provider": "openai"})
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}), self.assertLogs("kotodama.infer", "WARNING"):
            eng = self.make_engine(cfg=cfg)
        self.assertFalse(eng.inference.enabled)
        self.assertIn("no OpenAI API key", eng.inference.unavailable)

    def test_the_game_is_told_why(self):
        eng = self.engine_without_sdk()
        eng.handle({"t": "hello", "player": "Adventurer"})                   # the addon connects
        eng.handle({"t": "cmd", "name": "voice_reinfer", "npc": "Ayame"})   # //koto recast
        eng.handle({"t": "cmd", "name": "status"})
        told = [m for m in self.say_lines() if m.startswith("voice casting is off: ")]
        self.assertEqual(len(told), 3, self.say_lines())
        self.assertTrue(all("no anthropic SDK" in m for m in told), told)

    def test_switching_provider_keeps_it_off_and_warns_once(self):
        oa, other = FakeTTS(), OtherTTS()
        eng = self.engine_without_sdk(tts=oa, provider_factory={"openai": oa, "elevenlabs": other}.__getitem__)
        with mock.patch.dict(sys.modules, NO_SDK), mock.patch.object(infer.log, "warning") as warn:
            eng.handle({"t": "config", "provider": "elevenlabs"})
        self.assertEqual(eng.tts.name, "elevenlabs")
        self.assertFalse(eng.inference.enabled)
        self.assertIn("no anthropic SDK", eng.inference.unavailable)
        warn.assert_not_called()

    def test_doctor_fails_the_casting_check_with_the_reason(self):
        eng = self.engine_without_sdk()
        results = []
        cli._doctor_casting(eng.cfg, eng, lambda *r: results.append(r))
        self.assertEqual(len(results), 1, results)
        label, good, detail = results[0]
        self.assertEqual((label, good), ("voice casting", False))
        self.assertIn("no anthropic SDK", detail)

    def test_cache_stats_runs_without_the_sdk(self):
        """The command that used to stop with a traceback."""
        cfg = self.tmp / "config.json"
        cfg.write_text(json.dumps({
            "cache": {"dir": str(self.tmp / "cache")},
            "library": {"file": str(self.tmp / "npcs.json"), "defaults": str(self.tmp / "npcs.default.json"),
                        "packs_dir": str(self.tmp / "packs"),
                        "overrides": str(self.tmp / "overrides.json")},
            "inference": {"provider": "anthropic"},
        }), encoding="utf-8")
        real_build = cli.build_engine

        def build_engine(*args, **kwargs):
            eng = real_build(*args, **kwargs)
            self.addCleanup(eng.close)      # the CLI leaves this to the process exiting
            return eng

        out = io.StringIO()
        with mock.patch.dict(sys.modules, NO_SDK), self.assertLogs("kotodama.infer", "WARNING"), \
                mock.patch.object(cli, "build_engine", build_engine), contextlib.redirect_stdout(out):
            rc = cli.main(["-c", str(cfg), "cache", "stats"])
        self.assertEqual(rc, 0)
        self.assertIn("0 lines", out.getvalue())


if __name__ == "__main__":
    unittest.main()
