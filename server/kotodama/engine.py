"""The voiceover engine: turns protocol messages into audio.

Per spoken line (one thread each):
  resolve voice profile (optionally waiting briefly for LLM casting)
  -> cache lookup -> stream the cached WAV, or
  -> stream TTS, tee PCM into the player and the cache.
Both go through the same gain stage (volume + makeup, peak-limited) and the
game audio is ducked while a line plays. A new line (when interrupt is on) or a stop message kills the player at once.
With cache.finish_download_on_stop the download still completes into the
cache after a cut-off, because it has already been paid for.
"""

from __future__ import annotations

import logging
import threading
import time
import wave
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .archetypes import Archetypes, PROFILE_FIELDS
from .audio import Ducker, Limiter, db_to_gain
from .cache import AudioCache, make_key
from .config import Config
from .infer import InferenceManager, make_inferer
from .library import Library
from .player import Player, PlayerHandle
from .profiles import ProfileResolver, Resolved
from .protocol import Speak, Speaker, log_msg, parse_speak, parse_speaker
from .textnorm import normalize, scrub_player
from .tts_base import TTSError
from .tts_elevenlabs import ElevenLabsTTS
from .tts_openai import OpenAITTS
from .usage import Usage

PROVIDERS = {"openai": OpenAITTS, "elevenlabs": ElevenLabsTTS}
PROVIDER_KEYS = {"openai": "OPENAI_API_KEY", "elevenlabs": "ELEVENLABS_API_KEY"}

log = logging.getLogger("kotodama.engine")

ERROR_REPEAT_SECONDS = 30.0
MAX_SEEN = 500
MAX_VOLUME = 400


class Job:
    def __init__(self, speak: Speak | None = None, replay_path: Path | None = None,
                 ducker: Ducker | None = None):
        self.speak = speak
        self.replay_path = replay_path
        self.ducker = ducker
        self._ducked = False
        self._duck_lock = threading.Lock()
        self.cancel = threading.Event()
        self.playback_done = threading.Event()
        self.handle: PlayerHandle | None = None
        self.prev_done: threading.Event | None = None
        self.source: str | None = None   # "cache" | "tts" | "replay", for tests and logs
        self.error: str | None = None
        self.ttfb_ms: float | None = None

    def duck(self) -> None:
        """Lower the game audio for as long as this line is audible."""
        with self._duck_lock:
            if self.ducker is not None and not self._ducked and not self.cancel.is_set():
                self._ducked = True
                self.ducker.acquire()

    def unduck(self) -> None:
        with self._duck_lock:
            if self._ducked and self.ducker is not None:
                self._ducked = False
                self.ducker.release()

    def stop_playback(self) -> None:
        self.cancel.set()
        handle = self.handle
        if handle is not None:
            handle.kill()
        self.unduck()


class Engine:
    def __init__(self, cfg: Config, *, arch: Archetypes, library: Library, cache: AudioCache,
                 tts: Any, player: Player, inference: InferenceManager, usage: Usage,
                 reply: Callable[[dict[str, Any]], None] | None = None,
                 ducker: Ducker | None = None,
                 provider_factory: Callable[[str], Any] | None = None,
                 inferer_factory: Callable[[Any], Any] | None = None):
        self.cfg = cfg
        self.arch = arch
        self.lib = library
        self.cache = cache
        self.tts = tts
        self.player = player
        self.inference = inference
        self.usage = usage
        self.resolver = ProfileResolver(arch, library, provider=tts.name)
        self.reply = reply or (lambda _m: None)
        # Every provider used this session, so switching back is instant and
        # each keeps its warm connection and voice catalog.
        self.providers: dict[str, Any] = {tts.name: tts}
        self.default_provider = tts.name
        self._provider_factory = provider_factory
        self._inferer_factory = inferer_factory
        if hasattr(tts, "notify"):
            tts.notify = self.say

        self.volume = max(0, min(MAX_VOLUME, int(cfg.get("volume", 100))))
        self.makeup_db = float(cfg.get("audio.makeup_db", 9.0))
        self.makeup = db_to_gain(self.makeup_db)
        self.ducker = ducker or Ducker(enabled=False)
        self.ducker.notify = self.say
        self.interrupt = bool(cfg.get("interrupt", True))
        self.debug = False
        self.player_name: str | None = None
        self.max_chars = int(cfg.get("max_text_chars", 1500))
        self.finish_on_stop = bool(cfg.get("cache.finish_download_on_stop", True))

        self._lock = threading.Lock()
        self._jobs: set[Job] = set()
        self._current: Job | None = None
        self.last: dict[str, Any] | None = None       # for //koto replay
        self.last_job: Job | None = None
        self._seen: dict[str, dict[str, Any]] = {}    # name.lower() -> speaker/zone/samples
        self._err_at: dict[str, float] = {}

    # -- plumbing ---------------------------------------------------------

    def gain(self) -> float:
        """Linear gain applied to every sample: volume% x fixed makeup."""
        return self.volume / 100.0 * self.makeup

    def say(self, text: str) -> None:
        for line in text.splitlines():
            if line.strip():
                self.reply(log_msg(line))

    def _report(self, text: str) -> None:
        now = time.monotonic()
        if now - self._err_at.get(text, -ERROR_REPEAT_SECONDS) < ERROR_REPEAT_SECONDS:
            return
        self._err_at[text] = now
        log.error(text)
        self.say(f"error: {text}")

    def handle(self, msg: dict[str, Any]) -> None:
        kind = msg.get("t")
        if kind == "ping":
            self.reply({"t": "pong"})
        elif kind == "hello":
            name = msg.get("player")
            self.player_name = name if isinstance(name, str) and name else self.player_name
            self.reply({"t": "pong"})
            self._warm()
        elif kind == "config":
            self._config(msg)
        elif kind == "speak":
            sp = parse_speak(msg, self.max_chars)
            if sp:
                self.speak(sp)
        elif kind == "hint":
            self.hint(msg)
        elif kind == "stop":
            self.stop_all()
        elif kind == "replay":
            self.replay()
        elif kind == "cmd":
            try:
                self.command(msg)
            except Exception as exc:  # noqa: BLE001 - never let a command kill the server loop
                log.exception("command failed")
                self.say(f"command failed: {exc}")

    def _warm(self) -> None:
        warm = getattr(self.tts, "warm", None)
        if warm:
            warm()

    def _config(self, msg: dict[str, Any]) -> None:
        v = msg.get("volume")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            self.volume = max(0, min(MAX_VOLUME, int(v)))
        duck_level = msg.get("duck_level")
        self.ducker.configure(
            enabled=msg["duck"] if isinstance(msg.get("duck"), bool) else None,
            level=int(duck_level) if isinstance(duck_level, (int, float)) and not isinstance(duck_level, bool) else None,
        )
        provider = msg.get("provider")
        if isinstance(provider, str):
            # "" means "whatever server/config.json says".
            self.set_provider(provider.strip().lower() or self.default_provider)
        if isinstance(msg.get("interrupt"), bool):
            self.interrupt = msg["interrupt"]
        if isinstance(msg.get("debug"), bool):
            self.debug = msg["debug"]
            logging.getLogger("kotodama").setLevel(logging.DEBUG if self.debug else logging.INFO)

    # -- providers --------------------------------------------------------

    def _get_provider(self, name: str) -> Any | None:
        prov = self.providers.get(name)
        if prov is None and self._provider_factory is not None and name in PROVIDERS:
            prov = self._provider_factory(name)
            if hasattr(prov, "notify"):
                prov.notify = self.say
            self.providers[name] = prov
        return prov

    def set_provider(self, name: str) -> bool:
        """Switch the voice provider live. Lines already playing finish on the
        provider they started with; each provider keeps its own cached audio."""
        if name == self.tts.name:
            return True
        if name not in PROVIDERS:
            self.say(f"unknown voice provider '{name}' (choose: {', '.join(PROVIDERS)})")
            return False
        try:
            prov = self._get_provider(name)
        except Exception as exc:  # noqa: BLE001 - e.g. a bad output_format in config
            self.say(f"could not start {name}: {exc}")
            return False
        if prov is None:
            self.say(f"voice provider '{name}' is not available")
            return False
        if not getattr(prov, "api_key", None):
            self.say(f"{name} needs an API key ({PROVIDER_KEYS.get(name, 'see README')}); staying on {self.tts.name}")
            return False
        with self._lock:
            self.tts = prov
            self.resolver = ProfileResolver(self.arch, self.lib, provider=prov.name)
        if self._inferer_factory is not None:
            self.inference.retarget(self._inferer_factory(prov), prov.name)
        self._warm()
        if getattr(prov, "dynamic_voices", False):
            threading.Thread(target=prov.ensure_catalog, daemon=True).start()
        self.say(f"voice provider: {self._provider_status(prov)}")
        return True

    @staticmethod
    def _provider_status(prov: Any) -> str:
        status = getattr(prov, "status_text", None)
        return status() if status else f"{prov.name}/{prov.model}"

    # -- speaking ---------------------------------------------------------

    def _remember(self, speaker: Speaker, zone: int | None, zone_name: str | None,
                  text: str | None = None) -> None:
        if not speaker.name or speaker.is_player or speaker.narrator:
            return
        key = speaker.name.lower()
        entry = self._seen.setdefault(key, {"speaker": speaker, "samples": []})
        entry["speaker"], entry["zone"], entry["zone_name"] = speaker, zone, zone_name
        if text and text not in entry["samples"] and len(entry["samples"]) < 3:
            entry["samples"].append(text)
        if len(self._seen) > MAX_SEEN:
            self._seen.pop(next(iter(self._seen)))

    def speak(self, sp: Speak) -> Job:
        text = normalize(sp.text)
        self._remember(sp.speaker, sp.zone, sp.zone_name, scrub_player(text, sp.player or self.player_name))
        job = Job(sp, ducker=self.ducker)
        with self._lock:
            prev = self._current
            if self.interrupt:
                for j in list(self._jobs):
                    j.stop_playback()
            elif prev is not None:
                job.prev_done = prev.playback_done
            self._current = job
            self._jobs.add(job)
        threading.Thread(target=self._run, args=(job,), daemon=True, name=f"speak-{sp.seq}").start()
        return job

    def stop_all(self) -> None:
        with self._lock:
            jobs = list(self._jobs)
        for j in jobs:
            j.stop_playback()

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Block until every job finished (tests / the `test` CLI command)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self._jobs:
                    return True
            time.sleep(0.02)
        return False

    def replay(self) -> None:
        last = self.last
        if not last:
            self.say("nothing to replay yet")
            return
        path = self.cache.get(last["key"])
        if not path:
            self.say("the last line is no longer in the cache")
            return
        job = Job(replay_path=path, ducker=self.ducker)
        with self._lock:
            for j in list(self._jobs):
                j.stop_playback()
            self._current = job
            self._jobs.add(job)
        threading.Thread(target=self._run, args=(job,), daemon=True, name="replay").start()

    def _run(self, job: Job) -> None:
        try:
            if job.replay_path is not None:
                job.source = "replay"
                self._play_file(job, job.replay_path)
            else:
                self._speak_job(job)
        except Exception:  # noqa: BLE001
            log.exception("speak job crashed")
        finally:
            job.unduck()
            job.playback_done.set()
            with self._lock:
                self._jobs.discard(job)
                self.last_job = job

    def _resolve(self, sp: Speak, text: str, resolver: ProfileResolver) -> Resolved:
        speaker = sp.speaker
        res = resolver.resolve(speaker, sp.zone)
        player = sp.player or self.player_name
        scrubbed = scrub_player(text, player)
        if self.inference.enabled and resolver.needs_inference(speaker):
            fut = self.inference.request(
                speaker, zone=sp.zone, zone_name=sp.zone_name, samples=[scrubbed], player=player,
                archetype_key=res.archetype, base_profile=res.profile)
            if self.inference.wait(fut):
                res = resolver.resolve(speaker, sp.zone)
        elif speaker.name and not speaker.is_player:
            self.lib.note_seen(speaker.name, sp.zone, scrubbed)
        return res

    def _speak_job(self, job: Job) -> None:
        sp = job.speak
        assert sp is not None
        text = normalize(sp.text)
        with self._lock:                     # one provider for the whole line
            tts, resolver = self.tts, self.resolver
        res = self._resolve(sp, text, resolver)
        if job.cancel.is_set():
            return

        req = tts.voice_request(res.profile, res.key)
        key = make_key(tts.name, tts.model, req.voice, req.cache_tag, text, tts.response_format)
        self.last = {"key": key, "text": text, "npc": sp.speaker.name}
        log.debug("line npc=%s kind=%s voice=%s sources=%s", sp.speaker.name, sp.kind, req.label, res.sources)

        if job.prev_done is not None:
            job.prev_done.wait(120)
            if job.cancel.is_set():
                return

        path = self.cache.get(key)
        if path is None and not self.cache.claim(key):
            # Someone else is synthesizing this exact line right now.
            self.cache.wait_inflight(key, 60)
            path = self.cache.get(key)
            if path is None and not self.cache.claim(key):
                return
        if path is not None:
            job.source = "cache"
            self.usage.add(cache_hits=1)
            self._play_file(job, path)
            return

        try:
            job.source = "tts"
            self._synthesize(job, key, text, tts, req, sp)
        finally:
            self.cache.release(key)

    def _play_file(self, job: Job, path: Path) -> None:
        """Stream a cached WAV through the same gain stage as live audio."""
        # (The sample rate comes from the WAV header, so audio cached by either
        # provider plays correctly whichever one is active.)
        try:
            wav = wave.open(str(path), "rb")
        except (wave.Error, OSError, EOFError) as exc:
            job.error = f"unreadable cached audio {path.name}: {exc}"
            self._report(job.error)
            return
        with wav:
            handle = self.player.start_stream(100, wav.getframerate())
            job.handle = handle
            if job.cancel.is_set():
                handle.kill()
                return
            job.duck()
            limiter = Limiter(wav.getframerate())
            while not job.cancel.is_set():
                data = wav.readframes(2400)   # 100 ms at 24 kHz
                if not data or not handle.write(limiter.process(data, self.gain())):
                    break
            handle.finish_input()
        self._wait_playback(job, handle)

    @staticmethod
    def _wait_playback(job: Job, handle: PlayerHandle) -> None:
        try:
            while not handle.wait(0.05):
                if job.cancel.is_set():
                    handle.kill()
                    return
        finally:
            job.unduck()

    def _synthesize(self, job: Job, key: str, text: str, tts: Any, req: Any, sp: Speak) -> None:
        rate = int(getattr(tts, "sample_rate", 24000))
        writer = self.cache.begin(key, {"npc": sp.speaker.name, "zone": sp.zone, "text": text}, rate)
        # Start the player before the HTTP request so its startup overlaps the network wait.
        handle = self.player.start_stream(100, rate)   # gain is applied here, not by the player
        job.handle = handle
        if job.cancel.is_set():
            handle.kill()

        limiter = Limiter(rate)
        started = time.monotonic()
        stream = None
        try:
            stream = tts.stream(text, req)
            for chunk in stream:
                if job.ttfb_ms is None:
                    job.ttfb_ms = (time.monotonic() - started) * 1000
                    log.debug("first audio after %.0f ms", job.ttfb_ms)
                writer.write(chunk)          # the cache keeps the unscaled audio
                if not job.cancel.is_set():
                    job.duck()
                    handle.write(limiter.process(chunk, self.gain()))
                elif not self.finish_on_stop:
                    writer.abort()
                    return
            seconds = writer.commit()
            per_char = getattr(tts, "credits_per_char", None)
            self.usage.add_tts(tts.name, len(text), seconds, per_char() * len(text) if per_char else None)
        except TTSError as exc:
            writer.abort()
            handle.kill()
            job.error = str(exc)
            self._report(str(exc))
            return
        except BaseException:
            writer.abort()
            handle.kill()
            raise
        finally:
            if stream is not None:
                stream.close()

        handle.finish_input()
        self._wait_playback(job, handle)

    # -- hints / voices ---------------------------------------------------

    def hint(self, msg: dict[str, Any]) -> None:
        name = msg.get("npc")
        if not isinstance(name, str) or not name.strip():
            return
        speaker = parse_speaker({
            "name": name, "race": msg.get("race"), "race_name": msg.get("race_name"),
            "model": msg.get("model"), "creature": msg.get("creature"),
        })
        zone = msg.get("zone") if isinstance(msg.get("zone"), int) else None
        zone_name = msg.get("zone_name") if isinstance(msg.get("zone_name"), str) else None
        self._remember(speaker, zone, zone_name)
        self._warm()
        if getattr(self.tts, "dynamic_voices", False):
            self.tts.ensure_catalog(wait=False)      # never block the network thread
        if self.inference.enabled and self.resolver.needs_inference(speaker):
            res = self.resolver.resolve(speaker, zone)
            if res.archetype == "unknown":
                # No race from the game (a fixed "precomposed" model) and
                # nothing in the name: casting now would be a guess from the
                # name alone, and the first line would join that guess
                # instead of informing it. Cast when the NPC speaks.
                return
            self.inference.request(
                speaker, zone=zone, zone_name=zone_name, samples=None, player=self.player_name,
                archetype_key=res.archetype, base_profile=res.profile)

    def _speaker_for(self, npc: str) -> tuple[Speaker, int | None, str | None, list[str]]:
        entry = self._seen.get(npc.lower())
        if entry:
            return entry["speaker"], entry.get("zone"), entry.get("zone_name"), list(entry["samples"])
        return Speaker(name=npc), None, None, []

    def _format_profile(self, npc: str, res: Resolved) -> str:
        p = res.profile
        who = " ".join(x for x in (p.get("gender"), p.get("age"), p.get("species")) if x)
        req = self.tts.voice_request(p, res.key)
        cast_note = "" if p.get("voice") else " (auto-cast)"
        lines = [
            f"{npc}: {who or 'unspecified'} | pitch {p.get('pitch')} | pace {p.get('pace')} | "
            f"{self.tts.name} voice {req.label or req.voice or 'none'}{cast_note}",
            f"  personality: {p.get('personality') or '-'} | tone: {p.get('tone') or '-'} | accent: {p.get('accent') or '-'}",
        ]
        if p.get("direction"):
            lines.append(f"  direction: {p['direction']}")
        lines.append("  layers: " + " > ".join(res.sources))
        return "\n".join(lines)

    # -- //koto commands --------------------------------------------------

    def command(self, msg: dict[str, Any]) -> None:
        name = msg.get("name")
        npc = msg.get("npc") if isinstance(msg.get("npc"), str) else None

        if name == "status":
            self._status()
            return
        if name in ("voices_sync", "voices_info"):
            self._voices_command(name == "voices_sync")
            return
        if not npc:
            self.say("no NPC given")
            return

        speaker, seen_zone, seen_zone_name, samples = self._speaker_for(npc)
        zone = msg.get("zone") if isinstance(msg.get("zone"), int) else seen_zone

        if name == "voice_show":
            self.say(self._format_profile(npc, self.resolver.resolve(speaker, zone)))

        elif name == "voice_set":
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            allowed = set(PROFILE_FIELDS) | {"voice"}
            unknown = sorted(k for k in params if k not in allowed)
            if unknown or not params:
                self.say(f"unknown field(s): {', '.join(unknown) or '-'}. Fields: {', '.join(sorted(allowed))}")
                return
            raw = dict(params)
            if "voice" in raw:
                wanted = str(raw["voice"])
                resolve = getattr(self.tts, "resolve_voice", None)
                if resolve is not None:            # account voices: name -> id
                    voice_id = resolve(wanted)
                    if not voice_id:
                        self.say(f"no {self.tts.name} voice called '{wanted}' (//koto voices sync, or see "
                                 "`kotodama_server.py voices list`)")
                        return
                    wanted = voice_id
                raw["voice"] = {self.tts.name: wanted}
            try:
                clean = self.arch.sanitize(raw, strict=True)
            except ValueError as exc:
                self.say(str(exc))
                return
            self.lib.set_override(npc, clean)
            self.say(self._format_profile(npc, self.resolver.resolve(speaker, zone)))

        elif name == "voice_reset":
            self._forget_casting(npc)
            removed = self.lib.reset_override(npc)
            self.say(f"{npc}: overrides cleared" if removed else f"{npc}: no overrides to clear")

        elif name == "voice_promote":
            res = self.resolver.resolve(speaker, zone)
            profile = {k: v for k, v in res.profile.items() if k != "voice" and v}
            profile["voice"] = {self.tts.name: self.tts.voice_request(res.profile, res.key).voice}
            self.lib.put(npc, profile, source="curated", game_race=speaker.race_name,
                         model_id=speaker.model, zone=zone)
            self.say(f"{npc}: saved to the library as curated")

        elif name == "voice_reinfer":
            if not self.inference.enabled:
                self.say("voice casting is off (inference.provider is 'none' in server/config.json)")
                return
            key = npc.lower()
            local = self.lib.local.get(key)
            if local and not str(local["meta"].get("source", "")).startswith("llm"):
                self.say(f"{npc} has a {local['meta'].get('source')} library entry; remove it first to recast")
                return
            if not samples and local:
                # Sidecar restarted since the NPC last spoke: reuse the lines
                # stored with the old casting so the new one isn't name-only.
                stored = local["meta"].get("sample_lines")
                samples = [s for s in stored if isinstance(s, str)][:3] if isinstance(stored, list) else []
            self.lib.delete_local(key)
            self._forget_casting(npc)
            self.inference.clear_failure(npc)
            res = self.resolver.resolve(speaker, zone)
            fut = self.inference.request(
                speaker, zone=zone, zone_name=seen_zone_name, samples=samples, player=self.player_name,
                archetype_key=res.archetype, base_profile=res.profile, force=True)
            if fut is None:
                self.say("casting is throttled right now; try again in a minute")
                return
            self.say(f"{npc}: casting voice...")

            def _done(f, npc=npc, speaker=speaker, zone=zone) -> None:
                if f.result() is None:
                    self.say(f"{npc}: casting failed (see server log)")
                else:
                    self.say(self._format_profile(npc, self.resolver.resolve(speaker, zone)))
            fut.add_done_callback(_done)

        else:
            self.say(f"unknown command {name}")

    def _forget_casting(self, npc: str) -> None:
        for prov in self.providers.values():
            forget = getattr(prov, "forget_casting", None)
            if forget:
                forget(npc)

    def _voices_command(self, sync: bool) -> None:
        prov = self._get_provider("elevenlabs")
        if prov is None or not getattr(prov, "api_key", None):
            self.say("voice lists only apply to ElevenLabs, and it needs ELEVENLABS_API_KEY")
            return
        if not sync:
            age = time.time() - prov.catalog.fetched_at if prov.catalog.fetched_at else None
            when = f"{age / 3600:.1f} h ago" if age is not None else "never"
            self.say(f"ElevenLabs: {len(prov.catalog.voices)} voices (synced {when}). "
                     "Full list: kotodama_server.py voices list")
            return

        def done(count: int | None, error: Exception | None) -> None:
            self.say(f"ElevenLabs voices: {count} available" if error is None
                     else f"ElevenLabs voice sync failed: {error}")
        if not prov.catalog.sync_in_background(done):
            self.say("a voice sync is already running")

    def _status(self) -> None:
        stats = self.cache.stats()
        key_state = "key set" if getattr(self.tts, "api_key", None) else "NO API KEY"
        inf = self.inference.inferer.model if self.inference.enabled else "off"
        self.say(
            f"sidecar {__version__} | tts {self._provider_status(self.tts)} ({key_state}) | "
            f"player {self.player.kind or 'NONE'} | casting {inf}")
        self.say(f"cache: {stats['files']} lines, {stats['bytes'] / 1048576:.1f} / "
                 f"{stats['max_bytes'] / 1048576:.0f} MB | {self.usage.summary()}")
        self.say(f"volume {self.volume}% (+{self.makeup_db:.0f} dB makeup, peak-limited) | "
                 f"duck {'on' if self.ducker.enabled else 'off'} to {self.ducker.level * 100:.0f}%")
        self.say(f"library: {len(self.lib.local)} local, {len(self.lib.packs)} pack, "
                 f"{len(self.lib.overrides)} override entries")

    def close(self) -> None:
        self.stop_all()
        self.wait_idle(3)   # let in-flight downloads land in the cache
        self.lib.flush()
        for prov in self.providers.values():
            close = getattr(prov, "close", None)
            if close:
                close()
        self.cache.close()
        self.ducker.shutdown()


def make_ducker(cfg: Config, *, enabled: bool = True) -> Ducker:
    return Ducker(
        enabled=enabled and bool(cfg.get("duck.enabled", True)),
        level=int(cfg.get("duck.level", 40)),
        match=list(cfg.get("duck.match", [])),
        attack_ms=int(cfg.get("duck.attack_ms", 120)),
        release_ms=int(cfg.get("duck.release_ms", 700)),
        hold_ms=int(cfg.get("duck.hold_ms", 500)),
        state_path=cfg.path("cache.dir") / "duck_state.json",
    )


def make_provider(cfg: Config, name: str) -> Any:
    try:
        return PROVIDERS[name](cfg)
    except KeyError:
        raise ValueError(f"unknown tts.provider '{name}' (choose: {', '.join(PROVIDERS)})") from None


def build_engine(cfg: Config, *, tts: Any = None, player: Player | None = None,
                 reply: Callable[[dict[str, Any]], None] | None = None,
                 null_player: bool = False, inferer: Any = None,
                 ducker: Ducker | None = None, provider: str | None = None,
                 provider_factory: Callable[[str], Any] | None = None) -> Engine:
    arch = Archetypes.load()
    library = Library(arch, cfg.path("library.file"), cfg.path("library.packs_dir"),
                      cfg.path("library.overrides"))
    cache = AudioCache(cfg.path("cache.dir"), int(cfg.get("cache.max_mb", 2048)))
    usage = Usage(cfg.path("cache.dir") / "usage.json")
    factory = provider_factory or (lambda name: make_provider(cfg, name))
    tts = tts or factory((provider or str(cfg.get("tts.provider", "openai"))).strip().lower())
    if player is None:
        command = cfg.get("player.command")
        player = Player(command if isinstance(command, dict) else None, null=null_player)
    def inferer_factory(prov: Any) -> Any:
        return make_inferer(cfg, arch, prov.name, include_voice=not getattr(prov, "dynamic_voices", False))

    fixed_inferer = inferer is not None          # tests inject one; keep it across switches
    inferer = inferer if fixed_inferer else inferer_factory(tts)
    inference = InferenceManager(cfg, arch, library, usage, inferer, tts_provider=tts.name)
    if ducker is None:
        ducker = make_ducker(cfg, enabled=not null_player)
        if not null_player:
            ducker.restore_stale()   # undo a duck left behind by a crashed run
    return Engine(cfg, arch=arch, library=library, cache=cache, tts=tts, player=player,
                  inference=inference, usage=usage, reply=reply, ducker=ducker,
                  provider_factory=factory,
                  inferer_factory=None if fixed_inferer else inferer_factory)
