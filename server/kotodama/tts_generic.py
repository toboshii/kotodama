"""One provider class for every adapter-driven TTS backend (adapters.py):
models on Replicate and servers speaking OpenAI's /v1/audio/speech.

Replicate returns finished audio files, never a stream, so a line is split
at sentence boundaries. The first sentence is synthesized on its own and
starts playing while the rest (at most two predictions in flight) are made.
The audio is yielded strictly in order. OpenAI-compatible servers stream
PCM/WAV directly, and so does Inworld, which speaks the same request format.
MiniMax and Gemini stream through their own clients.

The Replicate token is only ever sent to the configured API host. Output
files live on replicate.delivery and are fetched without credentials.
Predictions are polled by id, never via a URL taken from a response.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
import urllib.parse
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterator

from .adapters import (TEXT_SENTINEL, Adapter, fill_text, input_properties, overlay, render,
                       schema_ranges)
from .archetypes import stable_hash
from .casting import CastingMemory, Voice, VoiceCatalog, cast, derive_labels
from .config import Config
from .decode import DecodeError, PCMDecoder, decode_all
from .delivery import (PACE_SPEED, control, emotion_for, mood_control, speed_for, steering_text,
                       style_text, voice_description)
from .fsutil import replace_file
from .tts_base import CHUNK, HTTPStreamer, TTSError, VoiceRequest, error_detail
from .tts_gemini import GeminiClient
from .tts_inworld import SPEECH_PATH as INWORLD_SPEECH_PATH, InworldClient, inworld_error, inworld_voice
from .tts_minimax import MiniMaxClient
from .voice_bank import VoiceBank

log = logging.getLogger("kotodama.tts")

POLL_EVERY = 0.5
COLD_NOTICE_AFTER = 4.0
RETRY_DELAY = 1.0
SCHEMA_MAX_AGE = 24 * 3600
SCHEMA_RETRY = 300.0          # after a failed schema fetch, wait this long before trying again
MINIMAX_MAX_CHARS = 9500      # per request (the API allows under 10,000)
GEMINI_MAX_CHARS = 4000       # per request; FFXI lines are far shorter
LIVE_RETRY_DELAY = 0.7        # streaming transports: one retry on a rate limit, before any audio
# Square brackets steer Inworld TTS-2 (silently, and until [reset]): game text must never do that.
_NO_BRACKETS = str.maketrans("[]", "()")
FIRST_MIN_CHARS = 12          # a first "sentence" shorter than this joins the next one
GROUP_CHARS = 280             # later sentences are batched up to this size


# -- text splitting -------------------------------------------------------------

_SENTENCE_END = re.compile(r"[.!?…]+[\"'”’)\]]*(?=\s)")


def _hard_split(text: str, limit: int) -> list[str]:
    """Split one over-long sentence at commas, then spaces."""
    out: list[str] = []
    while len(text) > limit:
        cut = text.rfind(", ", 0, limit)
        cut = cut + 1 if cut > limit // 3 else text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        out.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        out.append(text)
    return out


def split_text(text: str, max_chars: int, mode: str = "sentences") -> list[str]:
    text = " ".join(text.split())
    if not text:
        return []
    if mode != "sentences" and len(text) <= max_chars:
        return [text]
    sentences: list[str] = []
    start = 0
    for m in _SENTENCE_END.finditer(text):
        sentences.append(text[start:m.end()].strip())
        start = m.end()
    if text[start:].strip():
        sentences.append(text[start:].strip())
    pieces = [p for s in sentences for p in _hard_split(s, max_chars)]
    if mode != "sentences":
        group, first = max_chars, None
    else:
        group = min(max_chars, GROUP_CHARS)
        first = pieces.pop(0)
        while len(first) < FIRST_MIN_CHARS and pieces and len(first) + 1 + len(pieces[0]) <= max_chars:
            first = f"{first} {pieces.pop(0)}"
    chunks: list[str] = [first] if first else []
    current = ""
    for p in pieces:
        if current and len(current) + 1 + len(p) > group:
            chunks.append(current)
            current = p
        else:
            current = f"{current} {p}" if current else p
    if current:
        chunks.append(current)
    return chunks


def _slices(pcm: bytes) -> Iterator[bytes]:
    for i in range(0, len(pcm), CHUNK * 2):
        yield pcm[i:i + CHUNK * 2]


# -- Replicate ------------------------------------------------------------------

def _replicate_error(status: int, body: bytes) -> TTSError:
    code, message = error_detail(body)
    try:
        data = json.loads(body)
        fields = data.get("invalid_fields") if isinstance(data, dict) else None
        if isinstance(fields, list) and fields:
            message = "; ".join(f"{f.get('field', '?')}: {f.get('description', '')}" for f in fields
                                if isinstance(f, dict))[:300] or message
    except ValueError:
        pass
    if status == 401:
        return TTSError("Replicate rejected the API token (REPLICATE_API_TOKEN)", status, "auth")
    if status == 402:
        return TTSError("Replicate: billing required or out of credit", status, "quota")
    if status == 404:
        return TTSError(f"Replicate: model or prediction not found ({message or code})", status, "model")
    if status == 422:
        return TTSError(f"Replicate refused the input: {message or code}", status, "input")
    if status == 429:
        return TTSError(f"Replicate is rate limiting ({message or 'too many requests'})", status, "rate")
    return TTSError(f"Replicate HTTP {status}: {message or code}", status, None)


def _find_output_url(output: Any) -> str | None:
    if isinstance(output, str):
        return output if output.startswith(("https://", "http://", "data:")) else None
    if isinstance(output, list):
        for item in output:
            found = _find_output_url(item)
            if found:
                return found
    if isinstance(output, dict):
        for key in ("audio", "audio_out", "wav", "url", "output", "file"):
            if key in output:
                found = _find_output_url(output[key])
                if found:
                    return found
        for value in output.values():
            found = _find_output_url(value)
            if found:
                return found
    return None


class ReplicateClient:
    """Create, wait for and download predictions."""

    def __init__(self, base_url: str, api_key: str | None, *, wait_s: int = 30, timeout: float = 120.0,
                 notify: Callable[[str], None] | None = None, cache_dir: Path | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.wait_s = max(1, min(60, int(wait_s)))
        self.timeout = float(timeout)
        self.api = HTTPStreamer(self.base_url, timeout=self.wait_s + 15)
        self.notify = notify or (lambda _m: None)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._files: dict[tuple[str, str, int], HTTPStreamer] = {}
        self._versions: dict[str, str] = {}
        self._models: dict[str, tuple[float, dict[str, Any]]] = {}
        self._fetch_locks: dict[str, threading.Lock] = {}
        self._cold_noticed: set[str] = set()
        self._lock = threading.Lock()

    # headers / hosts

    def _auth(self) -> dict[str, str]:
        if not self.api_key:
            raise TTSError("no Replicate API token (set REPLICATE_API_TOKEN)", code="auth")
        return {"Authorization": f"Bearer {self.api_key}"}

    def _is_api_host(self, url: str) -> bool:
        p = urllib.parse.urlparse(url)
        port = p.port or (443 if p.scheme == "https" else 80)
        return (p.scheme == ("https" if self.api.https else "http") and p.hostname == self.api.host
                and port == self.api.port)

    def _file_streamer(self, url: str) -> HTTPStreamer:
        p = urllib.parse.urlparse(url)
        key = (p.scheme, p.hostname or "", p.port or (443 if p.scheme == "https" else 80))
        with self._lock:
            s = self._files.get(key)
            if s is None:
                s = self._files[key] = HTTPStreamer(f"{p.scheme}://{p.netloc}", timeout=60)
            return s

    def close(self) -> None:
        self.api.close()
        with self._lock:
            files, self._files = list(self._files.values()), {}
        for s in files:
            s.close()

    # models / schemas

    def _schema_path(self, model: str) -> Path | None:
        return self.cache_dir / "replicate" / (model.replace("/", "__") + ".json") if self.cache_dir else None

    def model_json(self, model: str, *, max_age: float = SCHEMA_MAX_AGE, refresh: bool = False) -> dict[str, Any]:
        with self._lock:
            fetch_lock = self._fetch_locks.setdefault(model, threading.Lock())
        with fetch_lock:            # concurrent callers (schema + version) share one fetch
            return self._model_json(model, max_age=max_age, refresh=refresh)

    def _model_json(self, model: str, *, max_age: float, refresh: bool) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            hit = self._models.get(model)
        if hit and not refresh and now - hit[0] < max_age:
            return hit[1]
        path = self._schema_path(model)
        if path and not refresh and not hit:
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if now - float(saved.get("fetched_at", 0)) < max_age and isinstance(saved.get("model"), dict):
                    with self._lock:
                        self._models[model] = (float(saved["fetched_at"]), saved["model"])
                    return saved["model"]
            except (OSError, ValueError):
                pass
        owner, name = model.split("/", 1)
        status, _, body = self.api.request("GET", f"/v1/models/{owner}/{name}", None, self._auth())
        if status != 200:
            raise _replicate_error(status, body)
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise TTSError("Replicate returned a non-JSON model description", code="network") from exc
        with self._lock:
            self._models[model] = (now, data)
        if path:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_text(json.dumps({"fetched_at": now, "model": data}), encoding="utf-8")
                replace_file(tmp, path)
            except OSError:
                pass
        return data

    def version_for(self, adapter: Adapter) -> str:
        if adapter.version:
            return adapter.version
        with self._lock:
            cached = self._versions.get(adapter.model)
        if cached:
            return cached
        data = self.model_json(adapter.model)
        vid = (data.get("latest_version") or {}).get("id")
        if not isinstance(vid, str) or not vid:
            raise TTSError(f"{adapter.model} has no published version on Replicate", code="model")
        with self._lock:
            self._versions[adapter.model] = vid
        return vid

    def account(self) -> dict[str, Any]:
        status, _, body = self.api.request("GET", "/v1/account", None, self._auth())
        if status != 200:
            raise _replicate_error(status, body)
        return json.loads(body)

    # predictions

    def _create(self, adapter: Adapter, inp: dict[str, Any]) -> dict[str, Any]:
        headers = {**self._auth(), "Content-Type": "application/json", "Prefer": f"wait={self.wait_s}"}
        if adapter.official:
            owner, name = adapter.model.split("/", 1)
            path, payload = f"/v1/models/{owner}/{name}/predictions", {"input": inp}
        else:
            path, payload = "/v1/predictions", {"version": self.version_for(adapter), "input": inp}
        body = json.dumps(payload).encode("utf-8")
        for attempt in (0, 1):
            status, resp_headers, data = self.api.request("POST", path, body, headers)
            if status in (200, 201, 202):
                try:
                    return json.loads(data)
                except ValueError as exc:
                    raise TTSError("Replicate returned a non-JSON prediction", code="network") from exc
            err = _replicate_error(status, data)
            if err.code == "rate" and attempt == 0:
                try:
                    delay = min(5.0, float(resp_headers.get("retry-after", RETRY_DELAY)))
                except ValueError:
                    delay = RETRY_DELAY
                time.sleep(max(0.1, delay))
                continue
            raise err
        raise TTSError("Replicate prediction failed", code="network")   # pragma: no cover

    def _get(self, pred_id: str) -> dict[str, Any]:
        status, _, data = self.api.request("GET", f"/v1/predictions/{urllib.parse.quote(pred_id)}", None,
                                           self._auth())
        if status != 200:
            raise _replicate_error(status, data)
        return json.loads(data)

    def _cancel(self, pred_id: str) -> None:
        try:
            self.api.request("POST", f"/v1/predictions/{urllib.parse.quote(pred_id)}/cancel", b"", self._auth())
        except TTSError:
            pass

    def predict(self, adapter: Adapter, inp: dict[str, Any], *,
                sent: threading.Event | None = None) -> dict[str, Any]:
        """Run one prediction to completion; returns the final prediction."""
        started = time.monotonic()
        try:
            pred = self._create(adapter, inp)
        finally:
            if sent is not None:
                sent.set()
        deadline = started + self.timeout
        while pred.get("status") not in ("succeeded", "failed", "canceled"):
            pred_id = pred.get("id")
            if not isinstance(pred_id, str):
                raise TTSError("Replicate returned a prediction without an id", code="network")
            if time.monotonic() > deadline:
                self._cancel(pred_id)
                raise TTSError(f"{adapter.label} took longer than {self.timeout:.0f} s on Replicate", code="timeout")
            # >= so that a threshold of 0 means "at once", even with a clock
            # that ticks every 15.6 ms (Windows, Python 3.10).
            if (pred.get("status") == "starting" and time.monotonic() - started >= COLD_NOTICE_AFTER
                    and adapter.model not in self._cold_noticed):
                self._cold_noticed.add(adapter.model)
                self.notify(f"{adapter.label} is starting up on Replicate (cold boot); the first line may take a while")
            time.sleep(POLL_EVERY)
            pred = self._get(pred_id)
        if pred.get("status") != "succeeded":
            err = pred.get("error") or pred.get("status")
            raise TTSError(f"{adapter.label} failed on Replicate: {str(err)[:300]}", code="failed")
        return pred

    def download(self, url: str) -> bytes:
        if url.startswith("data:"):
            try:
                return base64.b64decode(url.split(",", 1)[1])
            except (IndexError, ValueError) as exc:
                raise TTSError("Replicate returned an unreadable data URI", code="format") from exc
        p = urllib.parse.urlparse(url)
        if p.scheme not in ("https", "http"):
            raise TTSError(f"unsupported output URL scheme '{p.scheme}'", code="format")
        # Credentials only ever go to the API host itself (newer file URLs
        # live there); replicate.delivery and anything else get none.
        headers = self._auth() if self._is_api_host(url) else {}
        path = p.path + (f"?{p.query}" if p.query else "")
        status, _, data = self._file_streamer(url).request("GET", path, None, headers)
        if status != 200:
            raise TTSError(f"could not download the audio (HTTP {status})", status, "network")
        return data

    def run(self, adapter: Adapter, inp: dict[str, Any], *,
            sent: threading.Event | None = None) -> tuple[bytes, dict[str, Any]]:
        pred = self.predict(adapter, inp, sent=sent)
        url = _find_output_url(pred.get("output"))
        if not url:
            raise TTSError(f"{adapter.label} returned no audio file", code="format")
        return self.download(url), (pred.get("metrics") or {})


# -- the provider ---------------------------------------------------------------

def _account_voice(item: dict[str, Any], category: str) -> Voice | None:
    """A voice from MiniMax's get_voice list, labelled from its name and description."""
    vid = item.get("voice_id")
    if not isinstance(vid, str) or not vid:
        return None
    name = str(item.get("voice_name") or vid)
    desc = item.get("description") or []
    desc = " ".join(str(d) for d in desc) if isinstance(desc, list) else str(desc)
    labels = derive_labels(name)
    for key, value in derive_labels(desc).items():
        labels.setdefault(key, value)
    return Voice(id=vid, name=name, category=category, labels=labels, description=desc[:200])


def _norm_id(voice_id: str) -> str:
    v = voice_id.lower()
    return v[len("voices/"):] if v.startswith("voices/") else v


def gemini_voice(item: dict[str, Any], languages: list[str]) -> Voice | None:
    """A voice from Gemini's GET /v1beta/voices, labelled from its structured
    fields; None if it's in a language we don't speak."""
    vid = item.get("id") or item.get("name")
    if not isinstance(vid, str) or not vid:
        return None
    kind = str(item.get("type") or "").lower()
    custom = (kind not in ("", "prebuilt", "voice_type_unspecified", "library")) or _norm_id(vid).startswith("voice_")
    lang = str(item.get("language_code") or "").lower()
    if lang and languages and not custom and not any(lang.startswith(l) for l in languages):
        return None
    name = str(item.get("display_name") or vid)
    desc = item.get("description") or ""
    desc = " ".join(str(d) for d in desc) if isinstance(desc, list) else str(desc)
    labels: dict[str, str] = {}
    gender = str(item.get("gender") or "").lower()
    if gender in ("male", "female"):
        labels["gender"] = gender
    if item.get("accent"):
        labels["accent"] = str(item["accent"]).lower()
    pitch = str(item.get("pitch") or "").lower()
    words = (["low", "deep"] if "low" in pitch else ["high", "bright"] if "high" in pitch else [])
    for key, value in derive_labels(f"{name} {desc}").items():
        if key == "descriptive":
            words.append(value)
        else:
            labels.setdefault(key, value)
    if words:
        labels["descriptive"] = " ".join(words)
    return Voice(id=vid, name=name, category="custom" if custom else "library", labels=labels,
                 description=desc[:200])


class GenericTTS:
    response_format = "pcm"
    dynamic_voices = True          # voices are auto-cast from a catalog

    def __init__(self, cfg: Config, adapter: Adapter, *, bank: VoiceBank | None = None,
                 notify: Callable[[str], None] | None = None, client: ReplicateClient | None = None,
                 minimax: MiniMaxClient | None = None, gemini: GeminiClient | None = None,
                 inworld: InworldClient | None = None):
        self.adapter = adapter
        self.name = adapter.name
        # A renamed adapter can keep the cache keys (and so the audio) of its old name.
        self.cache_name = adapter.cache_name or adapter.name
        self.model = adapter.model
        self.label = adapter.label
        self.sample_rate = adapter.sample_rate
        self.transport = adapter.transport
        self.usage: Any = None                       # set by the engine
        self._notify = notify or (lambda _m: None)
        cache_dir = cfg.path("cache.dir")
        self.client: ReplicateClient | None = None
        self.minimax: MiniMaxClient | None = None
        self.gemini: GeminiClient | None = None
        self.inworld: InworldClient | None = None
        self.http: HTTPStreamer | None = None
        self._languages: list[str] = []
        self._voices_file: Path | None = None
        self._voices_max_age = SCHEMA_MAX_AGE
        if adapter.transport == "replicate":
            self.api_key = cfg.api_key("replicate")
            self.needs_key = True
            self.key_hint = str(cfg.get("replicate.api_key_env") or "REPLICATE_API_TOKEN")
            self.client = client or ReplicateClient(
                str(cfg.get("replicate.base_url", "https://api.replicate.com")), self.api_key,
                wait_s=int(cfg.get("replicate.wait_seconds", 30)),
                timeout=float(adapter.timeout or cfg.get("replicate.timeout", 120)),
                notify=self._say, cache_dir=cache_dir)
        elif adapter.transport == "minimax":
            self.api_key = cfg.api_key("minimax")
            self.needs_key = True
            self.key_hint = str(cfg.get("minimax.api_key_env") or "MINIMAX_API_KEY")
            self.minimax = minimax or MiniMaxClient(
                adapter.base_url or str(cfg.get("minimax.base_url", "https://api.minimax.io")), self.api_key,
                timeout=float(adapter.timeout or cfg.get("minimax.timeout", 30)),
                group_id=str(cfg.get("minimax.group_id")) if cfg.get("minimax.group_id") else None)
            self._voices_file = cache_dir / "minimax" / "voices.json"
            self._voices_max_age = max(0.0, float(cfg.get("minimax.voices.refresh_hours", 24))) * 3600
        elif adapter.transport == "gemini":
            self.api_key = cfg.api_key("gemini")
            self.needs_key = True
            self.key_hint = str(cfg.get("gemini.api_key_env") or "GEMINI_API_KEY")
            self.gemini = gemini or GeminiClient(
                adapter.base_url or str(cfg.get("gemini.base_url", "https://generativelanguage.googleapis.com")),
                self.api_key, timeout=float(adapter.timeout or cfg.get("gemini.timeout", 30)))
            self._voices_file = cache_dir / "gemini" / "voices.json"
            self._voices_max_age = max(0.0, float(cfg.get("gemini.voices.refresh_hours", 24))) * 3600
            self._languages = [str(x).lower() for x in (cfg.get("gemini.voices.languages", ["en"]) or [])]
        elif adapter.transport == "inworld":
            self.api_key = cfg.api_key("inworld")
            self.needs_key = True
            self.key_hint = str(cfg.get("inworld.api_key_env") or "INWORLD_API_KEY")
            self.inworld = inworld or InworldClient(
                adapter.base_url or str(cfg.get("inworld.base_url", "https://api.inworld.ai")), self.api_key,
                timeout=float(adapter.timeout or cfg.get("inworld.timeout", 30)))
            self._voices_file = cache_dir / "inworld" / "voices.json"
            self._voices_max_age = max(0.0, float(cfg.get("inworld.voices.refresh_hours", 24))) * 3600
            self._languages = [str(x).lower() for x in (cfg.get("inworld.voices.languages", ["en"]) or [])]
        else:
            env_key = os.environ.get(adapter.api_key_env) if adapter.api_key_env else None
            self.api_key = adapter.api_key or env_key
            self.needs_key = False
            self.key_hint = adapter.api_key_env or "an api_key in its adapter"
            self.http = HTTPStreamer(adapter.base_url, float(adapter.timeout or 30))
        self.bank = bank if adapter.uses_bank else None
        self.memory = CastingMemory(cache_dir / "casting" / f"{adapter.name}.json")
        # Voices and limits learned from the service: a Replicate model's schema
        # (voice enum, ranges) or a voice list (MiniMax, Gemini, Inworld).
        self._remote_voices: list[Voice] = []
        self._schema_ranges: dict[str, tuple[float, float]] = {}
        self._dead_voices: set[str] = set()
        self._remote_state = "none"                  # none | loading | loaded | failed
        self._remote_attempt = 0.0
        self._remote_lock = threading.Lock()
        self._last_error = 0.0
        self.catalog = VoiceCatalog.static([], label=f"{adapter.name} voice")
        self._rebuild_catalog()
        self._load_cached_remote()

    # notify is assigned by the engine after construction
    @property
    def notify(self) -> Callable[[str], None]:
        return self._notify

    @notify.setter
    def notify(self, fn: Callable[[str], None]) -> None:
        self._notify = fn

    def _say(self, msg: str) -> None:
        self._notify(msg)

    # -- catalog ----------------------------------------------------------

    @property
    def _has_remote(self) -> bool:
        return self.client is not None or (self._live is not None and self.adapter.uses_account)

    @property
    def _live(self) -> MiniMaxClient | GeminiClient | InworldClient | None:
        """The client that lists this provider's voices, if any."""
        return self.minimax or self.gemini or self.inworld

    def _rebuild_catalog(self) -> None:
        curated = list(self.adapter.voices)
        remote = list(self._remote_voices)
        if self.adapter.remote_labels:
            # The service's own labels (Gemini, Inworld: gender, pitch, age) beat our fallback set.
            seen = {_norm_id(v.id) for v in remote}
            voices = remote + [v for v in curated if _norm_id(v.id) not in seen]
        else:
            # Curated labels win (MiniMax: the ids and names mislead).
            seen = {_norm_id(v.id) for v in curated}
            voices = curated + [v for v in remote if _norm_id(v.id) not in seen]
        if self.bank is not None:
            voices += self.bank.voices()
        self.catalog.replace([v for v in voices if v.id not in self._dead_voices])

    def _apply_schema(self, model_json: dict[str, Any]) -> None:
        props, _ = input_properties(model_json)
        self._schema_ranges = schema_ranges(props)
        if self.adapter.uses_schema:
            key = self.adapter.voice_key
            enum = props.get(key, {}).get("enum") if key else None
            self._remote_voices = [Voice(id=str(e), name=str(e), labels=derive_labels(str(e))) for e in (enum or [])]
        self._rebuild_catalog()

    def _apply_account(self, data: dict[str, Any]) -> None:
        voices: list[Voice] = []
        for item in data.get("system_voice") or []:
            if isinstance(item, dict) and str(item.get("voice_id", "")).startswith("English_"):
                v = _account_voice(item, "system")
                if v:
                    voices.append(v)
        for kind, category in (("voice_cloning", "cloned"), ("voice_generation", "designed")):
            for item in data.get(kind) or []:
                v = _account_voice(item, category) if isinstance(item, dict) else None
                if v:
                    voices.append(v)
        self._remote_voices = voices
        self._rebuild_catalog()

    def _apply_labelled(self, items: Any, labeller: Callable[[dict[str, Any], list[str]], Voice | None]) -> None:
        items = items if isinstance(items, list) else []
        self._remote_voices = [v for v in (labeller(i, self._languages) for i in items if isinstance(i, dict)) if v]
        self._rebuild_catalog()

    def _apply_live(self, data: Any) -> None:
        if self.gemini is not None:
            self._apply_labelled(data, gemini_voice)
        elif self.inworld is not None:
            self._apply_labelled(data, inworld_voice)
        else:
            self._apply_account(data if isinstance(data, dict) else {})

    def _load_cached_remote(self) -> None:
        """Use what an earlier run fetched (no network at startup)."""
        try:
            if self.client is not None:
                path = self.client._schema_path(self.adapter.model)
                if path and path.exists():
                    saved = json.loads(path.read_text(encoding="utf-8"))
                    self._apply_schema(saved.get("model") or {})
                    self._remote_state = "loaded"
            elif self._has_remote and self._voices_file and self._voices_file.exists():
                saved = json.loads(self._voices_file.read_text(encoding="utf-8"))
                self._apply_live(saved.get("voices"))
                fresh = time.time() - float(saved.get("fetched_at", 0)) < self._voices_max_age
                self._remote_state = "loaded" if fresh else "none"    # stale: used, and refreshed later
        except (OSError, ValueError, TypeError):
            pass

    def _fetch_remote(self) -> None:
        try:
            if self.client is not None:
                data = self.client.model_json(self.adapter.model)
                with self._remote_lock:
                    self._apply_schema(data)
            else:
                live = self._live
                assert live is not None
                data = live.fetch_voices()
                if self._voices_file is not None:
                    try:
                        self._voices_file.parent.mkdir(parents=True, exist_ok=True)
                        tmp = self._voices_file.with_name(self._voices_file.name + ".tmp")
                        tmp.write_text(json.dumps({"fetched_at": time.time(), "voices": data}), encoding="utf-8")
                        replace_file(tmp, self._voices_file)
                    except OSError:
                        pass
                with self._remote_lock:
                    self._apply_live(data)
            with self._remote_lock:
                self._remote_state = "loaded"
        except Exception as exc:  # noqa: BLE001 - reported, casting still works without it
            with self._remote_lock:
                self._remote_state = "failed"
            if time.monotonic() - self._last_error > 60:
                self._last_error = time.monotonic()
                what = "schema" if self.client is not None else "voice list"
                log.warning("could not read %s's %s: %s", self.name, what, exc)

    def load_remote_voices(self) -> None:
        """Re-read the bank and fetch the schema / account voices now, blocking (CLI)."""
        if self.bank is not None and self.bank.refresh():
            self._rebuild_catalog()
        if self._has_remote and self.api_key:
            with self._remote_lock:
                self._remote_state = "loading"
                self._remote_attempt = time.monotonic()
            self._fetch_remote()

    def ensure_catalog(self, wait: bool = True) -> None:
        """Pick up voice-bank changes; fetch the model schema or the account's
        voices once. wait=False never blocks (network thread)."""
        if self.bank is not None and self.bank.refresh():
            self._rebuild_catalog()
        if not self._has_remote or not self.api_key:
            return
        with self._remote_lock:
            if self._remote_state in ("loaded", "loading"):
                return
            if self._remote_state == "failed" and time.monotonic() - self._remote_attempt < SCHEMA_RETRY:
                return
            # Only a provider with nothing at all to cast from needs to wait.
            must_wait = wait and not self.catalog.voices
            self._remote_state = "loading"
            self._remote_attempt = time.monotonic()
        if must_wait:
            self._fetch_remote()
        else:
            threading.Thread(target=self._fetch_remote, daemon=True, name=f"voices-{self.name}").start()

    def refresh_voices(self) -> None:
        """Re-read the voice bank and the schema / account voices (//koto voices sync)."""
        with self._remote_lock:
            if self._remote_state != "loading":
                self._remote_state = "none"
                if self.client is not None:
                    with self.client._lock:
                        self.client._models.pop(self.adapter.model, None)
        self.ensure_catalog(wait=False)

    def _drop_voice(self, voice_id: str) -> None:
        """The service says this voice doesn't exist: stop casting to it."""
        self._dead_voices.add(voice_id)
        self.catalog.drop(voice_id)
        self.memory.forget_voice(voice_id)

    def resolve_voice(self, name_or_id: str) -> str | None:
        self.ensure_catalog()
        voice = self.catalog.find(name_or_id)
        return voice.id if voice else None

    def display(self, voice_id: str | None) -> str:
        if not voice_id:
            return "model default"
        voice = self.catalog.get(voice_id)
        return voice.name if voice else voice_id

    def forget_casting(self, ident: str) -> None:
        self.memory.forget(ident)

    def status_text(self) -> str:
        where = ("Replicate" if self.client is not None else "the MiniMax API" if self.minimax is not None
                 else "the Gemini API" if self.gemini is not None
                 else "the Inworld API" if self.inworld is not None else self.adapter.base_url)
        bank = f" + {len(self.bank.clips)} bank clips" if self.bank is not None else ""
        return f"{self.name} ({self.label} on {where}), {len(self.catalog.voices)} voices{bank}"

    # -- requests ---------------------------------------------------------

    def _values(self, profile: dict[str, Any], ident: str, voice: Voice | None, clip: Any) -> dict[str, Any]:
        c = self.adapter.controls
        speed_table = c.get("speed") if isinstance(c.get("speed"), dict) else PACE_SPEED
        return {
            "text": TEXT_SENTINEL,
            "voice": voice.id if (voice is not None and clip is None) else None,
            "speed": speed_for(profile, speed_table),
            "pitch": control(c.get("pitch"), profile, "pitch"),
            "emotion": emotion_for(profile) or c.get("emotion_default"),
            "exaggeration": mood_control(c.get("exaggeration"), profile),
            "cfg_weight": mood_control(c.get("cfg_weight"), profile),
            "style": style_text(profile),
            "steering": steering_text(profile),
            "voice_description": voice_description(profile),
            "seed": stable_hash(ident) % 2_147_483_646 + 1,       # 0 means "random" to some models
            "reference_audio": clip.data_uri if clip is not None else None,
            "reference_text": clip.reference_text if clip is not None else None,
        }

    def _template(self, clip: Any) -> dict[str, Any]:
        if self.adapter.transport in ("minimax", "gemini"):
            return self.adapter.input                    # minimax: voice_setting...; gemini: style
        if self.adapter.transport == "replicate":
            return overlay(self.adapter.input, self.adapter.input_clone) if clip is not None else self.adapter.input
        base = {"model": self.adapter.model, "input": "{text}", "voice": "{voice}",
                "response_format": self.adapter.response_format, "speed": "{speed}"}
        return {**base, **self.adapter.input}            # the adapter's 'extra' fields

    def voice_request(self, profile: dict[str, Any], ident: str) -> VoiceRequest:
        self.ensure_catalog()
        explicit = profile.get("voice")
        voice = self.catalog.get(explicit) if isinstance(explicit, str) else None
        if voice is None:
            voice = self.catalog.get(cast(self.catalog, profile, ident, self.memory))
        if voice is None and self.adapter.default_voice:
            voice = self.catalog.get(self.adapter.default_voice) or Voice(id=self.adapter.default_voice,
                                                                         name=self.adapter.default_voice)
        clip = self.bank.get(voice.id) if (self.bank is not None and voice is not None) else None
        values = self._values(profile, ident, voice, clip)
        ranges = {**self._schema_ranges, **self.adapter.ranges}
        template = self._template(clip)
        voice_id = voice.id if voice is not None else ""
        payload = self._fixed(render(template, values, ranges), voice_id)
        tag_values = dict(values, reference_audio=clip.tag if clip is not None else None)
        cache_tag = json.dumps(self._fixed(render(template, tag_values, ranges), voice_id), sort_keys=True,
                               separators=(",", ":"))
        return VoiceRequest(voice=voice_id, options={"payload": payload}, cache_tag=cache_tag,
                            label=self.display(voice_id or None))

    # -- synthesis --------------------------------------------------------

    def _fixed(self, payload: dict[str, Any], voice_id: str = "") -> dict[str, Any]:
        """Streaming transports: the fields that keep the request streaming
        raw PCM. They override the adapter's template so a config edit can't
        break them."""
        if self.inworld is not None:
            # OpenAI's request shape; raw 24 kHz PCM is the only format that streams headerless.
            fixed = {k: v for k, v in payload.items() if k != "stream_format"}
            if not (isinstance(fixed.get("instructions"), str) and fixed["instructions"].strip()):
                fixed.pop("instructions", None)
            fixed.update(model=self.adapter.model, input=TEXT_SENTINEL, response_format="pcm")
            if voice_id:
                fixed["voice"] = voice_id
            return fixed
        if self.gemini is not None:
            content: dict[str, Any] = {"type": "text", "text": TEXT_SENTINEL}
            style = payload.get("style")
            if isinstance(style, str) and style.strip():
                content["annotations"] = [{"type": "speech_metadata", "style": style.strip()}]
            gen = dict(payload["generation_config"]) if isinstance(payload.get("generation_config"), dict) else {}
            gen.pop("speech_config", None)
            if voice_id:
                gen["speech_config"] = [{"voice": voice_id}]
            return {"model": self.adapter.model, "stream": True,
                    "input": [{"type": "user_input", "content": [content]}],
                    "response_format": {"type": "audio", "mime_type": "audio/l16", "sample_rate": self.sample_rate},
                    "generation_config": gen}
        if self.minimax is None:
            return payload
        return {**payload, "model": self.adapter.model, "text": TEXT_SENTINEL, "stream": True,
                "stream_options": {"exclude_aggregated_audio": True},
                "audio_setting": {"sample_rate": self.sample_rate, "format": "pcm", "channel": 1}}

    def _record_minimax(self, text: str, info: dict[str, Any]) -> None:
        if self.usage is None:
            return
        billed = info.get("usage_characters")
        billed = int(billed) if isinstance(billed, (int, float)) else len(text)
        counts: dict[str, float] = {f"p:{self.name}:billed_chars": billed}
        if self.adapter.price.get("per_1k_chars"):
            counts[f"p:{self.name}:cost"] = self.adapter.price["per_1k_chars"] * billed / 1000
        try:
            self.usage.add(**counts)
        except Exception:  # noqa: BLE001 - usage is best effort
            pass

    def _record_chars(self, text: str) -> None:
        """Estimated cost for services billed per character of input
        (Inworld, OpenAI-compatible servers given a price). The engine
        already counts the characters."""
        per_1k = self.adapter.price.get("per_1k_chars")
        if self.usage is None or not per_1k:
            return
        try:
            self.usage.add(**{f"p:{self.name}:cost": per_1k * len(text) / 1000})
        except Exception:  # noqa: BLE001 - usage is best effort
            pass

    def _record_gemini(self, text: str, usage: dict[str, Any]) -> None:
        if self.usage is None:
            return
        out_t = usage.get("total_output_tokens")
        in_t = usage.get("total_input_tokens")
        counts: dict[str, float] = {}
        if isinstance(out_t, (int, float)):
            counts[f"p:{self.name}:output_tokens"] = out_t
        if isinstance(in_t, (int, float)):
            counts[f"p:{self.name}:input_tokens"] = in_t
        price = self.adapter.price
        cost = (price.get("per_1m_output_tokens", 0) * (out_t or 0) + price.get("per_1m_input_tokens", 0) * (in_t or 0)) / 1e6
        if cost:
            counts[f"p:{self.name}:cost"] = cost
        if counts:
            try:
                self.usage.add(**counts)
            except Exception:  # noqa: BLE001 - usage is best effort
                pass

    def _record(self, text: str, metrics: dict[str, Any]) -> None:
        if self.usage is None:
            return
        predict = metrics.get("predict_time")
        predict = float(predict) if isinstance(predict, (int, float)) else 0.0
        counts: dict[str, float] = {f"p:{self.name}:predictions": 1, f"p:{self.name}:predict_s": predict}
        price = self.adapter.price
        cost = 0.0
        if price.get("per_1k_chars"):
            cost += price["per_1k_chars"] * len(text) / 1000
        if price.get("per_second"):
            cost += price["per_second"] * predict
        if cost:
            counts[f"p:{self.name}:cost"] = cost
        try:
            self.usage.add(**counts)
        except Exception:  # noqa: BLE001 - usage is best effort
            pass

    def _synth(self, text: str, payload: dict[str, Any], sent: threading.Event | None = None) -> bytes:
        assert self.client is not None
        data, metrics = self.client.run(self.adapter, fill_text(payload, text), sent=sent)
        try:
            pcm = decode_all(data, self.sample_rate)
        except DecodeError as exc:
            raise TTSError(f"{self.label}: {exc}", code="format") from exc
        self._record(text, metrics)
        return pcm

    def stream(self, text: str, request: VoiceRequest) -> Iterator[bytes]:
        payload = request.options.get("payload") or {}
        if self.minimax is not None:
            yield from self._stream_minimax(text, payload, request.voice)
            return
        if self.gemini is not None:
            yield from self._stream_gemini(text, payload, request.voice)
            return
        if self.client is None:                      # OpenAI-compatible server or Inworld
            yield from self._stream_compatible(text, payload, request.voice)
            return
        if not self.api_key:
            raise TTSError(f"no Replicate API token (set {self.key_hint})", code="auth")
        chunks = split_text(text, self.adapter.max_chars, self.adapter.split)
        if len(chunks) <= 1:
            yield from _slices(self._synth(chunks[0] if chunks else text, payload))
            return
        pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix=f"{self.name}-chunk")
        pending: deque[Future] = deque()
        nxt = 0

        def submit(sent: threading.Event | None = None) -> None:
            nonlocal nxt
            pending.append(pool.submit(self._synth, chunks[nxt], payload, sent))
            nxt += 1

        try:
            first_sent = threading.Event()
            submit(first_sent)
            first_sent.wait(10)               # the first sentence goes out before anything else
            if nxt < len(chunks):
                submit()
            while pending:
                pcm = pending.popleft().result()
                if nxt < len(chunks):
                    submit()
                yield from _slices(pcm)
        finally:
            for f in pending:
                f.cancel()
            pool.shutdown(wait=False, cancel_futures=True)

    def _stream_live(self, make_chunks: Callable[[], Iterator[tuple[bytes, int, str]]],
                     voice_id: str) -> Iterator[bytes]:
        """Play a streaming transport's (audio, sample rate, kind) chunks as
        PCM at this provider's rate. One retry on a rate limit, but only
        before any audio: a retry after audio has played would repeat the
        start of the line. A voice the service doesn't know is dropped, so
        the next line re-casts."""
        retried = False
        while True:
            yielded = False
            dec: PCMDecoder | None = None
            try:
                for audio, rate, kind in make_chunks():
                    if dec is None:
                        dec = PCMDecoder(self.sample_rate, kind=kind, declared_rate=rate or self.sample_rate)
                    out = dec.feed(audio)
                    if out:
                        yielded = True
                        yield out
                if dec is not None:
                    tail = dec.finish()
                    if tail:
                        yield tail
                return
            except DecodeError as exc:
                raise TTSError(f"{self.label}: {exc}", code="format") from exc
            except TTSError as exc:
                if exc.code == "rate" and not yielded and not retried:
                    retried = True
                    time.sleep(LIVE_RETRY_DELAY)
                    continue
                if exc.code == "voice" and voice_id:
                    self._drop_voice(voice_id)
                raise

    def _stream_minimax(self, text: str, payload: dict[str, Any], voice_id: str) -> Iterator[bytes]:
        if not self.api_key:
            raise TTSError(f"no MiniMax API key (set {self.key_hint})", code="auth")
        assert self.minimax is not None
        client, rate = self.minimax, self.sample_rate
        # The API takes up to 10,000 characters per request; it streams, so
        # there is nothing to gain from splitting shorter lines.
        parts = [text] if len(text) <= MINIMAX_MAX_CHARS else split_text(text, MINIMAX_MAX_CHARS, "none")
        for part in parts:
            body = fill_text(payload, part)

            def chunks(body: dict[str, Any] = body, part: str = part) -> Iterator[tuple[bytes, int, str]]:
                for audio in client.stream(body, on_info=lambda info: self._record_minimax(part, info)):
                    yield audio, rate, "pcm"

            yield from self._stream_live(chunks, voice_id)

    def _stream_gemini(self, text: str, payload: dict[str, Any], voice_id: str) -> Iterator[bytes]:
        if not self.api_key:
            raise TTSError(f"no Gemini API key (set {self.key_hint})", code="auth")
        assert self.gemini is not None
        client, rate = self.gemini, self.sample_rate
        parts = [text] if len(text) <= GEMINI_MAX_CHARS else split_text(text, GEMINI_MAX_CHARS, "none")
        for part in parts:
            body = fill_text(payload, part)

            def chunks(body: dict[str, Any] = body, part: str = part) -> Iterator[tuple[bytes, int, str]]:
                yield from client.stream(body, rate, on_usage=lambda usage: self._record_gemini(part, usage))

            yield from self._stream_live(chunks, voice_id)

    def _compat_error(self, status: int, data: bytes) -> TTSError:
        code, message = error_detail(data)
        low = f"{code} {message}".lower()
        if status in (401, 403):
            kind: str | None = "auth"
        elif status == 402 or "insufficient_quota" in low:
            kind = "quota"
        elif status == 429:
            kind = "rate"
        elif status == 404 and "voice" in low:
            kind = "voice"
        else:
            kind = None
        return TTSError(f"{self.name} HTTP {status}: {message or code}", status, kind)

    def _stream_compatible(self, text: str, payload: dict[str, Any], voice_id: str = "") -> Iterator[bytes]:
        """OpenAI's /audio/speech, streamed: a self-hosted server, or Inworld."""
        if self.inworld is not None:
            if not self.api_key:
                raise TTSError(f"no Inworld API key (set {self.key_hint})", code="auth")
            http, path = self.inworld.http, INWORLD_SPEECH_PATH
            headers, on_error = self.inworld.headers(), inworld_error
            text, kind = text.translate(_NO_BRACKETS), "pcm"
        else:
            assert self.http is not None
            http, path, on_error = self.http, "/audio/speech", self._compat_error
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            kind = "pcm" if self.adapter.response_format == "pcm" else "auto"
        rate, limit = self.sample_rate, self.adapter.max_chars
        parts = [text] if len(text) <= limit else split_text(text, limit, "none")
        for part in parts:
            body = json.dumps(fill_text(payload, part)).encode("utf-8")

            def chunks(body: bytes = body, part: str = part) -> Iterator[tuple[bytes, int, str]]:
                for chunk in http.stream("POST", path, body, headers, on_error):
                    yield chunk, rate, kind
                self._record_chars(part)

            yield from self._stream_live(chunks, voice_id)

    # -- connections ------------------------------------------------------

    def warm(self) -> None:
        if self.inworld is not None:
            self.inworld.warm()
        elif self.gemini is not None:
            self.gemini.warm()
        elif self.minimax is not None:
            self.minimax.warm()
        elif self.client is not None:
            if self.api_key:
                self.client.api.warm()
        elif self.http is not None:
            self.http.warm()

    def close(self) -> None:
        if self.inworld is not None:
            self.inworld.close()
        if self.gemini is not None:
            self.gemini.close()
        if self.minimax is not None:
            self.minimax.close()
        if self.client is not None:
            self.client.close()
        if self.http is not None:
            self.http.close()
