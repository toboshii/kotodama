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

    def test_supervised_r_asks_the_supervisor_instead_of_exec(self):
        with mock.patch.dict(os.environ, {console.SUPERVISED_ENV: "1"}):
            rc, execs = self._serve(b"r")
        self.assertEqual((rc, execs), (console.RESTART_EXIT, []))

    def test_supervised_q_still_quits(self):
        with mock.patch.dict(os.environ, {console.SUPERVISED_ENV: "1"}):
            rc, execs = self._serve(b"q")
        self.assertEqual((rc, execs), (0, []))


class _FakeMsvcrt:
    """Console keys as msvcrt.getwch returns them, one character at a time."""

    def __init__(self, keys: str = ""):
        self.lock = threading.Lock()
        self.pending = list(keys)
        self.reads = 0

    def type(self, keys: str) -> None:
        with self.lock:
            self.pending.extend(keys)

    def kbhit(self) -> bool:
        with self.lock:
            return bool(self.pending)

    def getwch(self) -> str:
        with self.lock:
            self.reads += 1
            return self.pending.pop(0)


class _Console:
    def fileno(self) -> int:
        return 0


class WindowsKeyTests(unittest.TestCase):
    def controls(self, keys: str, *, console_ok: bool = True, msvcrt=None):
        actions: list[str] = []
        fake = msvcrt if msvcrt is not None else _FakeMsvcrt(keys)
        kc = KeyControls(actions.append, _Console(), platform="win32", msvcrt_mod=fake,
                         is_console=lambda fd: console_ok)
        self.addCleanup(kc.stop)
        return kc, actions, fake

    def test_r_and_q_act_without_enter(self):
        for keys, action in (("r", "restart"), ("R", "restart"), ("q", "quit"), ("Q", "quit")):
            kc, actions, _ = self.controls(keys)
            self.assertTrue(kc.start())
            self.assertTrue(wait_for(lambda: actions), keys)
            kc.stop()
            self.assertEqual(actions, [action])

    def test_two_part_keys_never_count_as_r_or_q(self):
        # Insert, Page Down, F1, Delete, then other letters: nothing happens.
        kc, actions, fake = self.controls("\xe0R\xe0Q\x00;\xe0Sxyz")
        self.assertTrue(kc.start())
        self.assertTrue(wait_for(lambda: not fake.pending))
        time.sleep(0.1)
        self.assertEqual(actions, [])
        fake.type("q")
        self.assertTrue(wait_for(lambda: actions))
        self.assertEqual(actions, ["quit"])

    def test_one_action_then_the_reader_stops(self):
        kc, actions, fake = self.controls("rq")
        self.assertTrue(kc.start())
        self.assertTrue(wait_for(lambda: actions))
        time.sleep(0.1)
        self.assertEqual(actions, ["restart"])
        self.assertEqual(fake.pending, ["q"], "the server is going down: nothing more is read")

    def test_no_console_means_no_key_handling(self):
        kc, actions, fake = self.controls("r", console_ok=False)
        self.assertFalse(kc.start())
        time.sleep(0.05)
        self.assertEqual((actions, fake.reads), ([], 0))

    def test_without_msvcrt_nothing_happens(self):
        actions: list[str] = []
        with mock.patch.object(console, "_load_msvcrt", return_value=None):
            kc = KeyControls(actions.append, _Console(), platform="win32", is_console=lambda fd: True)
            self.assertFalse(kc.start())

    def test_stop_ends_the_reader(self):
        kc, actions, fake = self.controls("")
        self.assertTrue(kc.start())
        kc.stop()
        self.assertFalse(kc._thread.is_alive())
        fake.type("r")
        time.sleep(0.1)
        self.assertEqual(actions, [])


class _Child:
    def __init__(self, code: int, interrupts: int = 0):
        self.code, self.interrupts, self.waits = code, interrupts, 0

    def wait(self):
        self.waits += 1
        if self.interrupts:
            self.interrupts -= 1
            raise KeyboardInterrupt
        return self.code


class SupervisorTests(unittest.TestCase):
    def run_supervisor(self, children, **kw):
        spawned: list = []
        queue = list(children)

        def popen(cmd, env):
            spawned.append((cmd, env))
            child = queue.pop(0)
            if isinstance(child, BaseException):
                raise child
            return child

        rc = cli.supervise(popen=popen, environ={"PATH": "x"}, **kw)
        return rc, spawned

    def test_restart_code_runs_a_fresh_copy(self):
        with mock.patch.object(sys, "orig_argv", ["py", "-B", "kotodama_server.py", "serve"], create=True):
            rc, spawned = self.run_supervisor([_Child(console.RESTART_EXIT), _Child(console.RESTART_EXIT),
                                               _Child(0)])
        self.assertEqual(rc, 0)
        self.assertEqual(len(spawned), 3)
        cmd, env = spawned[0]
        self.assertEqual(cmd, [sys.executable, "-B", "kotodama_server.py", "serve"])
        self.assertEqual(env, {"PATH": "x", console.SUPERVISED_ENV: "1"})

    def test_any_other_exit_is_passed_on(self):
        rc, spawned = self.run_supervisor([_Child(3)], argv=["python", "k.py"])
        self.assertEqual((rc, len(spawned)), (3, 1))
        self.assertEqual(spawned[0][0], ["python", "k.py"])

    def test_ctrl_c_waits_for_the_child_to_finish(self):
        child = _Child(0, interrupts=2)
        rc, spawned = self.run_supervisor([child], argv=["python", "k.py"])
        self.assertEqual((rc, len(spawned), child.waits), (0, 1, 3))

    def test_a_child_that_cannot_start(self):
        with self.assertLogs(level="ERROR"):
            rc, _ = self.run_supervisor([OSError("no python")], argv=["python", "k.py"])
        self.assertEqual(rc, 1)

    def test_only_serve_on_windows_outside_a_supervisor(self):
        need = cli._needs_supervisor
        self.assertTrue(need("serve", "win32", {}))
        self.assertTrue(need(None, "win32", {}))
        self.assertFalse(need("doctor", "win32", {}))
        self.assertFalse(need("serve", "linux", {}))
        self.assertFalse(need("serve", "win32", {console.SUPERVISED_ENV: "1"}))

    def test_main_supervises_before_loading_anything(self):
        env = {k: v for k, v in os.environ.items() if k != console.SUPERVISED_ENV}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(cli.sys, "platform", "win32"), \
                mock.patch.object(cli, "supervise", return_value=7) as supervise, \
                mock.patch.object(cli.envfile, "load") as load, mock.patch.object(cli.Config, "load") as cfg:
            self.assertEqual(cli.main(["serve"]), 7)
        supervise.assert_called_once_with()
        load.assert_not_called()
        cfg.assert_not_called()


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
