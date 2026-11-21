"""Loudness and ducking.

Limiter      software gain with a peak limiter, so the voice can be made much
             louder than the TTS model's native level without clipping.
Ducker       lowers the game's own audio while a line is spoken and restores
             it afterwards: its PipeWire/PulseAudio stream through pactl
             (PactlBackend), or on Windows its per-app session volume
             (winaudio.CoreAudioBackend).
"""

from __future__ import annotations

import array
import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .fsutil import replace_file

log = logging.getLogger("kotodama.audio")

# -- gain ------------------------------------------------------------------

def db_to_gain(db: float) -> float:
    return 10 ** (db / 20.0)


class Limiter:
    """Applies gain to 16-bit mono PCM without letting loud passages clip.

    A plain boost distorts as soon as speech peaks hit full scale. This
    limiter instead pulls its gain down quickly when a peak would exceed the
    ceiling and lets it recover slowly, so the waveform keeps its shape and
    the whole line simply gets louder. A tanh soft clip catches the few
    samples of overshoot during the attack.

    One instance per line: it carries its gain envelope from chunk to chunk.
    """

    def __init__(self, rate: int = 24000, ceiling: float = 0.92,
                 attack_ms: float = 0.5, release_ms: float = 80.0):
        self.ceiling = ceiling
        self._attack = 1.0 - math.exp(-1.0 / (rate * attack_ms / 1000.0))
        self._release = 1.0 - math.exp(-1.0 / (rate * release_ms / 1000.0))
        self._g = 1.0   # extra gain reduction, 1.0 = none

    def process(self, pcm: bytes, gain: float) -> bytes:
        if not pcm:
            return pcm
        samples = array.array("h")
        samples.frombytes(pcm[: len(pcm) // 2 * 2])
        if sys.byteorder == "big":
            samples.byteswap()
        k = gain / 32768.0
        ceil = self.ceiling
        head = 1.0 - ceil
        attack, release = self._attack, self._release
        tanh = math.tanh
        g = self._g
        out = array.array("h")
        append = out.append
        for s in samples:
            x = s * k
            ax = -x if x < 0 else x
            if ax * g > ceil:
                g += (ceil / ax - g) * attack
            elif g < 1.0:
                g += (1.0 - g) * release
            y = x * g
            ay = -y if y < 0 else y
            if ay > ceil:
                ay = ceil + head * tanh((ay - ceil) / head)
            v = int(ay * 32767.0 + 0.5)
            append(-v if y < 0 else v)
        self._g = g
        if sys.byteorder == "big":
            out.byteswap()
        return out.tobytes()


# -- sink-input parsing ----------------------------------------------------

@dataclass
class SinkInput:
    """A playing stream: a pactl sink input (int id), or a Windows audio
    session (its instance identifier as id). volumes: 65536 = 100%."""
    id: Any
    volumes: list[int]
    muted: bool = False
    corked: bool = False
    props: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        p = self.props
        return (p.get("application.name") or p.get("application.process.binary") or "?")


_BLOCK_START = re.compile(r"^Sink Input #(\d+)\s*$", re.M)
_PROP = re.compile(r'^\s+([\w.\-]+) = "(.*)"\s*$')
_RAW_VOL = re.compile(r"[\w\-]+:\s*(\d+)\s*/")


def parse_sink_inputs(text: str) -> list[SinkInput]:
    """Parse `pactl list sink-inputs` (run with LC_ALL=C)."""
    starts = list(_BLOCK_START.finditer(text))
    streams: list[SinkInput] = []
    for i, m in enumerate(starts):
        block = text[m.end(): starts[i + 1].start() if i + 1 < len(starts) else len(text)]
        vol_line = re.search(r"^\s*Volume:(.*)$", block, re.M)
        volumes = [int(v) for v in _RAW_VOL.findall(vol_line.group(1))] if vol_line else []
        props = {}
        for line in block.splitlines():
            pm = _PROP.match(line)
            if pm:
                props[pm.group(1)] = pm.group(2)
        streams.append(SinkInput(
            id=int(m.group(1)),
            volumes=volumes,
            muted=bool(re.search(r"^\s*Mute:\s*yes", block, re.M)),
            corked=bool(re.search(r"^\s*Corked:\s*yes", block, re.M)),
            props=props,
        ))
    return streams


MATCH_KEYS = ("application.name", "application.process.binary", "pipewire.access.portal.app_id",
              "application.id")
OWN_PLAYERS = {"pw-play", "pw-cat", "paplay", "ffplay", "kotodama"}
# props key a backend may set: an identifier for the stream that stays the
# same across runs of the same program (Windows session identifiers).
SESSION_KEY = "kotodama.session"


def _program(name: str) -> str:
    name = name.lower()
    return name[:-4] if name.endswith(".exe") else name


def is_game_stream(stream: SinkInput, patterns: list[str]) -> bool:
    props = stream.props
    for key in ("application.name", "application.process.binary"):
        if _program(props.get(key, "")) in OWN_PLAYERS:   # ffplay.exe is ffplay
            return False
    wanted = [p.lower() for p in patterns if p]
    return any(w in props.get(key, "").lower() for key in MATCH_KEYS for w in wanted)


def run_pactl(*args: str) -> str:
    result = subprocess.run(
        ["pactl", *args], capture_output=True, text=True, timeout=3,
        env={**os.environ, "LC_ALL": "C", "LANG": "C"},
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"pactl exited {result.returncode}")
    return result.stdout


# -- backends --------------------------------------------------------------
#
# A backend lists the playing streams and sets one stream's volumes:
#   list_streams() -> list[SinkInput]   (raises DuckingUnavailable if it can't work here)
#   set_volumes(id, volumes) -> bool    (False if the stream is gone)
#   close()
# plus `name` and `missing_hint` for messages.

class DuckingUnavailable(RuntimeError):
    """The system's audio control isn't there (no pactl, no Core Audio)."""


class PactlBackend:
    name = "pactl"
    missing_hint = "`pactl` not found (install pulseaudio-utils or pipewire-pulse)"

    def __init__(self, runner: Callable[..., str] | None = None):
        self.run = runner or run_pactl

    def list_streams(self) -> list[SinkInput]:
        try:
            return parse_sink_inputs(self.run("list", "sink-inputs"))
        except FileNotFoundError as exc:
            raise DuckingUnavailable(self.missing_hint) from exc

    def set_volumes(self, sid: Any, volumes: list[int]) -> bool:
        try:
            self.run("set-sink-input-volume", str(sid), *[str(max(0, v)) for v in volumes])
            return True
        except (RuntimeError, subprocess.SubprocessError, OSError):
            return False   # the stream went away

    def close(self) -> None:
        pass


def make_backend(platform: str | None = None) -> Any:
    """Core Audio on Windows, pactl everywhere else."""
    if (platform or sys.platform) == "win32":
        from .winaudio import CoreAudioBackend
        return CoreAudioBackend()
    return PactlBackend()


# -- ducking ---------------------------------------------------------------

class Ducker:
    """Smoothly lowers matching game streams while any line is playing.

    acquire()/release() bracket each playing line. The volume drops quickly
    when a line starts, and rises slowly only after `hold_ms` of silence so a
    run of lines in a cutscene doesn't pump the music up and down. Original
    volumes are written to a state file so a crash can't leave the game
    quiet: the next start (or `audio restore`) puts them back."""

    def __init__(self, *, enabled: bool = True, level: int = 40, match: list[str] | None = None,
                 attack_ms: int = 120, release_ms: int = 700, hold_ms: int = 500,
                 state_path: Path | None = None,
                 runner: Callable[..., str] | None = None,
                 backend: Any = None,
                 notify: Callable[[str], None] | None = None):
        self.enabled = enabled
        self.level = max(0, min(100, int(level))) / 100.0
        self.match = list(match or [])
        self.attack_s = max(0.0, attack_ms / 1000.0)
        self.release_s = max(0.0, release_ms / 1000.0)
        self.hold_s = max(0.0, hold_ms / 1000.0)
        self.state_path = Path(state_path) if state_path else None
        self.backend = backend if backend is not None else PactlBackend(runner)
        self.notify = notify or (lambda _m: None)

        self._cond = threading.Condition()
        self._active = 0
        self._release_at: float | None = None
        self._factor = 1.0
        self._streams: dict[Any, list[int]] = {}
        self._restoring = 0     # restores still setting volumes or removing the state file
        self._keys: dict[Any, str] = {}            # stream id -> identifier stable across runs
        self._stop = False
        self._thread: threading.Thread | None = None
        self._last_warning: dict[str, float] = {}
        self._broken = False

    # -- public API -------------------------------------------------------

    def configure(self, *, enabled: bool | None = None, level: int | None = None) -> None:
        with self._cond:
            if enabled is not None:
                self.enabled = bool(enabled)
            if level is not None:
                self.level = max(0, min(100, int(level))) / 100.0
            self._cond.notify_all()

    def acquire(self) -> None:
        with self._cond:
            self._active += 1
            self._release_at = None
            self._ensure_thread()
            self._cond.notify_all()

    def release(self) -> None:
        with self._cond:
            self._active = max(0, self._active - 1)
            if self._active == 0:
                self._release_at = time.monotonic() + self.hold_s
            self._cond.notify_all()

    def shutdown(self) -> None:
        """Stop the worker and put the volumes back immediately."""
        with self._cond:
            self._stop = True
            self._cond.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(2.0)
        self._restore_now()
        try:
            self.backend.close()
        except Exception:  # noqa: BLE001 - shutting down
            pass

    def settled(self, timeout: float = 5.0) -> bool:
        """Block until the game volume is fully restored (tests, CLI)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._cond:
                if (self._active == 0 and self._factor >= 0.999 and not self._streams
                        and not self._restoring):
                    return True
            time.sleep(0.01)
        return False

    def scan(self) -> list[SinkInput]:
        return [s for s in self.backend.list_streams() if is_game_stream(s, self.match)]

    def restore_stale(self) -> int:
        """Undo a duck left behind by a crashed sidecar.

        Streams are matched by id first, then by their stable identifier:
        Windows remembers each app's volume, so a game restarted since the
        crash has a new session that came back at the ducked level. Ids go
        first so that with two copies of the game running (same identifier),
        each keeps its own volume."""
        if not self.state_path or not self.state_path.is_file():
            return 0
        restored = 0
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            saved = {str(k): v for k, v in state.get("streams", {}).items() if isinstance(v, list)}
            keys = state.get("keys", {})
            live = self.scan()
            by_id = {str(s.id): s for s in live}
            by_key: dict[str, list[SinkInput]] = {}
            for s in live:
                if s.props.get(SESSION_KEY):
                    by_key.setdefault(s.props[SESSION_KEY], []).append(s)
            done: set[str] = set()

            def restore(target: SinkInput, volumes: list) -> None:
                nonlocal restored
                if self._set_raw(target.id, [int(v) for v in volumes]):
                    restored += 1
                done.add(str(target.id))

            unmatched = []
            for sid, volumes in saved.items():
                if sid in by_id:
                    restore(by_id[sid], volumes)
                else:
                    unmatched.append((sid, volumes))
            for sid, volumes in unmatched:
                spare = [s for s in by_key.get(keys.get(sid, ""), []) if str(s.id) not in done]
                if spare:
                    restore(spare[0], volumes)
        except (OSError, ValueError, RuntimeError, AttributeError) as exc:
            log.warning("could not restore game volume: %s", exc)
        self._clear_state()
        if restored:
            log.info("restored the volume of %d game stream(s) left ducked by a previous run", restored)
        return restored

    # -- worker -----------------------------------------------------------

    def _ensure_thread(self) -> None:
        if self._thread is None and not self._stop:
            self._thread = threading.Thread(target=self._worker, name="ducker", daemon=True)
            self._thread.start()

    def _worker(self) -> None:
        while True:
            with self._cond:
                if self._stop:
                    return
                now = time.monotonic()
                holding = self._release_at is not None and now < self._release_at
                want = self.enabled and not self._broken and (self._active > 0 or holding)
                desired = self.level if want else 1.0
                if abs(self._factor - desired) < 1e-3:
                    timeout = (self._release_at - now) if (self._active == 0 and holding) else None
                    self._cond.wait(timeout)
                    continue
                going_down = desired < self._factor
            try:
                self._step(desired, going_down)
            except Exception:  # noqa: BLE001 - ducking must never take the sidecar down
                log.exception("ducking failed; disabling it")
                self._broken = True

    def _step(self, desired: float, going_down: bool) -> None:
        if not going_down and not self._streams:
            self._factor = 1.0   # nothing was ducked, nothing to ramp back
            return
        if self._factor >= 0.999 and going_down:
            self._begin()
            if not self._streams:
                self._factor = desired   # nothing to duck; don't rescan every tick
                return
        span = abs(1.0 - self.level) or 1.0
        seconds = self.attack_s if going_down else self.release_s
        steps = max(1, int(round(seconds / 0.06))) if seconds > 0 else 1
        delta = span / steps
        nxt = max(desired, self._factor - delta) if going_down else min(desired, self._factor + delta)
        if nxt >= 0.999:
            self._restore_now()
        else:
            self._apply(nxt)
            self._factor = nxt
        if seconds > 0:
            time.sleep(seconds / steps)

    def _begin(self) -> None:
        try:
            found = self.scan()
        except DuckingUnavailable as exc:
            self._warn("unavailable", f"ducking is off: {exc}")
            self._broken = True
            return
        except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
            self._warn("scan", f"ducking could not list audio streams: {exc}")
            return
        self._streams = {s.id: list(s.volumes) for s in found if s.volumes}
        self._keys = {s.id: s.props[SESSION_KEY] for s in found if s.volumes and s.props.get(SESSION_KEY)}
        if not self._streams:
            self._warn("nomatch", "ducking found no game audio stream "
                       "(run `kotodama_server.py audio list`, then adjust duck.match)")
            return
        self._save_state()

    def _apply(self, factor: float) -> None:
        for sid, original in list(self._streams.items()):
            if not self._set_raw(sid, [int(v * factor) for v in original]):
                self._streams.pop(sid, None)

    def _restore_now(self) -> None:
        # Take the streams under the lock, so the worker and shutdown() can't
        # both restore them. The volumes and the state file are done outside
        # it; _restoring keeps settled() waiting until they are.
        with self._cond:
            streams, self._streams = self._streams, {}
            self._factor = 1.0
            self._restoring += 1
        try:
            for sid, original in streams.items():
                self._set_raw(sid, original)
            self._clear_state()
        finally:
            with self._cond:
                self._restoring -= 1
                self._cond.notify_all()

    def _set_raw(self, sid: Any, volumes: list[int]) -> bool:
        return self.backend.set_volumes(sid, volumes)

    # -- state file -------------------------------------------------------

    def _save_state(self) -> None:
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            state = {"pid": os.getpid(), "streams": {str(k): v for k, v in self._streams.items()},
                     "keys": {str(k): v for k, v in self._keys.items()}}
            tmp.write_text(json.dumps(state), encoding="utf-8")
            replace_file(tmp, self.state_path)
        except OSError as exc:
            log.warning("could not write duck state: %s", exc)

    def _clear_state(self) -> None:
        if self.state_path:
            try:
                self.state_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                log.warning("could not remove duck state: %s", exc)

    def _warn(self, key: str, message: str) -> None:
        now = time.monotonic()
        if now - self._last_warning.get(key, -600.0) < 600.0:
            return
        self._last_warning[key] = now
        log.warning(message)
        self.notify(message)
