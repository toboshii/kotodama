"""Shared pieces for TTS providers.

A provider turns a resolved voice profile into a VoiceRequest (which voice,
which provider-specific options, and a cache tag that captures everything
about the request that changes the audio), then streams raw 16-bit mono PCM
for a line of text. HTTPStreamer holds the warm keep-alive connection pool
and the chunked-streaming logic both providers use.
"""

from __future__ import annotations

import http.client
import json
import logging
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

log = logging.getLogger("kotodama.tts")

IDLE_LIMIT = 45.0   # drop pooled connections idle longer than this (servers close at ~60s)
CHUNK = 4096


class TTSError(Exception):
    """A synthesis failure, worded for the player's chat log.

    `code` classifies it for retry/fallback decisions: auth, quota,
    concurrency, rate, model, voice, network, or None."""

    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class VoiceRequest:
    voice: str                       # provider voice name / id
    options: dict[str, Any] = field(default_factory=dict)
    cache_tag: str = ""              # everything besides voice/text/model that changes the audio
    label: str = ""                  # human-readable voice name for //koto voice


def error_detail(body: bytes) -> tuple[str, str]:
    """Best-effort (status, message) from a provider's JSON error body."""
    text = body.decode("utf-8", "replace")
    try:
        data = json.loads(text)
    except ValueError:
        return "", text.strip()[:300]
    err = data.get("error") if isinstance(data, dict) else None
    detail = data.get("detail") if isinstance(data, dict) else None
    if isinstance(err, dict):                                   # OpenAI shape
        return str(err.get("code") or err.get("type") or ""), str(err.get("message") or "")
    if isinstance(detail, dict):                                # ElevenLabs shape
        return str(detail.get("status") or detail.get("code") or ""), str(detail.get("message") or "")
    if isinstance(detail, list) and detail:                     # 422 validation list
        first = detail[0] if isinstance(detail[0], dict) else {}
        loc = ".".join(str(p) for p in first.get("loc", []))
        return "validation_error", f"{loc}: {first.get('msg', '')}".strip(": ")
    if isinstance(detail, str):
        return "", detail
    return "", text.strip()[:300]


class HTTPStreamer:
    """Keep-alive HTTP(S) connections to one API host."""

    def __init__(self, base_url: str, timeout: float):
        parsed = urllib.parse.urlparse(base_url)
        self.https = parsed.scheme == "https"
        self.host = parsed.hostname or ""
        self.port = parsed.port or (443 if self.https else 80)
        self.prefix = parsed.path.rstrip("/")
        self.timeout = timeout
        self._pool: list[tuple[http.client.HTTPConnection, float]] = []
        self._lock = threading.Lock()
        self._closed = False

    def _new_conn(self) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
        return cls(self.host, self.port, timeout=self.timeout)

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
            if not self._closed:
                self._pool.append((conn, time.monotonic()))
                return
        conn.close()                 # closed meanwhile (e.g. a warm-up finishing late)

    @property
    def pooled(self) -> int:
        with self._lock:
            return len(self._pool)

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

    def close(self) -> None:
        with self._lock:
            self._closed = True
            pool, self._pool = self._pool, []
        for conn, _ in pool:
            conn.close()

    def stream(self, method: str, path: str, body: bytes | None, headers: dict[str, str],
               on_error: Callable[[int, bytes], TTSError], align: bool = True) -> Iterator[bytes]:
        """Yield the response body as soon as it arrives: in sample-aligned
        (even-length) chunks for raw PCM, or as-is with align=False (text
        such as server-sent events). A non-200 status raises
        on_error(status, body) before anything is yielded. Closing the
        generator early drops the connection instead of returning it to the
        pool."""
        resp = None
        conn = None
        for attempt in (0, 1):
            conn, pooled = self._take()
            try:
                conn.request(method, self.prefix + path, body=body, headers=headers)
                resp = conn.getresponse()
                break
            except (http.client.HTTPException, ConnectionError, OSError) as exc:
                conn.close()
                resp = None
                # A stale pooled connection fails once; retry on a fresh one.
                if not (pooled and attempt == 0):
                    raise TTSError(f"TTS request failed: {exc}", code="network") from exc
        assert resp is not None and conn is not None

        reusable = False
        try:
            if resp.status != 200:
                raise on_error(resp.status, resp.read(4000))
            carry = b""
            while True:
                # read1 returns as soon as any data arrives (read(n) would
                # wait to fill n bytes on a chunked response).
                chunk = resp.read1(CHUNK)
                if not chunk:
                    break
                if not align:
                    yield chunk
                    continue
                chunk = carry + chunk
                if len(chunk) % 2:
                    chunk, carry = chunk[:-1], chunk[-1:]
                else:
                    carry = b""
                if chunk:
                    yield chunk
            reusable = not resp.will_close
        except (http.client.HTTPException, OSError) as exc:
            raise TTSError(f"TTS stream interrupted: {exc}", code="network") from exc
        finally:
            if reusable:
                self._give_back(conn)
            else:
                conn.close()

    def request(self, method: str, path: str, body: bytes | None,
                headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        """One whole request on a pooled keep-alive connection:
        (status, lower-cased headers, body). Any status is returned."""
        for attempt in (0, 1):
            conn, pooled = self._take()
            try:
                conn.request(method, self.prefix + path, body=body, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
            except (http.client.HTTPException, ConnectionError, OSError) as exc:
                conn.close()
                # A stale pooled connection fails once; retry on a fresh one.
                if not (pooled and attempt == 0):
                    raise TTSError(f"request failed: {exc}", code="network") from exc
                continue
            if resp.will_close:
                conn.close()
            else:
                self._give_back(conn)
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
        raise TTSError("request failed", code="network")       # pragma: no cover - loop always returns

    def get_json(self, path: str, headers: dict[str, str],
                 on_error: Callable[[int, bytes], TTSError]) -> Any:
        """One-off JSON GET on its own connection (catalog fetches)."""
        conn = self._new_conn()
        try:
            conn.request("GET", self.prefix + path, headers=headers)
            resp = conn.getresponse()
            body = resp.read()
        except (http.client.HTTPException, OSError) as exc:
            raise TTSError(f"request failed: {exc}", code="network") from exc
        finally:
            conn.close()
        if resp.status != 200:
            raise on_error(resp.status, body[:4000])
        try:
            return json.loads(body)
        except ValueError as exc:
            raise TTSError("unexpected (non-JSON) response", code="network") from exc
