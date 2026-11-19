"""~/.config/kotodama.env loading for sidecars started from a terminal."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kotodama import cli, envfile


class ParseTests(unittest.TestCase):
    def setUp(self):
        # Whatever server/kotodama.env this checkout has must not leak in.
        patcher = mock.patch.object(envfile, "LOCAL", Path(tempfile.gettempdir()) / "kotodama-tests-no-local.env")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_systemd_and_shell_styles(self):
        text = "\n".join([
            "# a comment", "; another", "",
            "OPENAI_API_KEY=sk-plain",
            "  export REPLICATE_API_TOKEN = \"r8_quoted\"  ",
            "SINGLE='keep $this \\n literal'",
            'ESCAPED="a\\"b\\\\c"',
            "WITH_EQUALS=abc=def==",
            "not a line", "1BAD=x", "=nothing",
            "KOTODAMA_ENV_LOADED=evil",
        ])
        self.assertEqual(envfile.parse(text), {
            "OPENAI_API_KEY": "sk-plain",
            "REPLICATE_API_TOKEN": "r8_quoted",
            "SINGLE": "keep $this \\n literal",
            "ESCAPED": 'a"b\\c',
            "WITH_EQUALS": "abc=def==",
        })

    def test_default_location(self):
        self.assertEqual(envfile.default_path({"XDG_CONFIG_HOME": "/x/conf"}), Path("/x/conf/kotodama.env"))
        self.assertEqual(envfile.default_path({}, platform="linux"), Path.home() / ".config" / "kotodama.env")
        self.assertEqual(envfile.default_path({"KOTODAMA_ENV_FILE": "/etc/k.env", "XDG_CONFIG_HOME": "/x"}),
                         Path("/etc/k.env"))


    def test_windows_location(self):
        self.assertEqual(envfile.default_path({"APPDATA": "C:/Users/a/AppData/Roaming"}, platform="win32"),
                         Path("C:/Users/a/AppData/Roaming") / "Kotodama" / "kotodama.env")
        self.assertEqual(envfile.default_path({}, platform="win32"),
                         Path.home() / "AppData" / "Roaming" / "Kotodama" / "kotodama.env")
        # The explicit choices still come first.
        self.assertEqual(envfile.default_path({"KOTODAMA_ENV_FILE": "D:/k.env", "APPDATA": "C:/a"},
                                              platform="win32"), Path("D:/k.env"))
        self.assertEqual(envfile.default_path({"XDG_CONFIG_HOME": "C:/xdg", "APPDATA": "C:/a"},
                                              platform="win32"), Path("C:/xdg/kotodama.env"))
        self.assertTrue(envfile.shown_default("win32").endswith("%APPDATA%\\Kotodama\\kotodama.env"))
        self.assertTrue(envfile.shown_default("linux").endswith("~/.config/kotodama.env"))

    def test_local_file_takes_precedence_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "kotodama.env"
            with mock.patch.object(envfile, "LOCAL", local):
                self.assertEqual(envfile.default_path({"XDG_CONFIG_HOME": "/x"}), Path("/x/kotodama.env"))
                local.write_text("OPENAI_API_KEY=x\n", encoding="utf-8")
                self.assertEqual(envfile.default_path({"XDG_CONFIG_HOME": "/x"}), local)
                self.assertEqual(envfile.default_path({"APPDATA": "C:/a"}, platform="win32"), local)
                self.assertEqual(envfile.default_path({}, platform="linux"), local)
                # The explicit override still wins.
                self.assertEqual(envfile.default_path({"KOTODAMA_ENV_FILE": "/etc/k.env"}), Path("/etc/k.env"))


class LoadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "kotodama.env"

    def write(self, text: str, mode: int = 0o600) -> None:
        self.path.write_text(text, encoding="utf-8")
        self.path.chmod(mode)

    def test_fills_gaps_and_never_overrides_the_shell(self):
        self.write("OPENAI_API_KEY=from-file\nREPLICATE_API_TOKEN=r8_file\n")
        env = {"OPENAI_API_KEY": "from-shell"}
        _, names = envfile.load(self.path, env)
        self.assertEqual(names, ["REPLICATE_API_TOKEN"])
        self.assertEqual(env["OPENAI_API_KEY"], "from-shell")
        self.assertEqual(env["REPLICATE_API_TOKEN"], "r8_file")

    def test_restart_picks_up_edits_and_removals(self):
        self.write("REPLICATE_API_TOKEN=r8_old\nELEVENLABS_API_KEY=xi\n")
        env = {"OPENAI_API_KEY": "from-shell"}
        envfile.load(self.path, env)
        # The user edits the file and presses R: the process re-execs with
        # this environment and reads the file again.
        self.write("REPLICATE_API_TOKEN=r8_new\nOPENAI_API_KEY=from-file\n")
        _, names = envfile.load(self.path, env)
        self.assertEqual(env["REPLICATE_API_TOKEN"], "r8_new", "a value from the file can be updated")
        self.assertNotIn("ELEVENLABS_API_KEY", env, "a key removed from the file is gone")
        self.assertEqual(env["OPENAI_API_KEY"], "from-shell", "the shell's value still wins")
        self.assertEqual(names, ["REPLICATE_API_TOKEN"])
        self.path.unlink()
        envfile.load(self.path, env)
        self.assertNotIn("REPLICATE_API_TOKEN", env)
        self.assertNotIn(envfile.MARKER, env)

    @unittest.skipIf(sys.platform == "win32", "Windows mode bits always look world-readable")
    def test_missing_file_is_fine_and_loose_permissions_warn(self):
        env: dict[str, str] = {}
        self.assertEqual(envfile.load(self.path, env)[1], [])
        self.write("REPLICATE_API_TOKEN=r8\n", mode=0o644)
        with self.assertLogs("kotodama.env", "WARNING") as logs:
            envfile.load(self.path, {})
        self.assertIn("chmod 600", logs.output[0])
        self.assertNotIn("r8", "".join(logs.output).replace(str(self.path), ""), "values are never logged")


    def test_no_permission_warning_on_windows(self):
        self.write("REPLICATE_API_TOKEN=r8\n", mode=0o644)
        with self.assertNoLogs("kotodama.env", "WARNING"):
            _, names = envfile.load(self.path, {}, platform="win32")
        self.assertEqual(names, ["REPLICATE_API_TOKEN"])

    def test_a_byte_order_mark_does_not_hide_the_first_key(self):
        # Notepad can save UTF-8 with a BOM.
        self.path.write_bytes("\ufeffINWORLD_API_KEY=abc\r\nGEMINI_API_KEY=def\r\n".encode("utf-8"))
        env: dict[str, str] = {}
        _, names = envfile.load(self.path, env, platform="win32")
        self.assertEqual(names, ["GEMINI_API_KEY", "INWORLD_API_KEY"])
        self.assertEqual(env["INWORLD_API_KEY"], "abc")
        self.assertEqual(env["GEMINI_API_KEY"], "def")


class CliTests(unittest.TestCase):
    def test_console_run_sees_keys_from_the_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            env_file = tmp / "kotodama.env"
            env_file.write_text("REPLICATE_API_TOKEN=r8_from_file\n", encoding="utf-8")
            env_file.chmod(0o600)
            cfg = tmp / "config.json"
            cfg.write_text(json.dumps({"cache": {"dir": str(tmp / "cache")}}), encoding="utf-8")
            clean = {k: v for k, v in os.environ.items()
                     if k not in ("REPLICATE_API_TOKEN", envfile.MARKER, "KOTODAMA_CONFIG")}
            clean["KOTODAMA_ENV_FILE"] = str(env_file)
            with mock.patch.dict(os.environ, clean, clear=True):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = cli.main(["-c", str(cfg), "providers"])
                self.assertEqual(rc, 0)
                minimax = next(line for line in out.getvalue().splitlines() if " minimax-replicate " in f" {line} ")
                self.assertNotIn("no API key", minimax)

                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    cli.main(["-c", str(cfg), "--env-file", str(tmp / "missing.env"), "providers"])
                minimax = next(line for line in out.getvalue().splitlines() if " minimax-replicate " in f" {line} ")
                self.assertIn("no API key", minimax, "--env-file replaces the default, and drops its keys")


if __name__ == "__main__":
    unittest.main()
