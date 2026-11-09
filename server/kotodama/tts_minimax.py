"""MiniMax's own text-to-speech API (T2A v2), streamed.

POST {base}/v1/t2a_v2 with "stream": true answers with server-sent events,
each a JSON object carrying a hex-encoded slice of audio:

    data: {"data": {"audio": "<hex>", "status": 1}, "base_resp": {"status_code": 0, ...}}

status 2 marks the last event. Two things to get right:
  - errors come back inside an HTTP 200 as base_resp.status_code != 0;
  - unless stream_options.exclude_aggregated_audio is set (we set it), the
    final event repeats the whole line's audio. The parser yields only what
    goes beyond the audio already received, so either behaviour is safe.

The voice list comes from POST {base}/v1/get_voice: system voices plus the
account's own cloned and designed ones.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Any, Callable, Iterable, Iterator

from .tts_base import HTTPStreamer, TTSError

log = logging.getLogger("kotodama.minimax")

# Request fields an adapter's "input" may set (the transport sets model, text,
# stream and audio_setting itself), for doctor's sanity check.
BODY_FIELDS = {"voice_setting", "pronunciation_dict", "language_boost", "text_normalization_mode", "voice_modify",
               "subtitle_enable", "subtitle_type", "timbre_weights", "continuous_sound", "aigc_watermark"}
VOICE_SETTING_FIELDS = {"voice_id", "speed", "vol", "pitch", "emotion", "text_normalization", "latex_read",
                        "english_normalization"}


def unknown_fields(template: dict[str, Any]) -> list[str]:
    bad = [k for k in template if k not in BODY_FIELDS]
    vs = template.get("voice_setting")
    if isinstance(vs, dict):
        bad += [f"voice_setting.{k}" for k in vs if k not in VOICE_SETTING_FIELDS]
    return bad


def minimax_error(code: int, message: str | None) -> TTSError:
    msg = message or str(code)
    if code == 1004:
        return TTSError("MiniMax rejected the API key (MINIMAX_API_KEY)", code="auth")
    if code == 1008:
        return TTSError("MiniMax: insufficient balance", code="quota")
    if code in (1002, 1039):
        return TTSError(f"MiniMax is rate limiting ({msg})", code="rate")
    if code in (1042, 2013):
        return TTSError(f"MiniMax refused the request: {msg}", code="input")
    if code == 2054:
        return TTSError(f"MiniMax voice not found ({msg})", code="voice")
    if code == 1001:
        return TTSError(f"MiniMax timed out ({msg})", code="timeout")
    return TTSError(f"MiniMax error {code}: {msg}", code=None)


def _http_error(status: int, body: bytes) -> TTSError:
    try:
        base = (json.loads(body) or {}).get("base_resp") or {}
        if base.get("status_code"):
            return minimax_error(int(base["status_code"]), base.get("status_msg"))
    except (ValueError, AttributeError, TypeError):
        pass
    if status in (401, 403):
        return TTSError("MiniMax rejected the API key (MINIMAX_API_KEY)", status, "auth")
    if status == 429:
        return TTSError("MiniMax is rate limiting", status, "rate")
    return TTSError(f"MiniMax HTTP {status}: {body[:200].decode(errors='replace')}", status, None)


def iter_events(chunks: Iterable[bytes]) -> Iterator[dict[str, Any]]:
    """JSON events from an SSE stream ("data: {...}" lines), from bare JSON
    lines, or from a single (possibly pretty-printed) JSON body."""
    buf = b""
    pending = ""                      # bare JSON spread over several lines

    def handle(line: bytes) -> dict[str, Any] | None:
        nonlocal pending
        text = line.decode("utf-8", "replace").strip()
        if not pending:
            if not text or text.startswith((":", "event:", "id:", "retry:")):
                return None
            if text.startswith("data:"):
                text = text[5:].strip()
                if not text or text == "[DONE]":
                    return None
                return json.loads(text)
        candidate = f"{pending}\n{text}" if pending else text
        try:
            obj = json.loads(candidate)
        except ValueError:
            pending = candidate
            return None
        pending = ""
        return obj if isinstance(obj, dict) else None

    def lines() -> Iterator[bytes]:
        nonlocal buf
        for chunk in chunks:
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                yield line
        if buf:
            yield buf                 # a last line without a newline

    for line in lines():
        try:
            event = handle(line)
        except ValueError as exc:
            raise TTSError(f"MiniMax sent an unreadable event: {exc}", code="network") from exc
        if event is not None:
            yield event
    if pending.strip():
        raise TTSError("MiniMax response ended mid-event", code="network")


class MiniMaxClient:
    def __init__(self, base_url: str, api_key: str | None, *, timeout: float = 30.0, group_id: str | None = None):
        self.api_key = api_key
        self.group_id = group_id
        self.http = HTTPStreamer(base_url.rstrip("/"), timeout)

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise TTSError("no MiniMax API key (set MINIMAX_API_KEY)", code="auth")
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _path(self, path: str) -> str:
        return f"{path}?{urllib.parse.urlencode({'GroupId': self.group_id})}" if self.group_id else path

    def stream(self, payload: dict[str, Any],
               on_info: Callable[[dict[str, Any]], None] | None = None) -> Iterator[bytes]:
        """Yield raw audio bytes (in the requested format) as they arrive."""
        body = json.dumps(payload).encode("utf-8")
        received = bytearray()
        raw = self.http.stream("POST", self._path("/v1/t2a_v2"), body, self._headers(), _http_error, align=False)
        try:
            for event in iter_events(raw):
                base = event.get("base_resp") or {}
                code = base.get("status_code") or 0
                if code:
                    raise minimax_error(int(code), base.get("status_msg"))
                data = event.get("data") or {}
                hex_audio = data.get("audio") or ""
                if hex_audio:
                    try:
                        audio = bytes.fromhex(hex_audio)
                    except ValueError as exc:
                        raise TTSError("MiniMax sent audio that isn't hex", code="network") from exc
                    if data.get("status") == 2 and received and audio.startswith(bytes(received)):
                        audio = audio[len(received):]        # the aggregated repeat of the whole line
                    if audio:
                        received += audio
                        yield audio
                if event.get("extra_info") and on_info is not None and data.get("status") == 2:
                    on_info(event["extra_info"])
        finally:
            raw.close()

    def fetch_voices(self) -> dict[str, Any]:
        body = json.dumps({"voice_type": "all"}).encode("utf-8")
        status, _, data = self.http.request("POST", self._path("/v1/get_voice"), body, self._headers())
        if status != 200:
            raise _http_error(status, data)
        try:
            parsed = json.loads(data)
        except ValueError as exc:
            raise TTSError("MiniMax returned a non-JSON voice list", code="network") from exc
        base = parsed.get("base_resp") or {}
        if base.get("status_code"):
            raise minimax_error(int(base["status_code"]), base.get("status_msg"))
        return parsed

    def warm(self) -> None:
        if self.api_key:
            self.http.warm()

    def close(self) -> None:
        self.http.close()
