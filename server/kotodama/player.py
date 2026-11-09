"""Audio playback through an external player process (instantly killable).

Every line, cached or freshly synthesized, is played as a raw PCM stream so
the sidecar controls gain itself; the player is always run at full volume."""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("kotodama.player")


class PlayerHandle:
    """One playing line. Created by Player; PCM is written to stdin."""

    def __init__(self, proc: subprocess.Popen | None):
        self.proc = proc
        self._dead = proc is None

    def write(self, data: bytes) -> bool:
        if self._dead or self.proc is None or self.proc.stdin is None:
            return False
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, ValueError, OSError):
            self._dead = True
            return False

    def finish_input(self) -> None:
        """No more PCM: let the player drain what it has and exit."""
        if self.proc is not None and self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass

    def wait(self, timeout: float | None = None) -> bool:
        """True once the process has exited."""
        if self.proc is None:
            return True
        try:
            self.proc.wait(timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def kill(self) -> None:
        self._dead = True
        proc = self.proc
        if proc is None:
            return
        try:
            proc.kill()
        except OSError:
            pass
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
        # Reap in the background so kill() returns immediately.
        threading.Thread(target=proc.wait, daemon=True).start()


def _expand(template: list[str], **values: Any) -> list[str]:
    return [str(part).format(**values) for part in template]


class Player:
    """Chooses pw-play, paplay or ffplay (or a configured command)."""

    def __init__(self, command: dict[str, list[str]] | None = None, *, null: bool = False):
        self.null = null
        self.kind = "null" if null else None
        self.stream_cmd: list[str] | None = None
        if null:
            return
        if command:
            self.kind = "custom"
            self.stream_cmd = command.get("stream")
        else:
            self._autodetect()

    def _autodetect(self) -> None:
        if shutil.which("pw-play"):
            self.kind = "pw-play"
            self.stream_cmd = ["pw-play", "--raw", "--rate", "{rate}", "--channels", "1", "--format", "s16",
                               "--volume", "{volume_float}", "--latency", "60ms", "-"]
        elif shutil.which("paplay"):
            self.kind = "paplay"
            self.stream_cmd = ["paplay", "--raw", "--rate={rate}", "--channels=1", "--format=s16le",
                               "--volume={volume_pulse}"]
        elif shutil.which("ffplay"):
            self.kind = "ffplay"
            self.stream_cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-volume", "{volume}",
                               "-f", "s16le", "-ar", "{rate}", "-ac", "1", "-i", "pipe:0"]
        else:
            log.error("no audio player found (install pipewire-tools, pulseaudio-utils or ffmpeg)")

    @property
    def available(self) -> bool:
        return self.null or bool(self.stream_cmd)

    @staticmethod
    def _volumes(volume: int) -> dict[str, Any]:
        v = max(0, min(100, int(volume)))
        return {"volume": v, "volume_float": f"{v / 100:.2f}", "volume_pulse": int(65536 * v / 100)}

    def _spawn(self, argv: list[str], stdin: bool) -> PlayerHandle:
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            return PlayerHandle(proc)
        except OSError as exc:
            log.error("could not start player %s: %s", argv[0], exc)
            return PlayerHandle(None)

    def start_stream(self, volume: int, rate: int) -> PlayerHandle:
        if self.null or not self.stream_cmd:
            return PlayerHandle(None)
        return self._spawn(_expand(self.stream_cmd, rate=rate, **self._volumes(volume)), stdin=True)
