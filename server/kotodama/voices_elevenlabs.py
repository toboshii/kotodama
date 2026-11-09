"""ElevenLabs voice catalog and automatic casting.

ElevenLabs voices are account-specific: the stock defaults plus whatever the
player has added from the voice library, designed or cloned. So instead of a
fixed list, the sidecar fetches what this account can use and casts each NPC
by scoring every voice against the NPC's profile: gender, age, pitch, and
descriptive words from tone/personality/accent, matched against the voice's
labels and description. The top few are tie-broken by a stable hash of the
NPC's name, so casting is varied but repeatable.

Once an NPC has been cast, the choice is remembered (casting memory) so that
adding voices to the account later doesn't re-cast everyone. A changed
profile (an override, a new LLM description) does re-cast.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .archetypes import stable_hash

log = logging.getLogger("kotodama.voices")

TOP_K = 3
MARGIN = 1.5          # candidates must score within this of the best fit: less than one
                      # matching descriptor, so variety never costs a voice quality
WORD_WEIGHT = 2.0     # per matching descriptor: what a voice sounds like (deep, gravelly,
WORD_CAP = 8.0        # bright...) should outweigh a near-miss on age

# Species whose voices should lean towards "characters" voices.
CREATURE_SPECIES = {"moogle", "goblin", "yagudo", "quadav", "orc", "tonberry", "antica", "sahagin",
                    "mamool ja", "lamia", "troll"}

PITCH_WORDS = {
    "very low": {"deep", "low", "gravelly", "raspy", "husky", "resonant", "rough", "gruff", "booming"},
    "low": {"deep", "low", "resonant", "husky", "gravelly"},
    "high": {"high", "bright", "youthful", "light", "young"},
    "very high": {"high", "bright", "childish", "childlike", "cute", "youthful", "animated", "squeaky"},
}

# Profile word -> words worth finding in a voice's labels/description.
SYNONYMS = {
    "gruff": {"gruff", "raspy", "gravelly", "rough"},
    "gravelly": {"gravelly", "raspy", "deep"},
    "rumbling": {"deep", "resonant", "booming"},
    "booming": {"booming", "deep", "loud"},
    "raspy": {"raspy", "gravelly", "husky"},
    "formal": {"formal", "professional", "authoritative", "crisp"},
    "proud": {"confident", "authoritative"},
    "dignified": {"authoritative", "calm", "refined"},
    "stern": {"serious", "authoritative", "firm"},
    "aristocratic": {"british", "refined", "posh", "elegant"},
    "refined": {"british", "refined", "posh", "elegant"},
    "playful": {"playful", "sassy", "upbeat"},
    "sly": {"sassy", "seductive", "playful"},
    "teasing": {"playful", "sassy"},
    "cheerful": {"cheerful", "upbeat", "friendly", "bright"},
    "bubbly": {"bubbly", "upbeat", "energetic"},
    "excitable": {"energetic", "excited", "upbeat"},
    "energetic": {"energetic", "upbeat"},
    "warm": {"warm", "friendly"},
    "friendly": {"friendly", "warm"},
    "kindly": {"warm", "gentle", "friendly"},
    "calm": {"calm", "soothing", "soft"},
    "stoic": {"calm", "serious"},
    "wise": {"calm", "mature", "wise"},
    "menacing": {"intense", "sinister", "dark"},
    "cruel": {"sinister", "intense"},
    "snarling": {"intense", "raspy", "aggressive"},
    "aggressive": {"aggressive", "intense"},
    "eerie": {"whispery", "soft", "sinister", "creepy"},
    "unsettling": {"creepy", "sinister"},
    "whisper": {"whispery", "soft"},
    "soft": {"soft", "gentle"},
    "seductive": {"seductive", "sultry"},
    "silky": {"smooth", "sultry"},
    "squeaky": {"childish", "animated", "cute"},
    "hissing": {"raspy", "sinister"},
    "storyteller": {"narration", "storyteller", "narrative"},
    "atmospheric": {"narration", "narrative"},
    "weary": {"tired", "soft"},
    "confident": {"confident"},
    "nervous": {"nervous", "anxious"},
}

AGE_SCORES = {
    "child": {"young": 3},
    "young": {"young": 3, "middle_aged": 1},
    "adult": {"middle_aged": 3, "young": 2, "old": 1},
    "middle-aged": {"middle_aged": 3, "old": 1, "young": 1},
    "elderly": {"old": 3, "middle_aged": 1},
}

_WORD = re.compile(r"[a-z]+")


def _norm_label(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


@dataclass
class Voice:
    id: str
    name: str
    category: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    description: str = ""

    @property
    def gender(self) -> str:
        return _norm_label(self.labels.get("gender"))

    @property
    def age(self) -> str:
        return _norm_label(self.labels.get("age"))

    @property
    def accent(self) -> str:
        return _norm_label(self.labels.get("accent"))

    def words(self) -> set[str]:
        text = " ".join([self.description] + [str(v) for k, v in self.labels.items() if k not in ("gender", "age")])
        return set(_WORD.findall(text.lower().replace("_", " ")))

    def summary(self) -> str:
        bits = [b for b in (self.gender, self.age.replace("_", " "), self.accent, self.labels.get("use_case", ""))
                if b]
        desc = f" - {self.description}" if self.description else ""
        return f"{self.name} ({', '.join(bits) or 'unlabelled'}){desc}"[:160]

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "Voice | None":
        vid = raw.get("voice_id") or raw.get("id")
        if not isinstance(vid, str) or not vid:
            return None
        labels = raw.get("labels") if isinstance(raw.get("labels"), dict) else {}
        return cls(id=vid, name=str(raw.get("name") or vid), category=str(raw.get("category") or ""),
                   labels={str(k): str(v) for k, v in labels.items() if v is not None},
                   description=str(raw.get("description") or ""))


def _atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


class VoiceCatalog:
    """The voices this account can use, cached on disk."""

    def __init__(self, path: Path, fetch: Callable[[], list[dict[str, Any]]] | None = None,
                 refresh_hours: float = 24.0, exclude: list[str] | None = None):
        self.path = Path(path)
        self.fetch = fetch
        self.refresh_s = max(0.0, refresh_hours) * 3600
        self.exclude = {e.strip().lower() for e in (exclude or []) if e and e.strip()}
        self.voices: list[Voice] = []
        self.fetched_at = 0.0
        self._lock = threading.Lock()
        self._syncing = False
        self.load()

    def load(self) -> int:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        voices = [v for v in (Voice.from_api(r) for r in data.get("voices", [])) if v]
        with self._lock:
            self.voices = self._filter(voices)
            self.fetched_at = float(data.get("fetched_at") or 0)
        return len(self.voices)

    def _filter(self, voices: list[Voice]) -> list[Voice]:
        return [v for v in voices if v.id.lower() not in self.exclude and v.name.lower() not in self.exclude]

    @property
    def stale(self) -> bool:
        return not self.voices or (self.refresh_s > 0 and time.time() - self.fetched_at > self.refresh_s)

    def sync(self) -> int:
        """Fetch the account's voices now. Raises on failure (keeps the old list)."""
        if self.fetch is None:
            raise RuntimeError("no fetcher configured")
        raw = self.fetch()
        voices = [v for v in (Voice.from_api(r) for r in raw) if v]
        voices.sort(key=lambda v: (v.name.lower(), v.id))
        now = time.time()
        _atomic_json(self.path, {"fetched_at": now, "voices": [
            {"voice_id": v.id, "name": v.name, "category": v.category, "labels": v.labels,
             "description": v.description} for v in voices]})
        with self._lock:
            self.voices = self._filter(voices)
            self.fetched_at = now
        log.info("ElevenLabs voice catalog: %d voices", len(self.voices))
        return len(self.voices)

    def sync_in_background(self, on_done: Callable[[int | None, Exception | None], None] | None = None) -> bool:
        with self._lock:
            if self._syncing or self.fetch is None:
                return False
            self._syncing = True

        def _go() -> None:
            count, error = None, None
            try:
                count = self.sync()
            except Exception as exc:  # noqa: BLE001 - network/API errors are reported, not raised
                error = exc
                log.warning("voice catalog refresh failed: %s", exc)
            finally:
                with self._lock:
                    self._syncing = False
            if on_done:
                on_done(count, error)

        threading.Thread(target=_go, name="voice-sync", daemon=True).start()
        return True

    def get(self, voice_id: str | None) -> Voice | None:
        if not voice_id:
            return None
        with self._lock:
            return next((v for v in self.voices if v.id == voice_id), None)

    def find(self, name_or_id: str) -> Voice | None:
        """By id, or by name (case-insensitive, '_' counts as a space)."""
        key = name_or_id.strip().replace("_", " ").lower()
        with self._lock:
            for v in self.voices:
                if v.id == name_or_id.strip() or v.name.lower() == key:
                    return v
            matches = [v for v in self.voices if v.name.lower().startswith(key)]
        return matches[0] if len(matches) == 1 else None

    def snapshot(self) -> list[Voice]:
        with self._lock:
            return list(self.voices)

    def drop(self, voice_id: str) -> None:
        """Forget a voice the API says no longer exists (until the next sync)."""
        with self._lock:
            self.voices = [v for v in self.voices if v.id != voice_id]


class CastingMemory:
    """NPC -> voice once cast, plus the profile it was cast from."""

    BASIS_FIELDS = ("gender", "age", "pitch", "species", "accent", "tone", "personality")

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._data: dict[str, dict[str, str]] = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self._data = {}

    @classmethod
    def basis(cls, profile: dict[str, Any]) -> str:
        return "|".join(str(profile.get(f) or "") for f in cls.BASIS_FIELDS)

    def get(self, ident: str, profile: dict[str, Any]) -> str | None:
        with self._lock:
            entry = self._data.get(ident.lower())
        if entry and entry.get("basis") == self.basis(profile):
            return entry.get("voice")
        return None

    def remember(self, ident: str, profile: dict[str, Any], voice_id: str) -> None:
        with self._lock:
            self._data[ident.lower()] = {"voice": voice_id, "basis": self.basis(profile)}
            data = dict(self._data)
        try:
            _atomic_json(self.path, data)
        except OSError as exc:
            log.warning("could not save casting memory: %s", exc)

    def _save(self, data: dict[str, dict[str, str]]) -> None:
        try:
            _atomic_json(self.path, data)
        except OSError as exc:
            log.warning("could not save casting memory: %s", exc)

    def forget(self, ident: str) -> bool:
        with self._lock:
            removed = self._data.pop(ident.lower(), None) is not None
            data = dict(self._data)
        if removed:
            self._save(data)
        return removed

    def forget_voice(self, voice_id: str) -> int:
        """Drop every NPC cast to this voice (e.g. it was deleted from the account)."""
        with self._lock:
            gone = [k for k, e in self._data.items() if e.get("voice") == voice_id]
            for k in gone:
                del self._data[k]
            data = dict(self._data)
        if gone:
            self._save(data)
        return len(gone)


def _profile_words(profile: dict[str, Any]) -> set[str]:
    text = " ".join(str(profile.get(f) or "") for f in ("tone", "personality", "accent", "direction"))
    words = set(_WORD.findall(text.lower()))
    wanted = set(words)
    for w in words:
        wanted |= SYNONYMS.get(w, set())
    wanted |= PITCH_WORDS.get(str(profile.get("pitch") or ""), set())
    return wanted


def score_voice(voice: Voice, profile: dict[str, Any], role: str | None) -> float | None:
    """Higher is a better fit; None means unusable (wrong gender)."""
    score = 0.0
    want_gender = str(profile.get("gender") or "")
    if want_gender in ("male", "female"):
        if voice.gender == want_gender:
            score += 5
        elif voice.gender in ("male", "female"):
            return None
        else:
            score -= 1                     # unlabelled or neutral voice: usable, not preferred

    score += AGE_SCORES.get(str(profile.get("age") or ""), {}).get(voice.age, 0)

    words = voice.words()
    score += min(WORD_CAP, WORD_WEIGHT * len(_profile_words(profile) & words))

    accent = str(profile.get("accent") or "").lower()
    if any(w in accent for w in ("british", "aristocratic", "refined", "posh")) and "british" in voice.accent:
        score += 2

    use_case = _norm_label(voice.labels.get("use_case"))
    if role == "narrator" and any(u in use_case or u in words for u in ("narration", "narrative", "audiobook", "storyteller")):
        score += 3
    if role == "creature" and any(u in use_case or u in words for u in ("characters", "character", "animation", "cartoon")):
        score += 2
    return score


def role_for(profile: dict[str, Any], ident: str) -> str | None:
    if ident.lower() == "@narrator":
        return "narrator"
    if str(profile.get("species") or "").lower() in CREATURE_SPECIES:
        return "creature"
    return None


def rank(catalog: VoiceCatalog, profile: dict[str, Any], ident: str) -> list[tuple[float, Voice]]:
    role = role_for(profile, ident)
    voices = catalog.snapshot()
    scored = [(s, v) for v in voices if (s := score_voice(v, profile, role)) is not None]
    if not scored and voices:
        # No voice of the right gender on this account at all: voicing the NPC
        # with the best remaining fit beats leaving it silent.
        relaxed = {**profile, "gender": "neutral"}
        scored = [(s, v) for v in voices if (s := score_voice(v, relaxed, role)) is not None]
    scored.sort(key=lambda sv: (-sv[0], sv[1].id))
    return scored


def cast(catalog: VoiceCatalog, profile: dict[str, Any], ident: str,
         memory: CastingMemory | None = None) -> str | None:
    """Pick a voice id for this NPC (stable across runs)."""
    if memory is not None:
        remembered = memory.get(ident, profile)
        if remembered and catalog.get(remembered):
            return remembered
    ranked = rank(catalog, profile, ident)
    if not ranked:
        return None
    # Variety only among near-equal fits: never hand an NPC a clearly worse
    # voice just because of how its name hashes.
    best = ranked[0][0]
    top = [sv for sv in ranked[:TOP_K] if sv[0] >= best - MARGIN]
    choice = top[stable_hash(ident) % len(top)][1].id
    if memory is not None:
        memory.remember(ident, profile, choice)
    return choice
