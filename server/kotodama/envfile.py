"""Load ~/.config/kotodama.env ourselves, so a sidecar started from a
terminal sees the same API keys as the systemd unit (which reads the file
through EnvironmentFile=).

Format (systemd EnvironmentFile, plus `export` for shell-style files):

    # comment            ; comment
    OPENAI_API_KEY=sk-...
    export REPLICATE_API_TOKEN="r8_..."

Variables already in the environment win (your shell, or systemd having
read the file already). Names that came from the file are remembered in
KOTODAMA_ENV_LOADED, so a restart with R re-reads the file: edited values
take effect and removed ones disappear, while shell-set ones stay untouched.
Values are never logged.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from pathlib import Path
from typing import MutableMapping

log = logging.getLogger("kotodama.env")

MARKER = "KOTODAMA_ENV_LOADED"
OVERRIDE = "KOTODAMA_ENV_FILE"
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", '"': '"', "$": "$"}


def default_path(environ: MutableMapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    if env.get(OVERRIDE):
        return Path(env[OVERRIDE]).expanduser()
    base = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else Path.home() / ".config"
    return base / "kotodama.env"


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == '"':
        out, chars, i = [], value[1:-1], 0
        while i < len(chars):
            c = chars[i]
            if c == "\\" and i + 1 < len(chars):
                out.append(_ESCAPES.get(chars[i + 1], "\\" + chars[i + 1]))
                i += 2
                continue
            out.append(c)
            i += 1
        return "".join(out)
    return value


def parse(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or not _NAME.match(name) or name == MARKER:
            continue
        values[name] = _unquote(value.strip())
    return values


def load(path: Path | None = None, environ: MutableMapping[str, str] | None = None) -> tuple[Path, list[str]]:
    """Apply the env file to the environment. Returns (path, names set from it)."""
    env = os.environ if environ is None else environ
    path = Path(path) if path is not None else default_path(env)
    previous = {n for n in env.get(MARKER, "").split(",") if n}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        values: dict[str, str] = {}
    except OSError as exc:
        log.warning("could not read %s: %s", path, exc)
        return path, []
    else:
        values = parse(text)
        try:
            if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                log.warning("%s holds API keys but other users can read it: chmod 600 %s", path, path)
        except OSError:
            pass
    for name in previous - values.keys():             # removed from the file since we last read it
        env.pop(name, None)
    loaded = []
    for name, value in values.items():
        if name in env and name not in previous:      # set by the shell or systemd: that wins
            continue
        env[name] = value
        loaded.append(name)
    if loaded:
        env[MARKER] = ",".join(sorted(loaded))
    else:
        env.pop(MARKER, None)
    return path, sorted(loaded)
