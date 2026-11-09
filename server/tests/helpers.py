"""Shared test fixtures: fake TTS, temp-dir config, engine factory."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from kotodama.audio import Ducker
from kotodama.config import Config
from kotodama.engine import build_engine


class FakeTTS:
    name = "openai"
    model = "fake-tts"
    response_format = "pcm"
    sample_rate = 24000
    api_key = "test-key"

    def __init__(self, chunks: int = 4, chunk_bytes: int = 4800, delay: float = 0.0, fail: str | None = None):
        self.chunks, self.chunk_bytes, self.delay, self.fail = chunks, chunk_bytes, delay, fail
        self.calls: list[dict] = []
        self.warmed = 0
        self.completed = 0
        self.lock = threading.Lock()

    def warm(self) -> None:
        self.warmed += 1

    def stream(self, *, text, voice, instructions):
        from kotodama.tts_openai import TTSError
        with self.lock:
            self.calls.append({"text": text, "voice": voice, "instructions": instructions})
        if self.fail:
            raise TTSError(self.fail, 500)
        for _ in range(self.chunks):
            time.sleep(self.delay)
            yield b"\x10\x00" * (self.chunk_bytes // 2)
        with self.lock:
            self.completed += 1


class KotodamaTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.replies: list[dict] = []

    def make_config(self, **overrides) -> Config:
        data = {
            "cache": {"dir": str(self.tmp / "cache"), "max_mb": 64, "finish_download_on_stop": True},
            "library": {
                "file": str(self.tmp / "library" / "npcs.json"),
                "packs_dir": str(self.tmp / "library" / "packs"),
                "overrides": str(self.tmp / "config" / "overrides.json"),
            },
        }
        for k, v in overrides.items():
            if isinstance(v, dict) and isinstance(data.get(k), dict):
                data[k].update(v)
            else:
                data[k] = v
        return Config(data, root=self.tmp)

    def make_engine(self, tts=None, cfg: Config | None = None, **kwargs):
        cfg = cfg or self.make_config()
        # Never let a test touch the real PipeWire streams: inject a disabled ducker by default.
        kwargs.setdefault("ducker", Ducker(enabled=False))
        engine = build_engine(cfg, tts=tts or FakeTTS(), reply=self.replies.append,
                              null_player=kwargs.pop("null_player", True), **kwargs)
        self.addCleanup(engine.close)
        return engine

    def say_lines(self) -> list[str]:
        return [m["msg"] for m in self.replies if m.get("t") == "log"]


def speak_msg(text="Hello there.", name="Ayame", race=2, seq=1, **extra):
    msg = {"t": "speak", "seq": seq, "kind": "event", "mode": 150, "text": text, "zone": 230,
           "zone_name": "Southern San d'Oria", "player": "Adventurer",
           "speaker": {"name": name, "race": race, "race_name": "Hume F"}}
    msg.update(extra)
    return msg
