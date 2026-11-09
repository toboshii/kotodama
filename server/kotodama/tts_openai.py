"""OpenAI text-to-speech over a warm, keep-alive HTTPS connection.

POST /v1/audio/speech with response_format "pcm" streams raw 24 kHz, 16-bit,
mono little-endian samples, so playback can start on the first chunk.
Personality is carried by the free-text `instructions` field.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterator

from .config import Config
from .profiles import describe
from .tts_base import HTTPStreamer, TTSError, VoiceRequest, error_detail

__all__ = ["OpenAITTS", "TTSError"]

log = logging.getLogger("kotodama.tts")


class OpenAITTS:
    name = "openai"
    response_format = "pcm"
    dynamic_voices = False    # fixed voice list (server/archetypes.json)

    def __init__(self, cfg: Config):
        self.model: str = cfg.get("tts.model")
        self.timeout: float = float(cfg.get("tts.timeout", 30))
        self.sample_rate: int = int(cfg.get("tts.sample_rate", 24000))
        self.api_key = cfg.api_key("tts")
        self.http = HTTPStreamer(str(cfg.get("tts.base_url")), self.timeout)
        self.notify = lambda _msg: None

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

    @staticmethod
    def _error(status: int, body: bytes) -> TTSError:
        code, message = error_detail(body)
        kind = {401: "auth", 429: "rate"}.get(status)
        if code == "insufficient_quota":
            kind = "quota"
        return TTSError(f"TTS HTTP {status}: {message or code}", status, kind)

    def stream(self, text: str, request: VoiceRequest) -> Iterator[bytes]:
        """Yield PCM chunks (always an even number of bytes)."""
        if not self.api_key:
            raise TTSError("no OpenAI API key (set OPENAI_API_KEY or tts.api_key in server/config.json)",
                           code="auth")
        body = self._body(text, request.voice, request.options.get("instructions", ""))
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        yield from self.http.stream("POST", "/v1/audio/speech", body, headers, self._error)
