"""Provider-neutral delivery controls derived from an NPC profile.

Providers without free-text acting directions (ElevenLabs, most Replicate
models) turn a profile into numbers and tags instead: speed from pace,
stability/exaggeration from temperament, a pitch offset, an emotion, an
audio tag. The tables here are shared so every provider reads a profile the
same way.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

PACE_SPEED = {"slow": 0.88, "measured": 0.94, "normal": 1.0, "brisk": 1.06, "fast": 1.12}

EXPRESSIVE = {"excitable", "bubbly", "zealous", "aggressive", "energetic", "shrill", "playful", "eager",
              "snarling", "excited", "mischievous", "brutal", "fierce"}
STEADY = {"calm", "stoic", "formal", "dignified", "composed", "poised", "patient", "measured", "steady",
          "serious", "stern"}

# ElevenLabs v4 delivery tags, first match wins.
TAG_RULES = [
    ("[whispers]", {"whisper", "whispering", "eerie", "hushed"}),
    ("[sarcastic]", {"sarcastic", "sly", "teasing", "sneering", "mocking", "smug"}),
    ("[excited]", {"excited", "excitable", "cheerful", "bubbly", "eager", "energetic", "gleeful"}),
    ("[curious]", {"curious", "inquisitive"}),
    ("[sighs]", {"weary", "melancholy", "tired", "resigned", "sad"}),
]

# Emotion names shared by MiniMax and similar models, first match wins.
EMOTION_RULES = [
    ("angry", {"angry", "furious", "aggressive", "snarling", "hostile", "irritable", "brutal"}),
    ("fearful", {"fearful", "nervous", "anxious", "timid", "frightened", "scared"}),
    ("sad", {"sad", "melancholy", "mournful", "grieving", "sorrowful", "weary"}),
    ("happy", {"cheerful", "bubbly", "jovial", "gleeful", "happy", "excited", "excitable"}),
    ("surprised", {"surprised", "astonished"}),
    ("disgusted", {"disgusted", "contemptuous", "sneering"}),
]

_WORD = re.compile(r"[a-z]+")


def _words(*texts: Any) -> set[str]:
    return set(_WORD.findall(" ".join(str(t or "") for t in texts).lower()))


def temperament_words(profile: dict[str, Any]) -> set[str]:
    return _words(profile.get("tone"), profile.get("personality"))


def temperament(profile: dict[str, Any]) -> str | None:
    """'expressive', 'steady' or None (ordinary)."""
    words = temperament_words(profile)
    if words & EXPRESSIVE:
        return "expressive"
    if words & STEADY:
        return "steady"
    return None


def speed_for(profile: dict[str, Any], table: dict[str, float] | None = None) -> float:
    return (table or PACE_SPEED).get(str(profile.get("pace") or "normal"), 1.0)


def first_match(rules: Iterable[tuple[str, set[str]]], words: set[str]) -> str | None:
    for value, triggers in rules:
        if words & triggers:
            return value
    return None


def tag_for(profile: dict[str, Any]) -> str | None:
    words = _words(profile.get("tone"), profile.get("personality"), profile.get("direction"))
    return first_match(TAG_RULES, words)


def emotion_for(profile: dict[str, Any]) -> str | None:
    return first_match(EMOTION_RULES, temperament_words(profile))


def control(table: Any, profile: dict[str, Any], key: str) -> Any:
    """Look up an adapter control table by profile field, with 'default'."""
    if not isinstance(table, dict):
        return None
    value = str(profile.get(key) or "")
    return table.get(value, table.get("default"))


def mood_control(table: Any, profile: dict[str, Any]) -> Any:
    """Look up an adapter control table by temperament (expressive/steady/default)."""
    if not isinstance(table, dict):
        return None
    mood = temperament(profile)
    return table.get(mood, table.get("default")) if mood else table.get("default")


def style_text(profile: dict[str, Any], limit: int = 240) -> str | None:
    """Short acting direction for models that take a style instruction."""
    parts = []
    if profile.get("tone"):
        parts.append(f"Tone: {profile['tone']}.")
    if profile.get("personality"):
        parts.append(f"Character: {profile['personality']}.")
    if profile.get("pace") and profile.get("pace") != "normal":
        parts.append(f"Pace: {profile['pace']}.")
    if profile.get("direction"):
        parts.append(str(profile["direction"]).rstrip(".") + ".")
    text = " ".join(parts)
    return text[:limit] if text else None


_NOT_STEERING = re.compile(r"[^a-z0-9' -]+")


def _plain(text: Any) -> str:
    """Lower case, punctuation dropped, single spaces."""
    return " ".join(_NOT_STEERING.sub(" ", str(text or "").lower()).split())


def steering_text(profile: dict[str, Any], limit: int = 200) -> str | None:
    """Acting direction in the form Inworld TTS-2 steers best from: plain
    lower-case English without punctuation. Age and gender are left out;
    they come from the voice."""
    parts = []
    tone = _plain(profile.get("tone"))
    if tone:
        parts.append(f"in a {tone} tone")
    personality = _plain(profile.get("personality"))
    if personality:
        parts.append(f"with a {personality} manner")
    pace = _plain(profile.get("pace"))
    if pace and pace != "normal":
        parts.append(f"at a {pace} pace")
    direction = _plain(profile.get("direction"))
    if not parts and not direction:
        return None
    text = " ".join(["speak", *parts]) if parts else ""
    if direction:
        text = f"{text} and {direction}" if text else direction
    return text[:limit].rsplit(" ", 1)[0] if len(text) > limit else text


def voice_description(profile: dict[str, Any], flavour: str = "") -> str:
    """Natural-language description of how the character sounds, for models
    that design a voice from text (Qwen3-TTS voice_design)."""
    gender = profile.get("gender") if profile.get("gender") in ("male", "female") else None
    age = profile.get("age")
    age = {"child": "child's", "elderly": "elderly", "middle-aged": "middle-aged", "young": "young"}.get(str(age))
    species = profile.get("species") or None
    who = " ".join(x for x in (age, gender, species) if x)
    parts = [f"A {who} voice" if who else "A voice"]
    if profile.get("pitch") and profile.get("pitch") != "medium":
        parts.append(f"{profile['pitch']} pitch")
    if profile.get("tone"):
        parts.append(str(profile["tone"]))
    if profile.get("accent"):
        parts.append(f"{profile['accent']} accent")
    text = ", ".join(parts)
    if profile.get("personality"):
        text += f"; {profile['personality']}"
    if flavour:
        text += f"; {flavour}"
    return text + "."
