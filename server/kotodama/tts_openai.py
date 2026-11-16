"""OpenAI text-to-speech over a warm, keep-alive HTTPS connection.

POST /v1/audio/speech with response_format "pcm" streams raw 24 kHz, 16-bit,
mono little-endian samples, so playback can start on the first chunk.
Personality is carried by the free-text `instructions` field.

OpenAI shuts down every /v1/audio/speech model on 2027-01-06, so this
provider warns wherever it's listed or chosen (see shutdown_notice).
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

from .config import Config
from .profiles import describe
from .tts_base import HTTPStreamer, TTSError, VoiceRequest, error_detail

__all__ = ["OpenAITTS", "SPEECH_SHUTDOWN", "TTSError", "shutdown_label", "shutdown_notice"]

log = logging.getLogger("kotodama.tts")

# The day OpenAI turns off every /v1/audio/speech model (gpt-4o-mini-tts,
# tts-1, tts-1-hd). Their replacement speaks only over the Realtime API.
SPEECH_SHUTDOWN = date(2027, 1, 6)


def shutdown_notice(base_url: str | None, today: date | None = None) -> str | None:
    """The warning to show for OpenAI's speech shutdown, or None when
    base_url isn't OpenAI's own API (a self-hosted server isn't affected)."""
    host = (urlsplit(str(base_url or "")).hostname or "").lower()
    if host != "api.openai.com":
        return None
    when = SPEECH_SHUTDOWN.isoformat()
    if (today or date.today()) < SPEECH_SHUTDOWN:
        return f"OpenAI's speech models shut down on {when}; switch with //koto provider inworld (or another provider)"
    return f"OpenAI's speech models were shut down on {when}; switch with //koto provider inworld (or another provider)"


def shutdown_label(base_url: str | None, today: date | None = None) -> str:
    """A short "shuts down 2027-01-06" for provider lists ("" if not affected)."""
    if shutdown_notice(base_url, today) is None:
        return ""
    verb = "shuts down" if (today or date.today()) < SPEECH_SHUTDOWN else "shut down"
    return f"{verb} {SPEECH_SHUTDOWN.isoformat()}"


class OpenAITTS:
    name = "openai"
    response_format = "pcm"
    dynamic_voices = False    # fixed voice list (server/archetypes.json)

    def __init__(self, cfg: Config):
        self.model: str = cfg.get("tts.model")
        self.timeout: float = float(cfg.get("tts.timeout", 30))
        self.sample_rate: int = int(cfg.get("tts.sample_rate", 24000))
        self.api_key = cfg.api_key("tts")
        self.base_url = str(cfg.get("tts.base_url"))
        self.http = HTTPStreamer(self.base_url, self.timeout)
        self.notify = lambda _msg: None
        self.today: Callable[[], date] = date.today   # tests move the clock

    def shutdown_notice(self) -> str | None:
        return shutdown_notice(self.base_url, self.today())

    # -- voices -----------------------------------------------------------

    def voice_request(self, profile: dict[str, Any], ident: str) -> VoiceRequest:
        voice = profile.get("voice") or ""
        instructions = describe(profile)
        # cache_tag is exactly the instructions text, which keeps cache keys
        # identical to the ones written before providers were pluggable.
        return VoiceRequest(voice=voice, options={"instructions": instructions},
                            cache_tag=instructions, label=voice)

    def status_text(self) -> str:
        return f"openai/{self.model}"

    # -- connections ------------------------------------------------------

    def warm(self) -> None:
        self.http.warm()

    def close(self) -> None:
        self.http.close()

    # -- synthesis --------------------------------------------------------

    def _body(self, text: str, voice: str, instructions: str) -> bytes:
        payload = {"model": self.model, "input": text, "voice": voice,
                   "response_format": self.response_format}
        if instructions and self.model.startswith("gpt-4o"):
            payload["instructions"] = instructions
        return json.dumps(payload).encode("utf-8")

    def _error(self, status: int, body: bytes) -> TTSError:
        code, message = error_detail(body)
        kind = {401: "auth", 429: "rate"}.get(status)
        if code == "insufficient_quota":
            kind = "quota"
        text = f"TTS HTTP {status}: {message or code}"
        if (status in (404, 410) or code == "model_not_found") and self.today() >= SPEECH_SHUTDOWN:
            # After the shutdown, say why instead of leaving a bare "not found".
            notice = self.shutdown_notice()
            if notice:
                text, kind = f"{text} ({notice})", "model"
        return TTSError(text, status, kind)

    def stream(self, text: str, request: VoiceRequest) -> Iterator[bytes]:
        """Yield PCM chunks (always an even number of bytes)."""
        if not self.api_key:
            raise TTSError("no OpenAI API key (set OPENAI_API_KEY or tts.api_key in server/config.json)",
                           code="auth")
        body = self._body(text, request.voice, request.options.get("instructions", ""))
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        yield from self.http.stream("POST", "/v1/audio/speech", body, headers, self._error)
