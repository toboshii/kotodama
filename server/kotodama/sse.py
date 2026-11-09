"""Incremental parsing of streamed JSON events.

Streaming TTS APIs (MiniMax T2A, Gemini's Interactions API) answer with
server-sent events whose payloads are JSON objects. Their docs don't always
pin down the exact framing, so this accepts all of:

    data: {"...": ...}          SSE lines (comments, event:, id:, [DONE] ignored)
    {"...": ...}                one JSON object per line
    {                           a single, possibly pretty-printed JSON body
      "...": ...
    }
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Iterator

from .tts_base import TTSError


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
            raise TTSError(f"the TTS service sent an unreadable event: {exc}", code="network") from exc
        if event is not None:
            yield event
    if pending.strip():
        raise TTSError("the TTS service response ended mid-event", code="network")
