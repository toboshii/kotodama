"""Inworld text-to-speech through its OpenAI-compatible endpoint, streamed.

Synthesis is OpenAI's request shape, POST {base}/v1/audio/speech with
response_format "pcm" (raw 16-bit, 24 kHz, sent as it's generated), so it
shares GenericTTS's OpenAI-compatible code path. What's Inworld's own lives
here:

- the key: the Base64 credential from the Inworld portal, sent as
  "Authorization: Basic".
- error codes.
- the voice library from GET {base}/voices/v1/voices. It labels every voice
  with gender, age group and language, and includes the workspace's own
  cloned and designed voices.

The key travels only in the Authorization header, never in a URL.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.parse
from typing import Any

from .casting import Voice, derive_labels
from .tts_base import HTTPStreamer, TTSError, error_detail

log = logging.getLogger("kotodama.inworld")

SPEECH_PATH = "/v1/audio/speech"
VOICES_PATH = "/voices/v1/voices"
# Request fields an adapter's "extra" may set; the transport fixes the rest.
TEMPLATE_FIELDS = {"instructions", "speed"}
KEY_HINT = "use the Base64 key from the Inworld portal"

_AGES = {"young": "young", "middle_aged": "middle_aged", "elderly": "old"}
_ACCENTS = {"gb": "british", "uk": "british", "au": "australian", "us": "american", "ie": "irish",
            "in": "indian", "ca": "canadian", "nz": "new zealand", "za": "south african"}


def basic_key(key: str) -> str:
    """The Basic credential: the portal's Base64 key as-is, or a raw
    key:secret pair encoded for the user."""
    key = key.strip()
    if ":" in key:
        return base64.b64encode(key.encode("utf-8")).decode("ascii")
    return key


def inworld_error(status: int, body: bytes) -> TTSError:
    code, message = error_detail(body)
    param = ""
    try:
        err = json.loads(body).get("error")
        if isinstance(err, dict) and err.get("param"):
            param = str(err["param"])
    except (ValueError, AttributeError):
        pass
    low = f"{code} {message}".lower()
    shown = message or code or f"HTTP {status}"
    if status in (401, 403) or "authentication" in low or "permission" in low:
        return TTSError(f"Inworld rejected the API key (INWORLD_API_KEY; {KEY_HINT})", status, "auth")
    if status == 402 or "insufficient_quota" in low or "credits" in low:
        return TTSError(f"Inworld: no credits left ({shown})", status, "quota")
    if status == 429 or "rate_limit" in low or "rate limit" in low:
        return TTSError(f"Inworld is rate limiting ({shown})", status, "rate")
    if status == 404 or ("voice" in low and any(w in low for w in ("not found", "unknown", "invalid"))):
        return TTSError(f"Inworld voice not available ({shown})", status, "voice")
    if status == 400:
        where = f" ({param})" if param and param not in shown else ""
        return TTSError(f"Inworld refused the request{where}: {shown}", status, "input")
    return TTSError(f"Inworld error: {shown}", status, None)


def _language(item: dict[str, Any]) -> str:
    """'en-gb' from languageCode (BCP-47) or langCode (EN_GB)."""
    lang = item.get("languageCode") or item.get("langCode") or ""
    return str(lang).lower().replace("_", "-")


def inworld_voice(item: dict[str, Any], languages: list[str]) -> Voice | None:
    """A voice from Inworld's voice list, labelled from its structured
    fields; None if it's a library voice in a language we don't speak."""
    vid = item.get("voiceId")
    if not isinstance(vid, str) or not vid:
        return None
    source = str(item.get("source") or "SYSTEM").upper()
    custom = source != "SYSTEM" or item.get("owned") is True
    lang = _language(item)
    if lang and languages and not custom and not any(lang.startswith(l) for l in languages):
        return None
    name = str(item.get("displayName") or vid)
    desc = str(item.get("description") or "")
    labels: dict[str, str] = {}
    gender = str(item.get("gender") or "").lower()
    if gender in ("male", "female"):
        labels["gender"] = gender
    age = _AGES.get(str(item.get("ageGroup") or "").lower())
    if age:
        labels["age"] = age
    region = lang.split("-")[1] if lang.count("-") >= 1 else ""
    if lang.startswith("en") and region in _ACCENTS:
        labels["accent"] = _ACCENTS[region]
    tags = item.get("tags") if isinstance(item.get("tags"), list) else []
    words = [str(t).lower().replace("_", " ") for t in tags if t]
    for key, value in derive_labels(f"{name} {desc}").items():
        if key == "descriptive":
            words.append(value)
        else:
            labels.setdefault(key, value)
    if words:
        labels["descriptive"] = " ".join(words)
    return Voice(id=vid, name=name, category="custom" if custom else "library", labels=labels,
                 description=desc[:200])


class InworldClient:
    def __init__(self, base_url: str, api_key: str | None, *, timeout: float = 30.0):
        self.api_key = api_key
        self.http = HTTPStreamer(base_url.rstrip("/"), timeout)

    def headers(self) -> dict[str, str]:
        if not self.api_key:
            raise TTSError("no Inworld API key (set INWORLD_API_KEY)", code="auth")
        return {"Authorization": f"Basic {basic_key(self.api_key)}", "Content-Type": "application/json"}

    def fetch_voices(self) -> list[dict[str, Any]]:
        """Every voice the workspace can use: the system library and its own (paged)."""
        voices: list[dict[str, Any]] = []
        token = None
        for _ in range(20):
            query = {"pageSize": "2000"}
            if token:
                query["pageToken"] = token
            status, _, data = self.http.request("GET", VOICES_PATH + "?" + urllib.parse.urlencode(query), None,
                                                self.headers())
            if status != 200:
                raise inworld_error(status, data)
            try:
                parsed = json.loads(data)
            except ValueError as exc:
                raise TTSError("Inworld returned a non-JSON voice list", code="network") from exc
            voices.extend(v for v in parsed.get("voices") or [] if isinstance(v, dict))
            token = parsed.get("nextPageToken")
            if not token:
                break
        return voices

    def warm(self) -> None:
        if self.api_key:
            self.http.warm()

    def close(self) -> None:
        self.http.close()
