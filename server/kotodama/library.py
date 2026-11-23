"""The shareable NPC voice library, community packs and personal overrides.

library/npcs.default.json  profiles shipped with Kotodama, read-only. A new
                    release replaces this file, so nothing of yours goes in it.
library/npcs.json   what this install learned (LLM) and curated; stable,
                    diff-friendly JSON meant to be shared and merged between
                    players. Not shipped, so an upgrade never overwrites it.
library/packs/*.json  imported libraries from other people, read-only.
config/overrides.json personal per-NPC settings; always win.

All four use the same envelope:  {"format": 1, "npcs": {<key>: ...}}
Keys are NPC names, optionally zone-specific as "Name@<zone id>"; the special
keys "@player" and "@narrator" apply to those roles.

Player names never enter these files: sample lines are scrubbed to {player}.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import threading
from pathlib import Path
from typing import Any

from .archetypes import Archetypes
from .fsutil import replace_file

log = logging.getLogger("kotodama.library")

FORMAT = 1
MAX_SAMPLES = 3
MAX_ZONES = 20


def _read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    replace_file(tmp, path)


def _norm_key(key: str) -> str:
    return key.strip().lower()


# Entries the sidecar worked out for itself. A community pack outranks them,
# and //koto voice reinfer may replace them. They are either cast by a model
# ("llm:<model>"), or carried over from the "???" a character spoke as
# ("same-as:<identity>").
LEARNED_SOURCES = ("llm", "same-as")


def is_learned(source: Any) -> bool:
    return str(source or "").startswith(LEARNED_SOURCES)


class Library:
    def __init__(self, archetypes: Archetypes, file: Path, packs_dir: Path, overrides_file: Path,
                 save_delay: float = 2.0, defaults_file: Path | None = None):
        self.arch = archetypes
        self.file = Path(file)
        self.defaults_file = Path(defaults_file) if defaults_file else None
        self.packs_dir = Path(packs_dir)
        self.overrides_file = Path(overrides_file)
        self.save_delay = save_delay
        self._lock = threading.RLock()
        self._timer: threading.Timer | None = None
        self._otimer: threading.Timer | None = None
        self._dirty_local = False
        self._dirty_overrides = False
        # normalized key -> {"key": display key, "profile": {...}, "meta": {...}}
        self.local: dict[str, dict[str, Any]] = {}
        self.defaults: dict[str, dict[str, Any]] = {}
        self.packs: dict[str, dict[str, Any]] = {}
        self.overrides: dict[str, dict[str, Any]] = {}
        self.load()

    # -- loading ----------------------------------------------------------

    def _load_envelope(self, path: Path) -> dict[str, Any]:
        data = _read_json(path)
        if not isinstance(data, dict) or data.get("format") != FORMAT or not isinstance(data.get("npcs"), dict):
            raise ValueError(f"{path.name}: not a Kotodama library (format {FORMAT})")
        return data["npcs"]

    def _entry(self, key: str, raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        profile = self.arch.sanitize(raw.get("profile"))
        meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
        source = meta.get("source")
        meta = dict(meta)
        meta["source"] = source if isinstance(source, str) and source else "manual"
        return {"key": key, "profile": profile, "meta": meta}

    def load(self) -> None:
        with self._lock:
            self.local, self.defaults, self.packs, self.overrides = {}, {}, {}, {}
            for path, into in ((self.defaults_file, self.defaults), (self.file, self.local)):
                if not path or not path.is_file():
                    continue
                try:
                    for key, raw in self._load_envelope(path).items():
                        entry = self._entry(key, raw)
                        if entry:
                            into[_norm_key(key)] = entry
                except (ValueError, OSError) as exc:
                    log.error("could not read %s: %s", path, exc)

            if self.packs_dir.is_dir():
                for pack in sorted(self.packs_dir.glob("*.json")):
                    try:
                        for key, raw in self._load_envelope(pack).items():
                            entry = self._entry(key, raw)
                            if entry:
                                entry["meta"]["pack"] = pack.name
                                self.packs.setdefault(_norm_key(key), entry)
                    except (ValueError, OSError) as exc:
                        log.error("could not read pack %s: %s", pack, exc)

            if self.overrides_file.is_file():
                try:
                    for key, raw in self._load_envelope(self.overrides_file).items():
                        self.overrides[_norm_key(key)] = {"key": key, "profile": self.arch.sanitize(raw)}
                except (ValueError, OSError) as exc:
                    log.error("could not read %s: %s", self.overrides_file, exc)

    # -- lookup -----------------------------------------------------------

    @staticmethod
    def keys_for(name: str | None, zone: int | None) -> list[str]:
        """Normalized lookup keys, most specific first."""
        if not name:
            return []
        keys = []
        if zone is not None:
            keys.append(_norm_key(f"{name}@{zone}"))
        keys.append(_norm_key(name))
        return keys

    def lookup(self, key: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Library profile for one normalized key, or None.

        Precedence: locally curated/manual > community pack > locally learned
        (LLM) entry > shipped default. Anything of this install's own, or
        imported on purpose, beats what ships with Kotodama."""
        with self._lock:
            local = self.local.get(key)
            pack = self.packs.get(key)
            if local and not is_learned(local["meta"].get("source")):
                return local["profile"], local["meta"]
            if pack:
                return pack["profile"], pack["meta"]
            if local:
                return local["profile"], local["meta"]
            shipped = self.defaults.get(key)
            if shipped:
                return shipped["profile"], shipped["meta"]
        return None

    def has_any(self, name: str) -> bool:
        k = _norm_key(name)
        with self._lock:
            return k in self.local or k in self.packs or k in self.defaults

    def known_entry(self, key: str) -> dict[str, Any] | None:
        """This install's entry for a normalized key, else the shipped one."""
        with self._lock:
            return self.local.get(key) or self.defaults.get(key)

    def override(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self.overrides.get(key)
            return dict(entry["profile"]) if entry else None

    # -- learned entries --------------------------------------------------

    def put(self, name: str, profile: dict[str, Any], *, source: str,
            game_race: str | None = None, model_id: int | None = None,
            zone: int | None = None, sample: str | None = None) -> None:
        profile = self.arch.sanitize(profile)
        k = _norm_key(name)
        with self._lock:
            old = self.local.get(k)
            meta = dict(old["meta"]) if old else {}
            meta["source"] = source
            meta.setdefault("created", _dt.date.today().isoformat())
            if game_race:
                meta["game_race"] = game_race
            if model_id is not None:
                meta["model_id"] = model_id
            if zone is not None:
                zones = set(meta.get("zones_seen", []))
                zones.add(zone)
                meta["zones_seen"] = sorted(zones)[:MAX_ZONES]
            if sample:
                samples = list(meta.get("sample_lines", []))
                if sample not in samples and len(samples) < MAX_SAMPLES:
                    samples.append(sample)
                meta["sample_lines"] = samples
            self.local[k] = {"key": name, "profile": profile, "meta": meta}
            self._dirty_local = True
        self._schedule_save()

    def note_seen(self, name: str, zone: int | None, sample: str | None) -> None:
        """Record zone/sample for an existing local entry (cheap bookkeeping)."""
        k = _norm_key(name)
        with self._lock:
            entry = self.local.get(k)
            if not entry:
                return
            changed = False
            meta = entry["meta"]
            if zone is not None and zone not in meta.get("zones_seen", []):
                meta["zones_seen"] = sorted(set(meta.get("zones_seen", [])) | {zone})[:MAX_ZONES]
                changed = True
            samples = meta.get("sample_lines", [])
            if sample and sample not in samples and len(samples) < MAX_SAMPLES:
                meta["sample_lines"] = samples + [sample]
                changed = True
            if changed:
                self._dirty_local = True
        if changed:
            self._schedule_save()

    def delete_local(self, key: str) -> bool:
        with self._lock:
            removed = self.local.pop(key, None) is not None
            if removed:
                self._dirty_local = True
        if removed:
            self._schedule_save()
        return removed

    # -- overrides --------------------------------------------------------

    def set_override(self, key: str, profile: dict[str, Any]) -> dict[str, Any]:
        k = _norm_key(key)
        with self._lock:
            merged = dict(self.overrides.get(k, {}).get("profile", {}))
            for field, value in profile.items():
                if field == "voice" and isinstance(value, dict):
                    merged["voice"] = {**merged.get("voice", {}), **value}
                else:
                    merged[field] = value
            self.overrides[k] = {"key": key, "profile": merged}
            self._dirty_overrides = True
        self._schedule_save(overrides=True)
        return merged

    def reset_override(self, key: str) -> bool:
        with self._lock:
            removed = self.overrides.pop(_norm_key(key), None) is not None
            if removed:
                self._dirty_overrides = True
        if removed:
            self._schedule_save(overrides=True)
        return removed

    # -- persistence ------------------------------------------------------

    def _schedule_save(self, overrides: bool = False) -> None:
        attr = "_otimer" if overrides else "_timer"
        with self._lock:
            timer = getattr(self, attr)
            if timer:
                timer.cancel()
            timer = threading.Timer(self.save_delay, self.flush, kwargs={"overrides": overrides})
            timer.daemon = True
            setattr(self, attr, timer)
            timer.start()

    def flush(self, overrides: bool | None = None, force: bool = False) -> None:
        """Write pending changes now. overrides=None considers both files,
        False only the library, True only the overrides. Unchanged files are
        left alone unless force=True."""
        with self._lock:
            if overrides in (None, False) and (self._dirty_local or force):
                npcs = {e["key"]: {"profile": e["profile"], "meta": e["meta"]} for e in self.local.values()}
                _atomic_write(self.file, {"format": FORMAT, "npcs": npcs})
                self._dirty_local = False
            if overrides in (None, True) and (self._dirty_overrides or force):
                npcs = {e["key"]: e["profile"] for e in self.overrides.values()}
                _atomic_write(self.overrides_file, {"format": FORMAT, "npcs": npcs})
                self._dirty_overrides = False

    # -- sharing ----------------------------------------------------------

    def export(self, sources: list[str] | None = None, with_defaults: bool = False) -> dict[str, Any]:
        """Envelope of local entries, optionally filtered by meta.source
        prefix (e.g. ["llm", "curated"]). with_defaults also includes the
        shipped entries this install has no entry of its own for; that is how
        a maintainer rebuilds library/npcs.default.json."""
        with self._lock:
            npcs = {}
            entries = dict(self.defaults) if with_defaults else {}
            entries.update(self.local)
            for e in entries.values():
                src = str(e["meta"].get("source", ""))
                if sources and not any(src.startswith(s) for s in sources):
                    continue
                npcs[e["key"]] = {"profile": e["profile"], "meta": e["meta"]}
        return {"format": FORMAT, "npcs": npcs}

    def import_pack(self, source: Path) -> tuple[Path, int]:
        """Validate a shared library file and install it as a read-only pack."""
        source = Path(source)
        data = _read_json(source)
        if not isinstance(data, dict) or data.get("format") != FORMAT or not isinstance(data.get("npcs"), dict):
            raise ValueError(f"{source.name} is not a Kotodama library (expected format {FORMAT})")
        clean: dict[str, Any] = {}
        for key, raw in data["npcs"].items():
            entry = self._entry(str(key), raw)
            if entry and entry["profile"]:
                meta = {k: v for k, v in entry["meta"].items() if k != "pack"}
                clean[str(key)] = {"profile": entry["profile"], "meta": meta}
        dest = self.packs_dir / source.name
        _atomic_write(dest, {"format": FORMAT, "npcs": clean})
        self.flush()  # load() below would otherwise drop unsaved local changes
        self.load()
        return dest, len(clean)
