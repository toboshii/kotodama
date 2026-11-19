"""server/kotodama.sh: which Python it picks, and what it hands over."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kotodama.config import SERVER_DIR

# Stands in for Python: prints its name, the folder it runs in, then its arguments.
FAKE_PYTHON = '#!/bin/sh\nprintf "%s\\n" "{name}" "$(pwd -P)" "$@"\n'


@unittest.skipIf(sys.platform == "win32", "kotodama.sh is the Linux launcher")
class LauncherTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.server = self.root / "server"
        self.server.mkdir()
        shutil.copy(SERVER_DIR / "kotodama.sh", self.server)
        self.bin = self.root / "bin"
        self.fake(self.bin / "python3", "path-python3")
        self.cwd = self.root / "elsewhere"
        self.cwd.mkdir()

    def fake(self, path: Path, name: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(FAKE_PYTHON.format(name=name), encoding="utf-8")
        path.chmod(0o755)

    def run_launcher(self, *args: str, launcher: Path | None = None,
                     path: str | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PATH"] = path if path is not None else f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        return subprocess.run([shutil.which("sh") or "/bin/sh", str(launcher or self.server / "kotodama.sh"), *args],
                              cwd=self.cwd, env=env, capture_output=True, text=True, timeout=10)

    def test_uses_python3_without_a_venv(self):
        out = self.run_launcher("test", "--name", "Ayame", "Welcome to Norg.")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.splitlines(), [
            "path-python3", str(self.cwd), str(self.server / "kotodama_server.py"),
            "test", "--name", "Ayame", "Welcome to Norg.",
        ])

    def test_prefers_the_venv_and_stays_in_the_callers_folder(self):
        self.fake(self.server / ".venv" / "bin" / "python", "venv-python")
        out = self.run_launcher("library", "import", "friends-npcs.json")
        self.assertEqual(out.returncode, 0, out.stderr)
        # The relative path is passed through untouched and the folder is the
        # caller's, so it resolves where the user ran the command.
        self.assertEqual(out.stdout.splitlines(), [
            "venv-python", str(self.cwd), str(self.server / "kotodama_server.py"),
            "library", "import", "friends-npcs.json",
        ])

    def test_finds_its_folder_through_a_symlink(self):
        self.fake(self.server / ".venv" / "bin" / "python", "venv-python")
        link = self.bin / "kotodama"
        link.symlink_to(self.server / "kotodama.sh")
        out = self.run_launcher("doctor", launcher=link)
        self.assertEqual(out.returncode, 0, out.stderr)
        name, _, script, *_ = out.stdout.splitlines()
        self.assertEqual((name, script), ("venv-python", str(self.server / "kotodama_server.py")))

    def test_says_what_is_missing_without_python(self):
        # Only the tools the launcher itself needs: no python3 anywhere on PATH.
        tools = self.root / "tools"
        tools.mkdir()
        for tool in ("dirname", "readlink"):
            (tools / tool).symlink_to(shutil.which(tool))
        out = self.run_launcher("doctor", path=str(tools))
        self.assertEqual(out.returncode, 1)
        self.assertIn("needs Python 3.10 or newer", out.stderr)
        self.assertEqual(out.stdout, "")


if __name__ == "__main__":
    unittest.main()
