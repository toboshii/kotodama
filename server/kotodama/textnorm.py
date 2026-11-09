"""Text normalisation for TTS input, cache keys and library samples."""

from __future__ import annotations

import re
import unicodedata

PLAYER_TOKEN = "{player}"

_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Canonical form used both for synthesis and for the cache key."""
    text = unicodedata.normalize("NFC", text)
    text = text.replace("　", " ").replace("~", " ")
    return _WS.sub(" ", text).strip()


def scrub_player(text: str, player: str | None) -> str:
    """Replace the player's name so shared files never contain it."""
    if not player or len(player) < 2:
        return text
    return re.sub(rf"\b{re.escape(player)}\b", PLAYER_TOKEN, text, flags=re.IGNORECASE)
