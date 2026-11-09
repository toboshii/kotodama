"""Sidecar configuration: defaults, server/config.json overlay, key lookup."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

# server/kotodama/config.py -> addon root is two levels up from server/.
SERVER_DIR = Path(__file__).resolve().parents[1]
ROOT = SERVER_DIR.parent

DEFAULTS: dict[str, Any] = {
    "listen": {"host": "127.0.0.1", "port": 47123},
    "interrupt": True,
    # Percent of the voice's base level, 0-400. 100 = "normal" (which already
    # includes audio.makeup_db); raise it if the voice is quiet next to the game.
    "volume": 100,
    "max_text_chars": 1500,
    "tts": {
        "provider": "openai",
        "model": "gpt-4o-mini-tts",
        "base_url": "https://api.openai.com",
        "api_key": None,
        "api_key_env": "OPENAI_API_KEY",
        "timeout": 30,
        "sample_rate": 24000,
    },
    "inference": {
        # none | anthropic | openai.  "none" = archetypes + overrides only.
        "provider": "none",
        # Casting a voice is a small task, so the default is the cheap, fast
        # model. Set e.g. "claude-opus-5-5" for more considered casting.
        "model": "claude-haiku-5-5",
        "effort": "low",
        "openai_model": "gpt-4o-mini",
        "wait_ms": 2500,
        "max_per_minute": 6,
        "api_key": None,
        "api_key_env": "ANTHROPIC_API_KEY",
        "openai_api_key": None,
        "openai_api_key_env": "OPENAI_API_KEY",
    },
    "audio": {
        # Fixed makeup gain on top of `volume`. TTS output is not mastered to
        # game loudness, so a boost is the normal case. A peak limiter keeps
        # large boosts from clipping.
        "makeup_db": 9.0,
    },
    "duck": {
        # Lower the game's own audio while a line is spoken. Needs `pactl`.
        "enabled": True,
        "level": 40,            # percent of the original game volume while speaking
        "attack_ms": 120,
        "release_ms": 700,
        "hold_ms": 500,         # stay ducked this long between lines
        # A stream is ducked if any of these (case-insensitive) appears in its
        # application name, binary or Flatpak app id. See `audio list`.
        "match": ["windower", "playonline", "ffxi", "pol.exe"],
    },
    "player": {"command": None},
    "cache": {
        "dir": "cache",
        "max_mb": 2048,
        "finish_download_on_stop": True,
    },
    "library": {
        "file": "library/npcs.json",
        "packs_dir": "library/packs",
        "overrides": "config/overrides.json",
    },
}


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, data: dict[str, Any] | None = None, root: Path | None = None):
        self.data = _merge(DEFAULTS, data or {})
        self.root = Path(root) if root else ROOT

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        path = Path(path or os.environ.get("KOTODAMA_CONFIG") or SERVER_DIR / "config.json")
        data: dict[str, Any] = {}
        if path.is_file():
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        return cls(data)

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def path(self, dotted: str) -> Path:
        """A configured path, resolved against the addon root."""
        p = Path(str(self.get(dotted)))
        return p if p.is_absolute() else self.root / p

    def api_key(self, section: str, key: str = "api_key", env_key: str = "api_key_env") -> str | None:
        explicit = self.get(f"{section}.{key}")
        if explicit:
            return str(explicit)
        env = self.get(f"{section}.{env_key}")
        return os.environ.get(env) if env else None
