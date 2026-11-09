"""Archetype data: race/creature -> base voice traits and candidate voices."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .config import SERVER_DIR

PROFILE_FIELDS = (
    "gender", "age", "species", "personality", "tone", "accent", "pitch", "pace", "direction",
)
ENUM_FIELDS = ("gender", "age", "pitch", "pace")


class Archetypes:
    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.vocab: dict[str, list[str]] = data["vocab"]
        self.items: dict[str, dict[str, Any]] = data["archetypes"]
        self.race_map: dict[int, str] = {int(k): v for k, v in data["race_map"].items()}
        self.keywords: dict[str, str] = data["name_keywords"]
        self.provider_voices: dict[str, list[str]] = data["provider_voices"]

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Archetypes":
        path = Path(path) if path else SERVER_DIR / "archetypes.json"
        with open(path, "r", encoding="utf-8") as fh:
            return cls(json.load(fh))

    def key_for(self, *, name: str | None, race: int | None, creature: str | None,
                narrator: bool = False) -> str:
        """Pick the archetype for a speaker. Order: narrator, creature hint,
        player race, name keyword, unknown."""
        if narrator:
            return "narrator"
        if creature and creature in self.items:
            return creature
        if race is not None and race in self.race_map:
            return self.race_map[race]
        if name:
            lowered = name.lower()
            for word, key in self.keywords.items():
                if word in lowered:
                    return key
        return "unknown"

    def valid_voice(self, provider: str, voice: str) -> bool:
        return voice in self.provider_voices.get(provider, [])

    def sanitize(self, raw: Any, *, strict: bool = False) -> dict[str, Any]:
        """Reduce untrusted profile data (library packs, LLM output, user
        overrides) to known fields with valid values.

        strict=True raises ValueError on the first bad value instead of
        silently dropping it (used for interactive overrides so the player
        gets feedback)."""
        out: dict[str, Any] = {}
        if not isinstance(raw, dict):
            if strict:
                raise ValueError("profile must be an object")
            return out

        for f in ENUM_FIELDS:
            if f not in raw or raw[f] in (None, ""):
                continue
            v = str(raw[f]).strip().lower().replace("_", " ")
            if v in self.vocab[f]:
                out[f] = v
            elif strict:
                raise ValueError(f"{f} must be one of: {', '.join(self.vocab[f])}")

        for f in ("species", "personality", "tone", "accent", "direction"):
            v = raw.get(f)
            if isinstance(v, str) and v.strip():
                out[f] = " ".join(v.split())[:160]

        voice = raw.get("voice")
        if isinstance(voice, dict):
            clean: dict[str, str] = {}
            for provider, name in voice.items():
                if not isinstance(provider, str) or not isinstance(name, str):
                    continue
                known = self.provider_voices.get(provider)
                if known is None or name in known:
                    clean[provider] = name
                elif strict:
                    raise ValueError(f"unknown {provider} voice '{name}' (known: {', '.join(known)})")
            if clean:
                out["voice"] = clean
        return out


def stable_hash(name: str) -> int:
    """Deterministic across machines and Python versions (unlike hash())."""
    return int.from_bytes(hashlib.sha1(name.lower().encode("utf-8")).digest()[:8], "big")
