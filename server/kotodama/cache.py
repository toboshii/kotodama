"""On-disk audio cache: WAV files plus a small SQLite index (LRU pruning)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import wave
from pathlib import Path

from .fsutil import replace_file

log = logging.getLogger("kotodama.cache")


def make_key(provider: str, model: str, voice: str, instructions: str, text: str, fmt: str) -> str:
    """Everything that changes the audio is in the key, so editing a voice
    profile naturally yields fresh audio while old lines stay cached."""
    blob = json.dumps([provider, model, voice, instructions, text, fmt], ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class CacheWriter:
    """Streams PCM into a .part file; commit() publishes it atomically."""

    def __init__(self, cache: "AudioCache", key: str, meta: dict, rate: int):
        self.cache, self.key, self.meta, self.rate = cache, key, meta, rate
        self.final = cache.path_for(key)
        self.part = self.final.with_suffix(".wav.part")
        self.final.parent.mkdir(parents=True, exist_ok=True)
        self._wav = wave.open(str(self.part), "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(rate)
        self.frames = 0
        self._closed = False

    def write(self, pcm: bytes) -> None:
        self._wav.writeframes(pcm)
        self.frames += len(pcm) // 2

    def commit(self) -> float:
        """Finalize; returns the audio duration in seconds."""
        self._close()
        if self.frames == 0:
            self.abort()
            return 0.0
        replace_file(self.part, self.final)
        seconds = self.frames / self.rate
        self.cache._record(self.key, self.final, self.meta, seconds)
        return seconds

    def abort(self) -> None:
        self._close()
        try:
            self.part.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:   # e.g. Windows, while a scanner holds the file
            log.debug("could not remove %s: %s", self.part, exc)

    def _close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._wav.close()
            except Exception:  # noqa: BLE001 - closing a half-written file
                pass


class AudioCache:
    def __init__(self, directory: Path, max_mb: int = 2048):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = int(max_mb) * 1024 * 1024
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.dir / "index.sqlite"), check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS audio ("
            " key TEXT PRIMARY KEY, path TEXT NOT NULL, bytes INTEGER NOT NULL,"
            " npc TEXT, zone INTEGER, text TEXT, seconds REAL,"
            " created REAL NOT NULL, last_played REAL NOT NULL, plays INTEGER NOT NULL DEFAULT 0)"
        )
        self._db.commit()
        self._inflight: dict[str, threading.Event] = {}

    def path_for(self, key: str) -> Path:
        return self.dir / key[:2] / f"{key}.wav"

    def get(self, key: str) -> Path | None:
        path = self.path_for(key)
        with self._lock:
            if not path.is_file():
                self._db.execute("DELETE FROM audio WHERE key=?", (key,))
                self._db.commit()
                return None
            self._db.execute("UPDATE audio SET last_played=?, plays=plays+1 WHERE key=?",
                             (time.time(), key))
            self._db.commit()
        return path

    def touch_only(self, key: str) -> bool:
        return self.path_for(key).is_file()

    def begin(self, key: str, meta: dict, rate: int) -> CacheWriter:
        return CacheWriter(self, key, meta, rate)

    def _record(self, key: str, path: Path, meta: dict, seconds: float) -> None:
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO audio (key, path, bytes, npc, zone, text, seconds, created, last_played, plays)"
                " VALUES (?,?,?,?,?,?,?,?,?,1)",
                (key, str(path), path.stat().st_size, meta.get("npc"), meta.get("zone"),
                 meta.get("text"), seconds, now, now),
            )
            self._db.commit()
        self.prune()

    # In-flight de-duplication: only one thread synthesizes a given key.
    def claim(self, key: str) -> bool:
        with self._lock:
            if key in self._inflight:
                return False
            self._inflight[key] = threading.Event()
            return True

    def release(self, key: str) -> None:
        with self._lock:
            ev = self._inflight.pop(key, None)
        if ev:
            ev.set()

    def wait_inflight(self, key: str, timeout: float) -> bool:
        with self._lock:
            ev = self._inflight.get(key)
        return ev.wait(timeout) if ev else True

    def stats(self) -> dict:
        with self._lock:
            n, total, secs = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(bytes),0), COALESCE(SUM(seconds),0) FROM audio").fetchone()
        return {"files": n, "bytes": total, "seconds": secs, "max_bytes": self.max_bytes}

    def prune(self, target_fraction: float = 0.9) -> int:
        """Delete least-recently-played files until under the size limit."""
        removed = 0
        with self._lock:
            total = self._db.execute("SELECT COALESCE(SUM(bytes),0) FROM audio").fetchone()[0]
            if total <= self.max_bytes:
                return 0
            goal = self.max_bytes * target_fraction
            rows = self._db.execute("SELECT key, path, bytes FROM audio ORDER BY last_played ASC").fetchall()
            for key, path, size in rows:
                if total <= goal:
                    break
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    # On Windows a line that's playing can't be deleted. Keep
                    # its row so a later prune tries again.
                    log.debug("could not prune %s yet: %s", path, exc)
                    continue
                self._db.execute("DELETE FROM audio WHERE key=?", (key,))
                total -= size
                removed += 1
            self._db.commit()
        if removed:
            log.info("pruned %d cached lines", removed)
        return removed

    def close(self) -> None:
        with self._lock:
            self._db.close()
