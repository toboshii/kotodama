"""R/Q keyboard controls for the sidecar terminal, and the restart handshake."""

from __future__ import annotations

import argparse
import io
import os
import sys
import threading
import time
import unittest
from unittest import mock

from kotodama import cli, console
from kotodama.console import KeyControls

from helpers import KotodamaTestCase

try:
    import pty
    import termios
except ImportError:  # pragma: no cover
    pty = termios = None


def wait_for(cond, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


@unittest.skipIf(pty is None, "needs a POSIX pty")
class _PtyCase(KotodamaTestCase):
    def setUp(self):
        super().setUp()
        self.master, self.slave = pty.openpty()
        self.stream = os.fdopen(self.slave, "rb", buffering=0)
        self.addCleanup(self.stream.close)
        self.addCleanup(os.close, self.master)
        # A pty opened by the test isn't our controlling terminal, so pretend
        # we're the foreground job (the real check is covered separately).
        patcher = mock.patch.object(console, "_in_foreground", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def lflag(self) -> int:
        return termios.tcgetattr(self.slave)[3]

    def keys_live(self) -> bool:
        return not (self.lflag() & termios.ICANON)

    def type(self, data: bytes) -> None:
        os.write(self.master, data)


class KeyControlsTests(_PtyCase):
    def test_single_keys_without_enter_and_terminal_restored(self):
        before = termios.tcgetattr(self.slave)
        actions: list[str] = []
        keys = KeyControls(actions.append, self.stream)
        self.assertTrue(keys.start())
        lflag = self.lflag()
        self.assertFalse(lflag & termios.ICANON, "keys arrive without Enter")
        self.assertFalse(lflag & termios.ECHO)
        self.assertTrue(lflag & termios.ISIG, "Ctrl+C still works")

        self.type(b"x")
        self.type(b"x\x1bOQ\x1b[A\x1bq")          # stray key + F2 + Up + Alt+Q in one read
        time.sleep(0.4)
        self.assertEqual(actions, [], "function keys and other letters do nothing")

        self.type(b"R")
        self.assertTrue(wait_for(lambda: actions == ["restart"]))
        keys.stop()
        keys.stop()                                # idempotent
        self.assertEqual(termios.tcgetattr(self.slave), before, "terminal handed back unchanged")

    def test_q_quits(self):
        actions: list[str] = []
        keys = KeyControls(actions.append, self.stream)
        self.assertTrue(keys.start())
        self.addCleanup(keys.stop)
        self.type(b"q")
        self.assertTrue(wait_for(lambda: actions == ["quit"]))


class NoTerminalTests(unittest.TestCase):
    def test_no_terminal_means_no_key_handling(self):
        r, w = os.pipe()
        self.addCleanup(os.close, w)
        with os.fdopen(r, "rb", buffering=0) as piped:       # systemd / redirected stdin
            self.assertFalse(KeyControls(lambda _a: None, piped).start())
        self.assertFalse(KeyControls(lambda _a: None, io.StringIO()).start())

    @unittest.skipIf(pty is None, "needs a POSIX pty")
    def test_not_foreground_leaves_terminal_alone(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        with os.fdopen(slave, "rb", buffering=0) as stream:
            before = termios.tcgetattr(slave)
            # Not our controlling terminal, like a background job's: hands off.
            self.assertFalse(KeyControls(lambda _a: None, stream).start())
            self.assertEqual(termios.tcgetattr(slave), before)

    def test_reexec_reruns_the_same_command_line(self):
        calls = []
        with mock.patch.object(sys, "orig_argv", ["python3", "-B", "kotodama_server.py", "serve"], create=True):
            console.reexec(lambda path, argv: calls.append((path, argv)))
        self.assertEqual(calls, [(sys.executable, ["python3", "-B", "kotodama_server.py", "serve"])])


class ServeKeysTests(_PtyCase):
    def _serve(self, key: bytes):
        cfg = self.make_config(listen={"host": "127.0.0.1", "port": 0})
        engine = self.make_engine(cfg=cfg)
        execs: list = []

        def press():
            if wait_for(self.keys_live):
                self.type(key)

        threading.Thread(target=press, daemon=True).start()
        rc = cli.cmd_serve(cfg, argparse.Namespace(), stdin=self.stream,
                           execv=lambda path, argv: execs.append((path, argv)), engine=engine)
        return rc, execs

    def test_q_stops_the_server_without_restarting(self):
        before = termios.tcgetattr(self.slave)
        rc, execs = self._serve(b"q")
        self.assertEqual((rc, execs), (0, []))
        self.assertEqual(termios.tcgetattr(self.slave), before)

    def test_r_shuts_down_cleanly_then_re_executes(self):
        before = termios.tcgetattr(self.slave)
        rc, execs = self._serve(b"r")
        self.assertEqual(execs, [(sys.executable, console.restart_argv())])
        self.assertEqual(termios.tcgetattr(self.slave), before, "terminal restored before exec")


class RestartHandshakeTests(KotodamaTestCase):
    def test_fresh_sidecar_asks_for_hello_until_it_gets_one(self):
        eng = self.make_engine()
        eng.handle({"t": "ping"})
        self.assertEqual(self.replies[-1], {"t": "pong", "want_hello": True})
        eng.handle({"t": "hello", "player": "Adventurer"})
        eng.handle({"t": "ping"})
        self.assertEqual(self.replies[-1], {"t": "pong"})


if __name__ == "__main__":
    unittest.main()
