"""Single-key controls for the sidecar's terminal: R restarts, Q quits.

Ctrl+C is awkward on the Steam Deck's on-screen keyboard, so when the
sidecar runs in the foreground of a terminal, stdin is put in cbreak mode
(each key arrives without Enter; echo off; Ctrl+C still works) and read on
a background thread. Under systemd, with stdin redirected, or in the
background (`&`), there is no terminal to own and this does nothing.
"""

from __future__ import annotations

import logging
import os
import re
import select
import sys
import threading
from typing import Any, Callable

try:
    import termios
    import tty
except ImportError:  # pragma: no cover - not POSIX
    termios = tty = None  # type: ignore[assignment]

log = logging.getLogger("kotodama.console")

KEYS = {"r": "restart", "q": "quit"}
# CSI (ESC [ ... final), SS3 (ESC O x) and Alt+key (ESC x) sequences.
ESCAPE_SEQ = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|O.?|.?)", re.DOTALL)


def _in_foreground(fd: int) -> bool:
    """A background job that changes or reads the terminal gets stopped by
    SIGTTOU/SIGTTIN, so only take the keyboard when we own it."""
    try:
        return os.tcgetpgrp(fd) == os.getpgrp()
    except OSError:
        return False


class KeyControls:
    def __init__(self, on_action: Callable[[str], None], stream: Any = None):
        self.on_action = on_action
        self.stream = stream if stream is not None else sys.stdin
        self._fd: int | None = None
        self._saved: list[Any] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        """Take the keyboard. False (and nothing changed) without a usable terminal."""
        if termios is None or self.stream is None:
            return False
        try:
            fd = self.stream.fileno()
        except (AttributeError, ValueError, OSError):
            return False
        if not os.isatty(fd) or not _in_foreground(fd):
            return False
        try:
            self._saved = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        except termios.error:
            self._saved = None
            return False
        self._fd = fd
        self._thread = threading.Thread(target=self._read, daemon=True, name="keys")
        self._thread.start()
        return True

    def stop(self) -> None:
        """Hand the terminal back exactly as it was. Safe to call twice."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(1.0)
        if self._fd is not None and self._saved is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            except termios.error:
                pass
        self._saved = None
        self._fd = None

    def _read(self) -> None:
        assert self._fd is not None
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self._fd], [], [], 0.25)
                if not ready:
                    continue
                data = os.read(self._fd, 64)
            except (OSError, ValueError):
                return
            if not data:                     # terminal hung up
                return
            # Drop arrow/function/Alt keys first: F2 is ESC O Q, which must not quit.
            text = ESCAPE_SEQ.sub("", data.decode("ascii", "ignore"))
            for ch in text.lower():
                action = KEYS.get(ch)
                if action:
                    self.on_action(action)
                    return                   # one action per run; the server is going down


def restart_argv() -> list[str]:
    """The command line this interpreter was started with, flags included
    (python3 -B, a venv's python, or a shebang all come back the same way)."""
    orig = getattr(sys, "orig_argv", None)
    return list(orig) if orig else [sys.executable, *sys.argv]


def reexec(execv: Callable[[str, list[str]], Any] = os.execv) -> None:
    """Replace this process with a fresh copy, which reloads code and config.
    Sockets are non-inheritable, so the UDP port is free for the new copy."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (AttributeError, OSError, ValueError):
            pass
    execv(sys.executable, restart_argv())
