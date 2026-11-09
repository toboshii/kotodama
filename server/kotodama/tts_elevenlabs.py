"""ElevenLabs text-to-speech.

ElevenLabs has no free-text acting directions, so a profile becomes:
  - a voice, auto-cast from this account's voices (casting.py)
    unless the profile names one explicitly;
  - voice_settings: speed from pace, stability from temperament;
  - on models that understand them (eleven_v4*, eleven_v3*), at most one
    leading audio tag such as [excited], chosen from the documented set.

Streams POST /v1/text-to-speech/{voice}/stream?output_format=pcm_24000, raw
16-bit mono PCM that drops straight into the shared player/cache path.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
from typing import Any, Callable, Iterator

from .casting import CastingMemory, VoiceCatalog, cast
from .config import Config
from .delivery import speed_for, tag_for, temperament
from .tts_base import HTTPStreamer, TTSError, VoiceRequest, error_detail

log = logging.getLogger("kotodama.tts")

TAG_MODEL_PREFIXES = ("eleven_v4", "eleven_v3")
RETRY_DELAY = 0.7


class ElevenLabsTTS:
    name = "elevenlabs"
    response_format = "pcm"
    dynamic_voices = True     # voices come from the account, not a fixed list

    def __init__(self, cfg: Config, *, notify: Callable[[str], None] | None = None):
        self.api_key = cfg.api_key("elevenlabs")
        self.configured_model: str = str(cfg.get("elevenlabs.model", "eleven_v4_turbo"))
        self.fallback_model: str = str(cfg.get("elevenlabs.fallback_model", "eleven_flash_v2_5"))
        self.fell_back = False
        self.output_format: str = str(cfg.get("elevenlabs.output_format", "pcm_24000"))
        if not re.fullmatch(r"pcm_\d+", self.output_format):
            raise ValueError("elevenlabs.output_format must be a raw PCM format such as pcm_24000")
        self.sample_rate = int(self.output_format.split("_")[1])
        self.audio_tags = bool(cfg.get("elevenlabs.audio_tags", True))
        base = cfg.get("elevenlabs.voice_settings", {}) or {}
        self.base_settings = {
            "stability": float(base.get("stability", 0.5)),
            "similarity_boost": float(base.get("similarity_boost", 0.75)),
            "style": float(base.get("style", 0.0)),
            "use_speaker_boost": bool(base.get("use_speaker_boost", True)),
        }
        cpc = cfg.get("elevenlabs.credits_per_char")
        self._credits_per_char = float(cpc) if isinstance(cpc, (int, float)) and not isinstance(cpc, bool) else None
        self.http = HTTPStreamer(str(cfg.get("elevenlabs.base_url", "https://api.elevenlabs.io")),
                                 float(cfg.get("elevenlabs.timeout", 30)))
        cache_dir = cfg.path("cache.dir")
        self.catalog = VoiceCatalog(cache_dir / "elevenlabs_voices.json", fetch=self.fetch_voices,
                                    refresh_hours=float(cfg.get("elevenlabs.voices.refresh_hours", 24)),
                                    exclude=list(cfg.get("elevenlabs.voices.exclude", []) or []),
                                    label="ElevenLabs voice")
        self.memory = CastingMemory(cache_dir / "elevenlabs_casting.json")
        self.notify = notify or (lambda _msg: None)
        self._sync_lock = threading.Lock()
        self._last_catalog_error = 0.0

    # -- model ------------------------------------------------------------

    @property
    def model(self) -> str:
        return self.fallback_model if self.fell_back else self.configured_model

    def supports_tags(self, model: str | None = None) -> bool:
        return (model or self.model).startswith(TAG_MODEL_PREFIXES)

    def credits_per_char(self) -> float:
        if self._credits_per_char is not None:
            return self._credits_per_char
        return 0.5 if self.model.startswith("eleven_flash") else 1.0

    def status_text(self) -> str:
        note = f" (fallback from {self.configured_model})" if self.fell_back else ""
        return f"elevenlabs/{self.model}{note}, {len(self.catalog.voices)} voices"

    # -- voices -----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"xi-api-key": self.api_key or "", "Content-Type": "application/json"}

    def fetch_voices(self) -> list[dict[str, Any]]:
        """Every voice this account can use (GET /v2/voices, paged)."""
        if not self.api_key:
            raise TTSError("no ElevenLabs API key (set ELEVENLABS_API_KEY)", code="auth")
        voices: list[dict[str, Any]] = []
        token = None
        for _ in range(50):                      # 5,000 voices is plenty of safety margin
            query = {"page_size": "100", "include_total_count": "false"}
            if token:
                query["next_page_token"] = token
            data = self.http.get_json("/v2/voices?" + urllib.parse.urlencode(query), self._headers(), self._error)
            voices.extend(v for v in data.get("voices", []) if isinstance(v, dict))
            token = data.get("next_page_token")
            if not data.get("has_more") or not token:
                break
        return voices

    def ensure_catalog(self, wait: bool = True) -> None:
        """Make sure there are voices to cast from. With none at all, fetch
        them (wait=True blocks the caller, for line threads; wait=False starts
        a background fetch, for the network thread). An old list is refreshed
        quietly in the background."""
        if not self.api_key:
            return
        if self.catalog.voices:
            if self.catalog.stale:
                self.catalog.sync_in_background()
            return
        if not wait:
            self.catalog.sync_in_background()
            return
        with self._sync_lock:                       # concurrent first lines share one fetch
            if self.catalog.voices:
                return
            try:
                self.catalog.sync()
            except Exception as exc:  # noqa: BLE001
                if time.monotonic() - self._last_catalog_error > 60:
                    self._last_catalog_error = time.monotonic()
                    self.notify(f"could not load ElevenLabs voices: {exc}")

    def resolve_voice(self, name_or_id: str) -> str | None:
        self.ensure_catalog()
        voice = self.catalog.find(name_or_id)
        return voice.id if voice else None

    def display(self, voice_id: str | None) -> str:
        voice = self.catalog.get(voice_id)
        return voice.name if voice else (voice_id or "none")

    def forget_casting(self, ident: str) -> None:
        self.memory.forget(ident)

    def voice_settings(self, profile: dict[str, Any], ident: str) -> dict[str, Any]:
        settings = dict(self.base_settings)
        mood = temperament(profile)
        if ident.lower() == "@narrator":
            settings["stability"] = 0.6
        elif mood == "expressive":
            settings["stability"] = 0.35
        elif mood == "steady":
            settings["stability"] = 0.65
        settings["speed"] = speed_for(profile)
        return settings

    def audio_tag(self, profile: dict[str, Any]) -> str | None:
        if not (self.audio_tags and self.supports_tags()):
            return None
        return tag_for(profile)

    def voice_request(self, profile: dict[str, Any], ident: str) -> VoiceRequest:
        self.ensure_catalog()
        explicit = profile.get("voice")
        if explicit and self.catalog.get(explicit):
            voice_id = explicit
        else:
            # No choice made, or an id from another account's shared library.
            voice_id = cast(self.catalog, profile, ident, self.memory) or ""
        settings = self.voice_settings(profile, ident)
        tag = self.audio_tag(profile)
        cache_tag = json.dumps({"settings": settings, "tag": tag}, sort_keys=True, separators=(",", ":"))
        return VoiceRequest(voice=voice_id, options={"voice_settings": settings, "tag": tag},
                            cache_tag=cache_tag, label=self.display(voice_id))

    # -- connections ------------------------------------------------------

    def warm(self) -> None:
        if self.api_key:
            self.http.warm()

    def close(self) -> None:
        self.http.close()

    # -- synthesis --------------------------------------------------------

    @staticmethod
    def _error(status: int, body: bytes) -> TTSError:
        code, message = error_detail(body)
        detail = f"{code} {message}".lower()
        if "quota" in detail or "credit" in detail or status == 402:
            return TTSError("ElevenLabs: out of credits (quota exceeded)", status, "quota")
        if status == 401:
            return TTSError("ElevenLabs rejected the API key", status, "auth")
        if status == 429:
            kind = "concurrency" if "concurren" in detail else "rate"
            return TTSError(f"ElevenLabs is busy ({message or code or 'rate limited'})", status, kind)
        if status == 404 and "voice" in detail:
            return TTSError("ElevenLabs voice not found (run `kotodama_server.py voices sync`)", status, "voice")
        if status in (400, 422) and "model" in detail:
            return TTSError(f"ElevenLabs refused the model: {message or code}", status, "model")
        return TTSError(f"ElevenLabs HTTP {status}: {message or code}", status, None)

    def _body(self, text: str, request: VoiceRequest, model: str) -> bytes:
        tag = request.options.get("tag") if self.supports_tags(model) else None
        payload = {
            "text": f"{tag} {text}" if tag else text,
            "model_id": model,
            "voice_settings": request.options.get("voice_settings") or dict(self.base_settings),
        }
        return json.dumps(payload).encode("utf-8")

    def stream(self, text: str, request: VoiceRequest) -> Iterator[bytes]:
        if not self.api_key:
            raise TTSError("no ElevenLabs API key (set ELEVENLABS_API_KEY or elevenlabs.api_key)", code="auth")
        if not request.voice:
            raise TTSError("no ElevenLabs voice available (run `kotodama_server.py voices sync`)", code="voice")
        path = (f"/v1/text-to-speech/{urllib.parse.quote(request.voice, safe='')}/stream"
                f"?output_format={self.output_format}")
        retried = False
        while True:
            model = self.model
            try:
                yield from self.http.stream("POST", path, self._body(text, request, model), self._headers(),
                                            self._error)
                return
            except TTSError as exc:
                # Every status error is raised before any audio is yielded, so
                # retrying here never duplicates audio.
                if exc.code == "model" and not self.fell_back and model != self.fallback_model:
                    self.fell_back = True
                    log.warning("%s rejected (%s); falling back to %s", model, exc, self.fallback_model)
                    self.notify(f"ElevenLabs model {model} isn't available here; using {self.fallback_model}")
                    continue
                if exc.code in ("concurrency", "rate") and not retried:
                    retried = True
                    time.sleep(RETRY_DELAY)
                    continue
                if exc.code == "voice":
                    # Deleted from the account since the last sync: stop using
                    # it so the next line re-casts.
                    self.catalog.drop(request.voice)
                    self.memory.forget_voice(request.voice)
                raise
