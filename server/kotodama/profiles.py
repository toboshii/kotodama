"""Voice profile resolution.

Layers, lowest to highest priority:
  1. archetype defaults        race / creature / narrator base traits
  2. stable name variation     same NPC -> same voice on every machine
  3. library                   curated > community pack > learned (LLM)
  4. overrides                 config/overrides.json (//koto voice ...)
Within a layer a zone-specific key ("Name@<zone>") beats the plain name, and
the role keys "@player" / "@narrator" apply to those speakers. A custom line
(role "custom") takes the "@narrator" layers and then "@custom" on top, so it
sounds exactly like the narrator until "@custom" is given a layer of its own.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from .archetypes import Archetypes, PROFILE_FIELDS, stable_hash
from .library import Library, _norm_key
from .protocol import Speaker

_NUDGE = (-1, 0, 0, 1)

CUSTOM_KEY = "@custom"


def _narrated(speaker: Speaker) -> bool:
    return speaker.narrator or speaker.role == "custom"


@dataclass
class Resolved:
    profile: dict[str, Any]
    archetype: str
    key: str
    sources: list[str] = field(default_factory=list)


def _nudge(vocab: list[str], current: str, h: int, shift: int) -> str:
    if current not in vocab:
        return current
    idx = vocab.index(current) + _NUDGE[(h >> shift) % 4]
    return vocab[max(0, min(len(vocab) - 1, idx))]


class ProfileResolver:
    def __init__(self, archetypes: Archetypes, library: Library, provider: str = "openai"):
        self.arch = archetypes
        self.lib = library
        self.provider = provider

    def archetype_key(self, speaker: Speaker) -> str:
        return self.arch.key_for(
            name=speaker.name, race=speaker.race, creature=speaker.creature,
            narrator=_narrated(speaker),
        )

    def _base(self, speaker: Speaker) -> tuple[dict[str, Any], str]:
        akey = self.archetype_key(speaker)
        arch = self.arch.items[akey]
        profile = {f: arch[f] for f in PROFILE_FIELDS if f in arch}

        narrated = _narrated(speaker)
        ident = "@narrator" if narrated else (speaker.name or "@unknown")
        h = stable_hash(ident)
        if not narrated:
            profile["pitch"] = _nudge(self.arch.vocab["pitch"], profile.get("pitch", "medium"), h, 8)
            profile["pace"] = _nudge(self.arch.vocab["pace"], profile.get("pace", "normal"), h, 16)

        candidates = arch.get("voices", {}).get(self.provider) or self.arch.provider_voices.get(self.provider, [])
        if candidates:
            profile["voice"] = {self.provider: candidates[h % len(candidates)]}
        return profile, akey

    @staticmethod
    def _merge(into: dict[str, Any], layer: dict[str, Any]) -> None:
        for k, v in layer.items():
            if v in (None, "", {}):
                continue
            if k == "voice" and isinstance(v, dict):
                into["voice"] = {**into.get("voice", {}), **v}
            else:
                into[k] = copy.deepcopy(v)

    def resolve(self, speaker: Speaker, zone: int | None) -> Resolved:
        profile, akey = self._base(speaker)
        sources = [f"archetype:{akey}"]

        ident = None
        if speaker.role == "custom":
            keys = [CUSTOM_KEY, "@narrator"]
            # The narrator's identity (and with it the narrator's remembered
            # voice) until "@custom" is tuned; then a voice of its own.
            ident = CUSTOM_KEY if self.has_layer(CUSTOM_KEY) else "@narrator"
        elif speaker.narrator:
            keys = ["@narrator"]
        elif speaker.is_player:
            keys = ["@player"]
        else:
            keys = self.lib.keys_for(speaker.name, zone)

        # General first so the more specific (zone) key wins.
        for key in reversed(keys):
            hit = self.lib.lookup(key)
            if hit:
                self._merge(profile, hit[0])
                sources.append(f"library:{hit[1].get('source', '?')}")
        for key in reversed(keys):
            ov = self.lib.override(key)
            if ov:
                self._merge(profile, ov)
                sources.append("override")

        voice = profile.get("voice")
        profile["voice"] = voice.get(self.provider) if isinstance(voice, dict) else None
        if not profile["voice"]:
            pool = self.arch.provider_voices.get(self.provider, [])
            profile["voice"] = pool[0] if pool else None
        return Resolved(profile=profile, archetype=akey, key=_norm_key(ident or speaker.name or "@narrator"),
                        sources=sources)

    def has_layer(self, key: str) -> bool:
        """True when the library or an override has anything for this key."""
        k = _norm_key(key)
        return bool(self.lib.lookup(k) or self.lib.override(k))

    def needs_inference(self, speaker: Speaker) -> bool:
        """True for a named NPC nobody has described yet."""
        if speaker.narrator or speaker.role or speaker.is_player or not speaker.name:
            return False
        return not self.lib.has_any(speaker.name)


def describe(profile: dict[str, Any]) -> str:
    """Provider-neutral acting direction rendered from a profile. Used as the
    TTS `instructions` and therefore part of the cache key."""
    who = " ".join(p for p in (
        profile.get("gender") if profile.get("gender") not in (None, "neutral") else None,
        profile.get("age"),
        profile.get("species"),
    ) if p)
    parts = []
    if who:
        voice = f"Voice: {who}"
        if profile.get("accent"):
            voice += f", {profile['accent']} accent"
        parts.append(voice + ".")
    elif profile.get("accent"):
        parts.append(f"Accent: {profile['accent']}.")
    if profile.get("personality"):
        parts.append(f"Personality: {profile['personality']}.")
    if profile.get("tone"):
        parts.append(f"Tone: {profile['tone']}.")
    if profile.get("pitch"):
        parts.append(f"Pitch: {profile['pitch']}.")
    if profile.get("pace"):
        parts.append(f"Pace: {profile['pace']}.")
    if profile.get("direction"):
        d = profile["direction"].rstrip(".")
        parts.append(d + ".")
    parts.append(
        "Performance: in-character dialogue from a fantasy role-playing game. "
        "Say only the given text exactly as written; add, drop or reword nothing."
    )
    return " ".join(parts)
