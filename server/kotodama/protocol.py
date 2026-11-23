"""JSON datagram protocol between the Windower addon and this sidecar."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

MAX_DATAGRAM = 16384

# Speaker roles other than an NPC. "custom": a line the player triggered
# (//koto say, //koto line), voiced as the narrator until "@custom" is tuned.
ROLES = ("custom",)
KINDS = ("ambient", "event", "battle", "custom")
# An addon from before cutscene detection was removed still sends "cutscene".
LEGACY_KINDS = {"cutscene": "event"}


def decode(data: bytes) -> dict[str, Any] | None:
    """Parse one datagram; None if it is not a well-formed message."""
    if not data or len(data) > MAX_DATAGRAM:
        return None
    try:
        msg = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(msg, dict) or not isinstance(msg.get("t"), str):
        return None
    return msg


def encode(msg: dict[str, Any]) -> bytes:
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def log_msg(text: str) -> dict[str, Any]:
    return {"t": "log", "msg": text}


def _str(v: Any, limit: int = 200) -> str | None:
    if isinstance(v, str) and v.strip():
        return v.strip()[:limit]
    return None


def _int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    return None


@dataclass
class Speaker:
    name: str | None = None
    narrator: bool = False
    is_player: bool = False
    race: int | None = None
    race_name: str | None = None
    model: int | None = None
    creature: str | None = None
    was: str | None = None           # the "???" identity this character spoke as before being named
    role: str | None = None          # one of ROLES, or None for an ordinary speaker


@dataclass
class Speak:
    seq: int
    kind: str
    mode: int
    text: str
    zone: int | None = None
    zone_name: str | None = None
    player: str | None = None
    timed: bool = False
    speaker: Speaker = field(default_factory=Speaker)


def parse_speaker(raw: Any) -> Speaker:
    raw = raw if isinstance(raw, dict) else {}
    return Speaker(
        name=_str(raw.get("name"), 64),
        narrator=bool(raw.get("narrator")),
        is_player=bool(raw.get("is_player")),
        race=_int(raw.get("race")),
        race_name=_str(raw.get("race_name"), 64),
        model=_int(raw.get("model")),
        creature=_str(raw.get("creature"), 32),
        was=_str(raw.get("was"), 64),
        role=raw.get("role") if raw.get("role") in ROLES else None,
    )


def parse_speak(msg: dict[str, Any], max_chars: int = 1500) -> Speak | None:
    text = msg.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    kind = LEGACY_KINDS.get(msg.get("kind"), msg.get("kind"))
    if kind not in KINDS:
        kind = "ambient"
    return Speak(
        seq=_int(msg.get("seq")) or 0,
        kind=kind,
        mode=_int(msg.get("mode")) or 0,
        text=text.strip()[:max_chars],
        zone=_int(msg.get("zone")),
        zone_name=_str(msg.get("zone_name"), 64),
        player=_str(msg.get("player"), 32),
        timed=bool(msg.get("timed")),
        speaker=parse_speaker(msg.get("speaker")),
    )
