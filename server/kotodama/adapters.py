"""Declarative adapters: how to drive one TTS model from a Kotodama profile.

Every Replicate model (and every OpenAI-compatible server) wants its own
input fields. An adapter is a small JSON object naming the model and an
input template whose "{placeholders}" are filled per line from the NPC's
profile:

    "input": {"text": "{text}", "voice_id": "{voice}", "speed": "{speed}",
              "pitch": "{pitch}", "audio_format": "wav"}

Shipped presets live in server/tts_models.json. The user's config.json
"models" section adds new adapters or overrides preset fields by name.
Each adapter becomes a provider name (//koto provider minimax).

Placeholders:
  text, voice, speed (from pace), pitch (profile pitch via the adapter's
  table), emotion (tone words), exaggeration / cfg_weight (temperament),
  style (short acting direction), steering (the same, as plain lower-case
  words for Inworld), voice_description, seed (stable per
  NPC), reference_audio / reference_text (a voice-bank clip).

A value that is exactly "{name}" keeps its type. An unresolved placeholder
drops the field, so the model's default applies. Numbers are clamped to the
adapter's (or the model schema's) range.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .casting import Voice, derive_labels
from .config import SERVER_DIR, Config

log = logging.getLogger("kotodama.adapters")

MODELS_FILE = SERVER_DIR / "tts_models.json"
RESERVED = {"openai", "elevenlabs", "default"}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
TRANSPORTS = ("replicate", "openai_compatible", "minimax", "gemini", "inworld")
PLACEHOLDERS = {"text", "voice", "speed", "pitch", "emotion", "exaggeration", "cfg_weight", "style",
                "steering", "voice_description", "seed", "reference_audio", "reference_text"}
MARKERS = ("bank", "schema", "account")
TEXT_SENTINEL = "\x00TEXT\x00"

_FULL = re.compile(r"^\{([a-z_]+)\}$")
_ANY = re.compile(r"\{([a-z_]+)\}")
_OMIT = object()


class AdapterError(ValueError):
    pass


@dataclass
class Adapter:
    name: str
    transport: str
    model: str
    label: str = ""
    official: bool = True
    version: str | None = None
    sample_rate: int = 24000
    max_chars: int = 1000
    split: str = "sentences"
    input: dict[str, Any] = field(default_factory=dict)
    input_clone: dict[str, Any] | None = None
    controls: dict[str, Any] = field(default_factory=dict)
    ranges: dict[str, tuple[float, float]] = field(default_factory=dict)
    voices: list[Voice] = field(default_factory=list)
    voice_sources: tuple[str, ...] = ()          # "bank" and/or "schema", resolved at run time
    default_voice: str | None = None
    price: dict[str, float] = field(default_factory=dict)
    # openai_compatible (minimax, gemini, inworld: overrides the section's base_url)
    base_url: str = ""
    api_key: str | None = None
    api_key_env: str | None = None
    response_format: str = "pcm"
    timeout: float | None = None
    cache_name: str | None = None        # cache keys under another name (a renamed adapter keeps its audio)
    remote_labels: bool = False          # voices fetched from the service win over the curated set

    @property
    def uses_bank(self) -> bool:
        return "bank" in self.voice_sources

    @property
    def uses_schema(self) -> bool:
        return "schema" in self.voice_sources

    @property
    def uses_account(self) -> bool:
        return "account" in self.voice_sources

    @property
    def voice_key(self) -> str | None:
        """The input field that receives {voice} (for schema enums)."""
        for key, tpl in self.input.items():
            if tpl == "{voice}":
                return key
        return None


# -- loading ----------------------------------------------------------------

def _deep_merge(base: Any, over: Any) -> Any:
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _deep_merge(base.get(k), v) if k in base else copy.deepcopy(v)
        return out
    return copy.deepcopy(over)


def load_models_file(path: Path | None = None) -> dict[str, Any]:
    path = Path(path) if path else MODELS_FILE
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise AdapterError(f"{path} must hold a JSON object")
    return data


def _voice_from(entry: Any) -> Voice | None:
    if isinstance(entry, str) and entry:
        return Voice(id=entry, name=entry, labels=derive_labels(entry))
    if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
        return None
    vid = entry["id"]
    name = str(entry.get("name") or vid)
    labels = {str(k): str(v) for k, v in (entry.get("labels") or {}).items() if v not in (None, "")}
    derived = derive_labels(name if name != vid else vid)
    for k in ("gender", "age", "accent"):
        if k not in labels and k in derived:
            labels[k] = derived[k]
    return Voice(id=vid, name=name, category=str(entry.get("category") or ""), labels=labels,
                 description=str(entry.get("description") or ""))


def _resolve_voices(spec: Any, voice_sets: dict[str, Any], name: str) -> tuple[list[Voice], tuple[str, ...]]:
    items = spec if isinstance(spec, list) else ([spec] if spec else [])
    voices: list[Voice] = []
    markers: list[str] = []
    for item in items:
        if isinstance(item, str) and item.startswith("@"):
            vs = voice_sets.get(item[1:])
            if not isinstance(vs, list):
                raise AdapterError(f"{name}: unknown voice set {item}")
            voices.extend(v for v in (_voice_from(e) for e in vs) if v)
        elif item in MARKERS:
            markers.append(item)
        else:
            v = _voice_from(item)
            if v is None:
                raise AdapterError(f"{name}: bad voice entry {item!r}")
            voices.append(v)
    seen: set[str] = set()
    unique = [v for v in voices if not (v.id in seen or seen.add(v.id))]
    return unique, tuple(dict.fromkeys(markers))


def _placeholders(template: Any) -> set[str]:
    if isinstance(template, str):
        return set(_ANY.findall(template))
    if isinstance(template, dict):
        return set().union(*(_placeholders(v) for v in template.values())) if template else set()
    if isinstance(template, list):
        return set().union(*(_placeholders(v) for v in template)) if template else set()
    return set()


def build_adapter(name: str, raw: dict[str, Any], voice_sets: dict[str, Any]) -> Adapter:
    if not NAME_RE.match(name) or name in RESERVED:
        raise AdapterError(f"bad model name '{name}' (lowercase letters, digits, - and _; not {sorted(RESERVED)})")
    transport = str(raw.get("transport") or "replicate")
    if transport not in TRANSPORTS:
        raise AdapterError(f"{name}: transport must be one of {TRANSPORTS}")
    model = str(raw.get("model") or "")
    if transport == "replicate" and not re.fullmatch(r"[\w.-]+/[\w.-]+", model):
        raise AdapterError(f"{name}: model must be 'owner/name' on Replicate")
    if transport == "openai_compatible":
        if not raw.get("base_url"):
            raise AdapterError(f"{name}: openai_compatible needs base_url")
        if not model:
            raise AdapterError(f"{name}: openai_compatible needs model")
    if transport in ("minimax", "gemini", "inworld") and not model:
        example = {"minimax": "speech-2.8-turbo", "gemini": "gemini-3.8-flash-tts"}.get(transport, "inworld-tts-2")
        raise AdapterError(f"{name}: {transport} needs model (e.g. {example})")
    # replicate: the model's whole input; minimax: request fields besides the
    # text and audio format (voice_setting, language_boost...); openai_compatible
    # and inworld (the same request format): extra request fields.
    template = raw.get("extra", {}) if transport in ("openai_compatible", "inworld") else raw.get("input", {})
    if not isinstance(template, dict) or (transport == "replicate" and not template):
        raise AdapterError(f"{name}: 'input' must be an object of model fields")
    clone = raw.get("input_clone")
    if clone is not None and not isinstance(clone, dict):
        raise AdapterError(f"{name}: 'input_clone' must be an object or null")
    unknown = (_placeholders(template) | _placeholders(clone or {})) - PLACEHOLDERS
    if unknown:
        raise AdapterError(f"{name}: unknown placeholder(s) {', '.join('{' + u + '}' for u in sorted(unknown))}")
    if transport == "replicate" and "text" not in _placeholders(template):
        raise AdapterError(f"{name}: no input field uses {{text}}")
    ranges: dict[str, tuple[float, float]] = {}
    for key, rng in (raw.get("ranges") or {}).items():
        if (isinstance(rng, list) and len(rng) == 2 and all(isinstance(x, (int, float)) for x in rng)):
            ranges[key] = (rng[0], rng[1])
        else:
            raise AdapterError(f"{name}: range for {key} must be [min, max]")
    voices, markers = _resolve_voices(raw.get("voices"), voice_sets, name)
    return Adapter(
        name=name, transport=transport, model=model,
        label=str(raw.get("label") or model),
        official=bool(raw.get("official", True)),
        version=str(raw["version"]) if raw.get("version") else None,
        sample_rate=int(raw.get("sample_rate", 24000)),
        max_chars=max(50, int(raw.get("max_chars", 1000))),
        split=str(raw.get("split", "sentences" if transport == "replicate" else "none")),
        input=dict(template), input_clone=dict(clone) if clone else None,
        controls=dict(raw.get("controls") or {}), ranges=ranges,
        voices=voices, voice_sources=markers,
        default_voice=str(raw["default_voice"]) if raw.get("default_voice") else None,
        price={k: float(v) for k, v in (raw.get("price") or {}).items() if isinstance(v, (int, float))},
        base_url=str(raw.get("base_url") or ""),
        api_key=str(raw["api_key"]) if raw.get("api_key") else None,
        api_key_env=str(raw["api_key_env"]) if raw.get("api_key_env") else None,
        response_format=str(raw.get("response_format") or "pcm"),
        timeout=float(raw["timeout"]) if isinstance(raw.get("timeout"), (int, float)) else None,
        cache_name=str(raw["cache_name"]) if raw.get("cache_name") else None,
        remote_labels=bool(raw.get("remote_labels", False)),
    )


def merged_specs(cfg: Config, data: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Preset specs with the user's config.json "models" merged in by name."""
    data = data if data is not None else load_models_file()
    presets: dict[str, Any] = data.get("models", {})
    specs = {k: copy.deepcopy(v) for k, v in presets.items() if isinstance(v, dict)}
    for name, entry in (cfg.get("models", {}) or {}).items():
        if not isinstance(entry, dict):
            continue
        entry = dict(entry)
        base_name = entry.pop("preset", None)
        if name in specs:
            specs[name] = _deep_merge(specs[name], entry)
        elif isinstance(base_name, str) and base_name in presets:
            specs[name] = _deep_merge(presets[base_name], entry)
        else:
            specs[name] = entry
    return {k: v for k, v in specs.items() if v.get("enabled", True) is not False}


def load_adapters(cfg: Config, problems: list[str] | None = None,
                  data: dict[str, Any] | None = None) -> dict[str, Adapter]:
    data = data if data is not None else load_models_file()
    voice_sets = data.get("voice_sets", {})
    out: dict[str, Adapter] = {}
    for name, spec in merged_specs(cfg, data).items():
        try:
            out[name] = build_adapter(name, spec, voice_sets)
        except (AdapterError, TypeError, ValueError) as exc:
            msg = str(exc) if str(exc).startswith(name) else f"{name}: {exc}"
            log.warning("model adapter skipped: %s", msg)
            if problems is not None:
                problems.append(msg)
    return out


# -- rendering ----------------------------------------------------------------

def _render_value(tpl: Any, values: dict[str, Any], ranges: dict[str, tuple[float, float]] | None = None,
                  path: str = "") -> Any:
    if isinstance(tpl, str):
        m = _FULL.match(tpl)
        if m:
            v = values.get(m.group(1))
            return _OMIT if v is None else v
        missing = False

        def sub(mo: re.Match) -> str:
            nonlocal missing
            v = values.get(mo.group(1))
            if v is None:
                missing = True
                return ""
            return str(v)

        out = _ANY.sub(sub, tpl)
        return _OMIT if missing else out
    if isinstance(tpl, dict):
        return render(tpl, values, ranges, path + ".")
    if isinstance(tpl, list):
        return [v for v in (_render_value(x, values) for x in tpl) if v is not _OMIT]
    if tpl is None:
        return _OMIT
    return copy.deepcopy(tpl)


def _clamp(value: Any, rng: tuple[float, float]) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    lo, hi = rng
    clamped = max(lo, min(hi, value))
    return int(round(clamped)) if isinstance(value, int) else float(clamped)


def render(template: dict[str, Any], values: dict[str, Any],
           ranges: dict[str, tuple[float, float]] | None = None, prefix: str = "") -> dict[str, Any]:
    """Fill a template. Range keys are field names, dotted for nested fields
    ("voice_setting.speed")."""
    out: dict[str, Any] = {}
    for key, tpl in template.items():
        v = _render_value(tpl, values, ranges, prefix + key)
        if v is _OMIT:
            continue
        if ranges and prefix + key in ranges:
            v = _clamp(v, ranges[prefix + key])
        out[key] = v
    return out


def overlay(base: dict[str, Any], over: dict[str, Any] | None) -> dict[str, Any]:
    """input + input_clone: null in the overlay removes a field."""
    if not over:
        return dict(base)
    out = dict(base)
    for k, v in over.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = v
    return out


def fill_text(payload: Any, text: str) -> Any:
    """Put a chunk's text where the {text} sentinel was rendered."""
    if isinstance(payload, str):
        return payload.replace(TEXT_SENTINEL, text) if TEXT_SENTINEL in payload else payload
    if isinstance(payload, dict):
        return {k: fill_text(v, text) for k, v in payload.items()}
    if isinstance(payload, list):
        return [fill_text(v, text) for v in payload]
    return payload


# -- Replicate model schemas ----------------------------------------------------

def _resolve_ref(node: Any, components: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    if not isinstance(node, dict) or depth > 5:
        return node if isinstance(node, dict) else {}
    out = {k: v for k, v in node.items() if k not in ("$ref", "allOf")}
    refs = []
    if isinstance(node.get("$ref"), str):
        refs.append(node["$ref"])
    for part in node.get("allOf") or []:
        if isinstance(part, dict) and isinstance(part.get("$ref"), str):
            refs.append(part["$ref"])
        elif isinstance(part, dict):
            out.update(part)
    for ref in refs:
        target = components.get(ref.rsplit("/", 1)[-1], {})
        for k, v in _resolve_ref(target, components, depth + 1).items():
            out.setdefault(k, v)
    return out


def input_properties(model_json: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """(properties, required) of a Replicate model's input, $refs resolved.
    Accepts a models.get response or a version object."""
    version = model_json.get("latest_version") if "latest_version" in model_json else model_json
    schema = ((version or {}).get("openapi_schema") or {})
    components = (schema.get("components") or {}).get("schemas") or {}
    inp = components.get("Input") or {}
    props = {k: _resolve_ref(v, components) for k, v in (inp.get("properties") or {}).items()}
    return props, list(inp.get("required") or [])


def schema_ranges(props: dict[str, dict[str, Any]]) -> dict[str, tuple[float, float]]:
    out = {}
    for key, p in props.items():
        lo, hi = p.get("minimum"), p.get("maximum")
        if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
            out[key] = (lo, hi)
    return out


def check_fields(adapter: Adapter, props: dict[str, dict[str, Any]]) -> list[str]:
    """Problems between an adapter and the model's live schema."""
    if not props:
        return ["the model publishes no input schema"]
    problems = []
    for tpl in (adapter.input, adapter.input_clone or {}):
        for key, value in tpl.items():
            if key not in props:
                problems.append(f"input '{key}' is not a field of {adapter.model} any more")
                continue
            enum = props[key].get("enum")
            if enum and isinstance(value, str) and not _ANY.search(value) and value not in enum:
                problems.append(f"'{key}': '{value}' is not one of {enum[:12]}")
    vk = adapter.voice_key
    enum = props.get(vk, {}).get("enum") if vk else None
    if enum:
        missing = [v.id for v in adapter.voices if v.id not in enum]
        if missing:
            problems.append(f"{len(missing)} listed voice(s) not offered by the model: {', '.join(missing[:6])}")
    return problems


_TEXT_KEYS = ("text", "prompt", "input", "input_text", "tts_text", "speech", "sentence")
_VOICE_KEYS = ("voice", "voice_id", "speaker", "speaker_id", "voice_name")
_REF_AUDIO_HINTS = ("reference", "audio_prompt", "speaker_wav", "ref_audio", "prompt_audio", "voice_sample")
_REF_TEXT_KEYS = ("reference_text", "ref_text", "prompt_text", "reference_transcript")
_STYLE_KEYS = ("style_instruction", "instructions", "instruction", "style_prompt", "description")
_FORMAT_KEYS = ("audio_format", "output_format", "format", "response_format")


def draft_adapter(model_ref: str, props: dict[str, dict[str, Any]], required: list[str],
                  official: bool) -> dict[str, Any]:
    """A starting adapter for an unknown Replicate model, from its schema."""
    def is_str(k: str) -> bool:
        return props.get(k, {}).get("type") == "string"

    def is_num(k: str) -> bool:
        return props.get(k, {}).get("type") in ("number", "integer")

    inp: dict[str, Any] = {}
    clone: dict[str, Any] = {}
    text_key = next((k for k in _TEXT_KEYS if k in props and is_str(k)), None) or \
        next((k for k in required if is_str(k) and props[k].get("format") != "uri"), None)
    if text_key:
        inp[text_key] = "{text}"
    voice_key = next((k for k in _VOICE_KEYS if k in props), None)
    if voice_key:
        inp[voice_key] = "{voice}"
    for key, p in props.items():
        if key in inp:
            continue
        if key in ("speed", "rate", "speaking_rate", "length_scale") and is_num(key):
            inp[key] = "{speed}"
        elif key == "seed" and is_num(key):
            inp[key] = "{seed}"
        elif key in ("exaggeration", "cfg_weight", "emotion") or (key == "pitch" and is_num(key)):
            inp[key] = "{" + key + "}"
        elif key in _STYLE_KEYS and is_str(key):
            inp[key] = "{style}"
        elif key in _FORMAT_KEYS and "wav" in (p.get("enum") or []):
            inp[key] = "wav"
        elif key == "sample_rate" and 24000 in (p.get("enum") or [24000]):
            inp[key] = 24000
        elif p.get("format") == "uri" and any(h in key for h in _REF_AUDIO_HINTS):
            clone[key] = "{reference_audio}"
        elif key in _REF_TEXT_KEYS and is_str(key):
            clone[key] = "{reference_text}"
    voices: list[str] = []
    if voice_key and props[voice_key].get("enum"):
        voices.append("schema")
    if clone:
        voices.append("bank")
        if voice_key:
            clone[voice_key] = None
    max_len = props.get(text_key or "", {}).get("maxLength")
    draft: dict[str, Any] = {
        "transport": "replicate", "model": model_ref, "official": official, "label": model_ref,
        "sample_rate": 24000, "split": "sentences",
        "max_chars": int(max_len) if isinstance(max_len, int) else 1000,
        "input": inp,
    }
    if clone:
        draft["input_clone"] = clone
    if voices:
        draft["voices"] = voices
    if "pitch" in inp:
        draft["controls"] = {"pitch": {"very low": -3, "low": -1, "medium": 0, "high": 1, "very high": 3}}
    return draft
