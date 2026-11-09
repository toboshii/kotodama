"""Gemini text-to-speech through Google's Interactions API, streamed.

POST {base}/v1beta/interactions with "stream": true answers with events:

    interaction.created, step.start,
    step.delta  {"delta": {"type": "audio", "mime_type": "audio/l16",
                           "data": "<base64>", "sample_rate": 24000}},
    step.stop, interaction.completed {"interaction": {"usage": {...}}},
    error {"error": {"code": "...", "message": "..."}}

We ask for headerless 16-bit PCM (audio/l16), which plays as it arrives. A
non-streamed answer (steps[] holding a base64 WAV) is understood too.

The key travels only in the x-goog-api-key header, never in the URL.
Voices come from GET {base}/v1beta/voices (prebuilt, the extended voice
library, and the project's own designed or cloned voices).
"""

from __future__ import annotations

import base64
import json
import logging
import re
import urllib.parse
from typing import Any, Callable, Iterator

from .sse import iter_events
from .tts_base import HTTPStreamer, TTSError

log = logging.getLogger("kotodama.gemini")

# Request fields an adapter's "input" may set; the transport builds the rest.
TEMPLATE_FIELDS = {"style", "generation_config"}
_RATE = re.compile(r"rate=(\d+)")


def gemini_error(status: int | None, err: dict[str, Any] | None) -> TTSError:
    err = err if isinstance(err, dict) else {}
    message = str(err.get("message") or "")
    state = str(err.get("status") or "")
    code = err.get("code")
    http = status or (code if isinstance(code, int) else None)
    low = f"{message} {state} {code}".lower()
    shown = message or state or str(http or "error")
    if http in (401, 403) or "api key" in low or "api_key" in low or "permission_denied" in low \
            or "unauthenticated" in low:
        return TTSError("Gemini rejected the API key (GEMINI_API_KEY)", http, "auth")
    if http == 429 or "resource_exhausted" in low or "rate limit" in low:
        if any(w in low for w in ("per day", "daily", "billing", "exceeded your current quota")):
            return TTSError(f"Gemini: quota used up ({shown})", http, "quota")
        return TTSError(f"Gemini is rate limiting ({shown})", http, "rate")
    if "voice" in low and any(w in low for w in ("not found", "invalid", "unknown", "not exist", "unsupported")):
        return TTSError(f"Gemini voice not available ({shown})", http, "voice")
    if http == 404 or ("model" in low and any(w in low for w in ("not found", "not supported", "unsupported"))):
        return TTSError(f"Gemini refused the model: {shown}", http, "model")
    if http == 400 or "invalid_argument" in low:
        return TTSError(f"Gemini refused the request: {shown}", http, "input")
    return TTSError(f"Gemini error: {shown}", http, None)


def _http_error(status: int, body: bytes) -> TTSError:
    try:
        parsed = json.loads(body)
        err = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(err, list) and err:
            err = err[0]
        if isinstance(err, dict):
            return gemini_error(status, err)
    except ValueError:
        pass
    return gemini_error(status, {"message": body[:200].decode(errors="replace")})


def _audio(block: dict[str, Any], rate: int) -> tuple[bytes, int, str] | None:
    """(bytes, sample rate, 'pcm' | 'auto') from an audio delta or content block."""
    data = block.get("data")
    if not isinstance(data, str) or not data:
        return None
    try:
        raw = base64.b64decode(data)
    except (ValueError, TypeError) as exc:
        raise TTSError("Gemini sent audio that isn't base64", code="network") from exc
    mime = str(block.get("mime_type") or "audio/l16").lower()
    m = _RATE.search(mime)
    sample_rate = int(block.get("sample_rate") or (m.group(1) if m else 0) or rate)
    kind = "pcm" if ("l16" in mime or "pcm" in mime) else "auto"
    return raw, sample_rate, kind


class GeminiClient:
    def __init__(self, base_url: str, api_key: str | None, *, timeout: float = 30.0):
        self.api_key = api_key
        self.http = HTTPStreamer(base_url.rstrip("/"), timeout)

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise TTSError("no Gemini API key (set GEMINI_API_KEY)", code="auth")
        return {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

    def stream(self, body: dict[str, Any], rate: int,
               on_usage: Callable[[dict[str, Any]], None] | None = None) -> Iterator[tuple[bytes, int, str]]:
        """Yield (audio bytes, sample rate, kind) as the deltas arrive."""
        raw = self.http.stream("POST", "/v1beta/interactions", json.dumps(body).encode("utf-8"), self._headers(),
                               _http_error, align=False)
        try:
            for event in iter_events(raw):
                kind = event.get("event_type")
                if kind == "error" or (kind is None and isinstance(event.get("error"), dict)):
                    raise gemini_error(None, event.get("error"))
                if kind == "step.delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "audio":
                        chunk = _audio(delta, rate)
                        if chunk:
                            yield chunk
                elif kind == "interaction.completed":
                    interaction = event.get("interaction") or {}
                    status = str(interaction.get("status") or "").lower()
                    if status in ("failed", "cancelled", "canceled"):
                        errors = interaction.get("errors") or [{}]
                        raise gemini_error(None, errors[0] if isinstance(errors, list) else errors)
                    usage = interaction.get("usage") or event.get("usage")
                    if isinstance(usage, dict) and on_usage is not None:
                        on_usage(usage)
                elif kind is None and isinstance(event.get("steps"), list):      # a non-streamed answer
                    blocks = [c for st in event["steps"] if isinstance(st, dict) and st.get("type") == "model_output"
                              for c in (st.get("content") or []) if isinstance(c, dict) and c.get("type") == "audio"]
                    if blocks:
                        chunk = _audio(blocks[-1], rate)
                        if chunk:
                            yield chunk
                    if isinstance(event.get("usage"), dict) and on_usage is not None:
                        on_usage(event["usage"])
        finally:
            raw.close()

    def fetch_voices(self) -> list[dict[str, Any]]:
        """Every voice the project can use (paged)."""
        voices: list[dict[str, Any]] = []
        token = None
        for _ in range(20):
            query = {"page_size": "1000"}
            if token:
                query["page_token"] = token
            status, _, data = self.http.request("GET", "/v1beta/voices?" + urllib.parse.urlencode(query), None,
                                                self._headers())
            if status != 200:
                raise _http_error(status, data)
            try:
                parsed = json.loads(data)
            except ValueError as exc:
                raise TTSError("Gemini returned a non-JSON voice list", code="network") from exc
            voices.extend(v for v in parsed.get("voices") or [] if isinstance(v, dict))
            token = parsed.get("next_page_token") or parsed.get("nextPageToken")
            if not token:
                break
        return voices

    def warm(self) -> None:
        if self.api_key:
            self.http.warm()

    def close(self) -> None:
        self.http.close()
