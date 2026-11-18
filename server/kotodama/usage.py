"""Daily usage counters and a rough cost estimate (shown by //koto status)."""

from __future__ import annotations

import datetime as _dt
import json
import threading
from pathlib import Path

# List-price estimate for gpt-4o-mini-tts; check your provider's pricing page.
# ElevenLabs is billed in credits per character; dollars come from the
# configured price per 1,000 credits (elevenlabs.price.per_1k_credits).
EST_USD_PER_AUDIO_MINUTE = 0.015
EST_USD_PER_MILLION_INPUT_TOKENS = 0.60


class Usage:
    def __init__(self, path: Path, el_usd_per_1k_credits: float | None = None):
        self.path = Path(path)
        self.el_usd_per_1k_credits = el_usd_per_1k_credits
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

    def add_tts(self, provider: str, chars: int, seconds: float, credits: float | None = None) -> None:
        if provider == "elevenlabs":
            self.add(el_requests=1, el_chars=chars, el_seconds=seconds, el_credits=credits or 0.0)
        elif provider == "openai":
            self.add(tts_requests=1, tts_chars=chars, tts_seconds=seconds)
        else:
            # Adapter-driven providers (Replicate models, OpenAI-compatible
            # servers); they add their own p:<name>:predict_s / cost.
            self.add(**{f"p:{provider}:requests": 1, f"p:{provider}:chars": chars,
                        f"p:{provider}:seconds": seconds})

    @staticmethod
    def _adapter_totals(d: dict[str, float]) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for key, value in d.items():
            if key.startswith("p:") and key.count(":") == 2:
                _, name, field = key.split(":")
                out.setdefault(name, {})[field] = value
        return out

    def today(self) -> dict[str, float]:
        with self._lock:
            return dict(self._days.get(self._today(), {}))

    def summary(self) -> str:
        d = self.today()
        minutes = d.get("tts_seconds", 0) / 60
        chars = d.get("tts_chars", 0)
        cost = minutes * EST_USD_PER_AUDIO_MINUTE + (chars / 4) / 1e6 * EST_USD_PER_MILLION_INPUT_TOKENS
        adapters = self._adapter_totals(d)
        synthesized = d.get("tts_requests", 0) + d.get("el_requests", 0) + sum(
            a.get("requests", 0) for a in adapters.values())
        text = (f"today: {int(synthesized)} synthesized, "
                f"{int(d.get('cache_hits', 0))} from cache, {int(d.get('infer_calls', 0))} voice-casting calls")
        if d.get("tts_requests"):
            text += f", OpenAI ~${cost:.3f} est."
        if d.get("el_requests"):
            credits = d.get("el_credits", 0)
            dollars = ""
            if self.el_usd_per_1k_credits:
                dollars = f", ~${credits * self.el_usd_per_1k_credits / 1000:.3f}"
            text += (f", ElevenLabs {int(d.get('el_chars', 0))} chars"
                     f" (~{int(credits)} credits{dollars} est.)")
        for name, a in sorted(adapters.items()):
            if not a.get("requests") and not a.get("predictions"):
                continue
            text += f", {name} {int(a.get('chars', 0))} chars"
            if a.get("billed_chars"):
                text += f" ({int(a['billed_chars'])} billed)"
            if a.get("output_tokens"):
                text += f" ({int(a['output_tokens'])} audio tokens)"
            if a.get("predict_s"):
                text += f" / {a['predict_s']:.0f} s compute"
            if a.get("cost"):
                text += f" (~${a['cost']:.3f} est.)"
        return text
