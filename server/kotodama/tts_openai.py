"""OpenAI text-to-speech over a warm, keep-alive HTTPS connection.

POST /v1/audio/speech with response_format "pcm" streams raw 24 kHz, 16-bit,
mono little-endian samples, so playback can start on the first chunk.
"""

from __future__ import annotations

import http.client
import json
import logging
import threading
import time
import urllib.parse
from typing import Iterator

from .config import Config

log = logging.getLogger("kotodama.tts")

IDLE_LIMIT = 45.0   # drop pooled connections idle longer than this (servers close at ~60s)
CHUNK = 4096


class TTSError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class OpenAITTS:
    name = "openai"
    response_format = "pcm"

    def __init__(self, cfg: Config):
        self.model: str = cfg.get("tts.model")
        self.timeout: float = float(cfg.get("tts.timeout", 30))
        self.sample_rate: int = int(cfg.get("tts.sample_rate", 24000))
        self.api_key = cfg.api_key("tts")
        parsed = urllib.parse.urlparse(str(cfg.get("tts.base_url")))
        self._https = parsed.scheme == "https"
        self._host = parsed.hostname or "api.openai.com"
        self._port = parsed.port or (443 if self._https else 80)
        self._prefix = parsed.path.rstrip("/")
        self._pool: list[tuple[http.client.HTTPConnection, float]] = []
        self._lock = threading.Lock()

    # -- connections ------------------------------------------------------

    def _new_conn(self) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
        return cls(self._host, self._port, timeout=self.timeout)

    def _take(self) -> tuple[http.client.HTTPConnection, bool]:
        """(connection, was_pooled)."""
        now = time.monotonic()
        with self._lock:
            while self._pool:
                conn, t = self._pool.pop()
                if now - t <= IDLE_LIMIT:
                    return conn, True
                conn.close()
        return self._new_conn(), False

    def _give_back(self, conn: http.client.HTTPConnection) -> None:
        with self._lock:
            self._pool.append((conn, time.monotonic()))

    def close(self) -> None:
        with self._lock:
            pool, self._pool = self._pool, []
        for conn, _ in pool:
            conn.close()

    def warm(self) -> None:
        """Open (and TLS-handshake) a connection ahead of the first line."""
        def _go() -> None:
            try:
                conn = self._new_conn()
                conn.connect()
                self._give_back(conn)
            except OSError as exc:
                log.debug("warm-up failed: %s", exc)
        with self._lock:
            if self._pool and time.monotonic() - self._pool[-1][1] < IDLE_LIMIT:
                return
        threading.Thread(target=_go, daemon=True).start()

    # -- synthesis --------------------------------------------------------

    def _body(self, text: str, voice: str, instructions: str) -> bytes:
        payload = {"model": self.model, "input": text, "voice": voice,
                   "response_format": self.response_format}
        if instructions and self.model.startswith("gpt-4o"):
            payload["instructions"] = instructions
        return json.dumps(payload).encode("utf-8")

    def stream(self, *, text: str, voice: str, instructions: str) -> Iterator[bytes]:
        """Yield PCM chunks (always an even number of bytes). Closing the
        generator early drops the connection."""
        if not self.api_key:
            raise TTSError("no OpenAI API key (set OPENAI_API_KEY or tts.api_key in server/config.json)")
        body = self._body(text, voice, instructions)
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

        resp = None
        conn = None
        for attempt in (0, 1):
            conn, pooled = self._take()
            try:
                conn.request("POST", f"{self._prefix}/v1/audio/speech", body=body, headers=headers)
                resp = conn.getresponse()
                break
            except (http.client.HTTPException, ConnectionError, OSError) as exc:
                conn.close()
                resp = None
                # A stale pooled connection fails once; retry on a fresh one.
                if not (pooled and attempt == 0):
                    raise TTSError(f"TTS request failed: {exc}") from exc
        assert resp is not None and conn is not None

        reusable = False
        try:
            if resp.status != 200:
                detail = resp.read(2000).decode("utf-8", "replace")
                try:
                    detail = json.loads(detail)["error"]["message"]
                except (ValueError, KeyError, TypeError):
                    pass
                raise TTSError(f"TTS HTTP {resp.status}: {detail}", resp.status)
            carry = b""
            while True:
                # read1 returns as soon as any data arrives (read(n) would
                # wait to fill n bytes on a chunked response).
                chunk = resp.read1(CHUNK)
                if not chunk:
                    break
                chunk = carry + chunk
                if len(chunk) % 2:
                    chunk, carry = chunk[:-1], chunk[-1:]
                else:
                    carry = b""
                if chunk:
                    yield chunk
            reusable = not resp.will_close
        except (http.client.HTTPException, OSError) as exc:
            raise TTSError(f"TTS stream interrupted: {exc}") from exc
        finally:
            if reusable:
                self._give_back(conn)
            else:
                conn.close()
