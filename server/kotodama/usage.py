"""Daily usage counters and a rough cost estimate (shown by //koto status)."""

from __future__ import annotations

import datetime as _dt
import json
import threading
from pathlib import Path

# List-price estimate for gpt-4o-mini-tts; check your provider's pricing page.
EST_USD_PER_AUDIO_MINUTE = 0.015
EST_USD_PER_MILLION_INPUT_TOKENS = 0.60


class Usage:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._days: dict[str, dict[str, float]] = {}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._days = data
        except (OSError, ValueError):
            pass

    @staticmethod
    def _today() -> str:
        return _dt.date.today().isoformat()

    def add(self, **counts: float) -> None:
        with self._lock:
            day = self._days.setdefault(self._today(), {})
            for k, v in counts.items():
                day[k] = day.get(k, 0) + v
            self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._days, fh, sort_keys=True)
            tmp.replace(self.path)
        except OSError:
            pass

    def today(self) -> dict[str, float]:
        with self._lock:
            return dict(self._days.get(self._today(), {}))

    def summary(self) -> str:
        d = self.today()
        minutes = d.get("tts_seconds", 0) / 60
        chars = d.get("tts_chars", 0)
        cost = minutes * EST_USD_PER_AUDIO_MINUTE + (chars / 4) / 1e6 * EST_USD_PER_MILLION_INPUT_TOKENS
        return (f"today: {int(d.get('tts_requests', 0))} synthesized, {int(d.get('cache_hits', 0))} from cache, "
                f"{int(d.get('infer_calls', 0))} voice-casting calls, ~${cost:.3f} est. TTS")
