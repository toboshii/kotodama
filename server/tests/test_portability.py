"""Failure modes that only show on Windows, reproduced with fakes so they
run everywhere: UDP connection resets, files held open, code pages."""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from kotodama import cache as cache_mod, cli, fsutil, protocol
from kotodama.audio import PactlBackend
from kotodama.cache import AudioCache
from kotodama.net import UDPServer

from helpers import KotodamaTestCase


class _FakeSock:
    """recvfrom replays a script: exceptions are raised, tuples returned."""

    def __init__(self, script):
        self.script = list(script)
        self.closed = False

    def settimeout(self, _t):
        pass

    def getsockname(self):
        return ("127.0.0.1", 0)

    def recvfrom(self, _n):
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def sendto(self, _data, _addr):
        pass

    def close(self):
        self.closed = True


class _Engine:
    def __init__(self):
        self.reply = None
        self.handled: list[dict] = []

    def handle(self, msg):
        self.handled.append(msg)


class UDPResetTests(KotodamaTestCase):
    def test_connection_reset_keeps_serving(self):
        engine = _Engine()
        server = UDPServer(self.make_config(listen={"host": "127.0.0.1", "port": 0}), engine)
        server.sock.close()
        addr = ("127.0.0.1", 50000)
        server.sock = _FakeSock([
            ConnectionResetError(10054, "reply hit a closed port"),
            (protocol.encode({"t": "ping"}), addr),
            ConnectionResetError(10054, "again"),
            (protocol.encode({"t": "pong"}), addr),
            OSError("socket closed"),       # what shutdown() causes: the loop ends
        ])
        server.serve_forever()
        self.assertEqual([m["t"] for m in engine.handled], ["ping", "pong"])
        self.assertEqual(server.addr, addr)


class PruneTests(KotodamaTestCase):
    def fill(self, cache: AudioCache, n: int) -> list[str]:
        keys = []
        for i in range(n):
            key = f"{i:02x}" * 32
            keys.append(key)
            w = cache.begin(key, {"text": str(i)}, 24000)
            w.write(b"\x01\x00" * 40_000)   # 80 KB each
            w.commit()
            time.sleep(0.01)
        return keys

    @staticmethod
    def unlink_refusing(path_to_refuse: Path):
        real = os.unlink

        def unlink(path, *a, **k):
            if Path(path) == path_to_refuse:
                raise PermissionError(32, "The process cannot access the file", str(path))
            return real(path, *a, **k)
        return unlink

    def test_a_file_in_use_is_skipped_and_kept(self):
        cache = AudioCache(self.tmp / "c", max_mb=10)
        self.addCleanup(cache.close)
        keys = self.fill(cache, 4)
        cache.max_bytes = 200_000            # prunes to 180 KB: room for two of the four
        with mock.patch.object(cache_mod.os, "unlink", self.unlink_refusing(cache.path_for(keys[0]))):
            removed = cache.prune()
        self.assertEqual(removed, 2)
        # The oldest couldn't be deleted: it stays, with its row, and the
        # next two oldest go instead.
        self.assertTrue(cache.path_for(keys[0]).is_file())
        self.assertIsNotNone(cache.get(keys[0]))
        self.assertFalse(cache.path_for(keys[1]).exists())
        self.assertFalse(cache.path_for(keys[2]).exists())
        self.assertTrue(cache.path_for(keys[3]).is_file())
        self.assertEqual(cache.stats()["files"], 2)

    def test_commit_survives_a_prune_that_hits_a_file_in_use(self):
        cache = AudioCache(self.tmp / "c", max_mb=10)
        self.addCleanup(cache.close)
        keys = self.fill(cache, 2)
        cache.max_bytes = 200_000
        w = cache.begin("ff" * 32, {"text": "new"}, 24000)
        w.write(b"\x01\x00" * 40_000)
        with mock.patch.object(cache_mod.os, "unlink", self.unlink_refusing(cache.path_for(keys[0]))):
            seconds = w.commit()
        self.assertAlmostEqual(seconds, 40_000 / 24000)
        self.assertTrue(cache.path_for("ff" * 32).is_file())
        self.assertTrue(cache.path_for(keys[0]).is_file())

    def test_abort_tolerates_a_held_part_file(self):
        cache = AudioCache(self.tmp / "c", max_mb=10)
        self.addCleanup(cache.close)
        w = cache.begin("ee" * 32, {"text": "x"}, 24000)
        w.write(b"\x00\x00" * 10)
        with mock.patch.object(Path, "unlink", side_effect=PermissionError(32, "in use")):
            w.abort()                       # must not raise
        self.assertIsNone(cache.get("ee" * 32))


class ReplaceFileTests(unittest.TestCase):
    def test_retries_while_the_file_is_held(self):
        calls, sleeps = [], []

        def flaky(src, dst):
            calls.append((src, dst))
            if len(calls) < 3:
                raise PermissionError(5, "Access is denied")

        with mock.patch.object(fsutil.os, "replace", flaky):
            fsutil.replace_file("a.tmp", "a", sleep=sleeps.append)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [fsutil.REPLACE_DELAY] * 2)

    def test_gives_up_after_the_attempts(self):
        calls, sleeps = [], []

        def held(src, dst):
            calls.append(src)
            raise PermissionError(5, "Access is denied")

        with mock.patch.object(fsutil.os, "replace", held), self.assertRaises(PermissionError):
            fsutil.replace_file("a.tmp", "a", attempts=4, sleep=sleeps.append)
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(sleeps), 3)
        self.assertAlmostEqual(fsutil.REPLACE_ATTEMPTS * fsutil.REPLACE_DELAY, 1.0)

    def test_other_errors_are_not_retried(self):
        calls = []

        def missing(src, dst):
            calls.append(src)
            raise FileNotFoundError(2, "No such file")

        with mock.patch.object(fsutil.os, "replace", missing), self.assertRaises(FileNotFoundError):
            fsutil.replace_file("a.tmp", "a", sleep=lambda _s: None)
        self.assertEqual(len(calls), 1)

    def test_atomic_writers_use_it(self):
        # Every atomic write in the sidecar goes through replace_file.
        root = Path(fsutil.__file__).parent
        offenders = [p.name for p in root.glob("*.py")
                     if p.name != "fsutil.py" and "os.replace(" in p.read_text(encoding="utf-8")]
        self.assertEqual(offenders, [])


class DoctorWritableTests(KotodamaTestCase):
    def test_a_folder_we_can_write(self):
        folder = self.tmp / "new" / "cache"
        ok, detail = cli._writable(folder)
        self.assertTrue(ok)
        self.assertEqual(detail, str(folder))
        self.assertEqual(list(folder.iterdir()), [], "the test file is removed")

    def test_a_folder_we_cannot_write(self):
        denied = PermissionError(13, "Permission denied")
        with mock.patch.object(cli.tempfile, "mkstemp", side_effect=denied):
            ok, detail = cli._writable(self.tmp)
            self.assertFalse(ok)
            self.assertIn("Permission denied", detail)
            self.assertNotIn("Program Files", detail)
            with mock.patch.object(cli.sys, "platform", "win32"):
                _, detail = cli._writable(self.tmp)
        self.assertIn("Program Files", detail)

    def test_doctor_checks_the_cache_and_library_folders(self):
        cfg = self.make_config(listen={"host": "127.0.0.1", "port": 0})
        engine = self.make_engine(cfg=cfg)
        out = io.StringIO()

        def no_pactl(*_a, **_k):
            raise FileNotFoundError("pactl")

        with mock.patch.object(cli, "build_engine", lambda *_a, **_k: engine), \
                mock.patch.object(cli, "make_backend", lambda *_a, **_k: PactlBackend(no_pactl)), contextlib.redirect_stdout(out):
            cli.cmd_doctor(cfg, argparse.Namespace(env_file=None, live=False, provider=None))
        text = out.getvalue()
        self.assertIn(f"[ok] cache folder writable: {self.tmp / 'cache'}", text)
        self.assertIn(f"[ok] library folder writable: {self.tmp / 'library'}", text)


class OutputEncodingTests(unittest.TestCase):
    def test_unencodable_names_become_escapes(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252")
        cli._tolerant_output([stream, io.StringIO()])     # StringIO can't reconfigure: skipped
        stream.write("Ru\u2019Lude \u30a2\n")
        stream.flush()
        self.assertEqual(raw.getvalue(), "Ru\u2019Lude \\u30a2\n".encode("cp1252"))

    def test_main_applies_it_first(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ), \
                mock.patch.object(cli, "_tolerant_output") as tolerant, contextlib.redirect_stdout(io.StringIO()):
            config = Path(d) / "config.json"
            config.write_text("{}", encoding="utf-8")
            rc = cli.main(["-c", str(config), "--env-file", str(Path(d) / "none.env"), "providers"])
        self.assertEqual(rc, 0)
        tolerant.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
