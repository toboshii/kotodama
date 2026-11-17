"""Command line: serve, test, library, cache, audio, voices, doctor."""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import shutil
import signal
import re
import socket
import sys
import threading
import time
import urllib.parse
import wave
from pathlib import Path
from typing import Any

from . import __version__, envfile
from .adapters import (build_adapter, check_fields, draft_adapter, input_properties, load_adapters,
                       load_models_file, render)
from .archetypes import Archetypes
from .audio import is_game_stream, parse_sink_inputs, run_pactl
from .casting import cast, rank
from .config import DEFAULT_PROVIDER, Config
from .console import KeyControls, reexec
from .engine import PROVIDER_KEYS, build_engine, make_ducker, make_provider, provider_catalog, provider_notice
from .library import Library
from .net import UDPServer
from .profiles import ProfileResolver
from .protocol import Speaker, parse_speak
from .tts_base import TTSError
from .tts_elevenlabs import ElevenLabsTTS
from .tts_gemini import TEMPLATE_FIELDS as GEMINI_TEMPLATE_FIELDS, GeminiClient
from .tts_generic import GenericTTS, ReplicateClient, gemini_voice
from .tts_inworld import TEMPLATE_FIELDS as INWORLD_TEMPLATE_FIELDS, InworldClient, inworld_voice
from .tts_minimax import MiniMaxClient, unknown_fields as minimax_unknown_fields
from .tts_openai import shutdown_notice
from .voice_bank import DESIGN_TEXT, VoiceBank, design_jobs, save_designed


def _setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_serve(cfg: Config, args: argparse.Namespace, *, stdin=None, execv=os.execv, engine=None) -> int:
    engine = engine or build_engine(cfg)
    if not engine.player.available:
        logging.error("no audio player available; install pipewire (pw-play), pulseaudio-utils or ffmpeg")
    if getattr(engine.tts, "needs_key", True) and not engine.tts.api_key:
        ready = [n for n, (_desc, has_key) in engine.provider_info.items() if has_key]
        logging.warning("no %s API key: only already-cached lines will play (set %s%s)", engine.tts.name,
                        getattr(engine.tts, "key_hint", None) or PROVIDER_KEYS.get(engine.tts.name, "the provider key"),
                        f", or switch in game with //koto provider NAME; ready to use: {', '.join(ready)}"
                        if ready else "")
    notice = provider_notice(engine.tts)
    if notice:
        logging.warning("%s", notice)
    server = UDPServer(cfg, engine)
    old_term = signal.signal(signal.SIGTERM, lambda *_: server.shutdown())
    chosen: list[str] = []

    def on_key(action: str) -> None:
        chosen.append(action)
        logging.info("%s requested from the keyboard", action)
        server.shutdown()

    keys = KeyControls(on_key, stdin)
    logging.info("Kotodama sidecar %s ready (player: %s, casting: %s)", __version__, engine.player.kind,
                 engine.inference.inferer.model if engine.inference.enabled else "off")
    if keys.start():
        logging.info("press R to restart (reloads code and config) or Q to quit")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        keys.stop()
        server.shutdown()
        engine.close()
        signal.signal(signal.SIGTERM, old_term)
    if chosen and chosen[0] == "restart":
        logging.info("restarting...")
        reexec(execv)
    return 0


def cmd_test(cfg: Config, args: argparse.Namespace) -> int:
    def show(msg: dict) -> None:
        if msg.get("t") == "log":
            print(f"[addon] {msg['msg']}")

    try:
        engine = build_engine(cfg, reply=show, null_player=args.no_audio, provider=args.provider)
    except ValueError as exc:
        print(exc)
        return 1
    text = " ".join(args.text)
    speaker = {"name": args.name, "race": args.race, "creature": args.creature,
               "narrator": args.name is None}
    engine.handle({"t": "hello", "player": args.player})
    job = engine.speak(parse_speak({
        "seq": 1, "kind": args.kind, "mode": 150, "text": text, "zone": args.zone,
        "zone_name": args.zone_name, "player": args.player, "speaker": speaker}))
    if not engine.wait_idle(60):
        print("timed out waiting for playback")
        return 1
    print(f"provider={engine.tts.name}/{engine.tts.model} source={job.source} "
          f"first-audio={'%.0f ms' % job.ttfb_ms if job.ttfb_ms else 'n/a'}")
    print(engine.usage.summary())
    engine.close()
    return 1 if job.error or not job.source else 0


def cmd_library(cfg: Config, args: argparse.Namespace) -> int:
    engine = build_engine(cfg, null_player=True)
    lib = engine.lib
    if args.action == "import":
        dest, n = lib.import_pack(Path(args.file))
        print(f"imported {n} NPC profiles -> {dest}")
    elif args.action == "export":
        data = lib.export(args.source or None)
        out = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output:
            Path(args.output).write_text(out, encoding="utf-8")
            print(f"wrote {len(data['npcs'])} NPC profiles -> {args.output}")
        else:
            sys.stdout.write(out)
    else:
        for entry in sorted(lib.local.values(), key=lambda e: e["key"].lower()):
            print(f"{entry['key']:<28} {entry['meta'].get('source', '?')}")
        print(f"-- {len(lib.local)} local, {len(lib.packs)} pack, {len(lib.overrides)} override entries")
    return 0


def cmd_cache(cfg: Config, args: argparse.Namespace) -> int:
    engine = build_engine(cfg, null_player=True)
    cache = engine.cache
    if args.action == "stats":
        s = cache.stats()
        print(f"{s['files']} lines, {s['bytes'] / 1048576:.1f} MB of {s['max_bytes'] / 1048576:.0f} MB, "
              f"{s['seconds'] / 60:.1f} min of audio")
        print(engine.usage.summary())
    elif args.action == "prune":
        print(f"removed {cache.prune(target_fraction=args.fraction)} lines")
    elif args.action == "analyze":
        return _analyze_cache(cache, args.all)
    return 0


def _analyze_cache(cache: Any, everything: bool) -> int:
    """Measure the loudness of cached lines (those cached before loudness
    was tracked, or with --all every line), so normalization knows each
    voice's level from its first line this session."""
    from .loudness import SILENT, measure_wav
    rows = cache.to_measure(everything)
    if not rows:
        print("every cached line is already measured")
        return 0
    levels, failed = [], 0
    started = time.monotonic()
    for i, (key, path) in enumerate(rows, 1):
        try:
            level, _seconds = measure_wav(Path(path))
        except (OSError, EOFError, wave.Error):
            failed += 1
            continue
        cache.annotate(key, lufs=level, replace=everything)
        if level > SILENT:
            levels.append(level)
        if i % 100 == 0:
            print(f"  {i}/{len(rows)}...", flush=True)
    print(f"measured {len(rows) - failed} lines in {time.monotonic() - started:.0f} s"
          + (f" ({failed} unreadable)" if failed else ""))
    if levels:
        levels.sort()
        print(f"loudness: quietest {levels[0]:.1f}, median {levels[len(levels) // 2]:.1f}, "
              f"loudest {levels[-1]:.1f} LUFS")
    return 0


def cmd_audio(cfg: Config, args: argparse.Namespace) -> int:
    match = list(cfg.get("duck.match", []))
    ducker = make_ducker(cfg)
    try:
        streams = parse_sink_inputs(run_pactl("list", "sink-inputs"))
    except FileNotFoundError:
        print("pactl not found (install pipewire-pulse / pulseaudio-utils); ducking is unavailable")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"could not list audio streams: {exc}")
        return 1

    if args.action == "list":
        print(f"duck.match = {match}\n")
        for st in streams:
            hit = is_game_stream(st, match)
            vol = "/".join(f"{v * 100 // 65536}%" for v in st.volumes) or "?"
            print(f"{'DUCK ' if hit else '     '} #{st.id:<5} {st.label:<24} vol {vol:<9} "
                  f"binary={st.props.get('application.process.binary', '-')} "
                  f"app_id={st.props.get('pipewire.access.portal.app_id', '-')}")
        if not streams:
            print("no playback streams right now (start the game, then run this again)")
        elif not any(is_game_stream(st, match) for st in streams):
            print("\nnothing matches: add part of your game's stream name or app id to duck.match")
        return 0

    if args.action == "restore":
        n = ducker.restore_stale()
        print(f"restored {n} stream(s)" if n else "nothing to restore")
        return 0

    # duck-test
    found = ducker.scan()
    if not found:
        print("no game stream matched; run `audio list` and adjust duck.match")
        return 1
    print("Ducking: " + ", ".join(f"#{s.id} {s.label}" for s in found)
          + f" to {cfg.get('duck.level')}% for 3 seconds (your game volume will dip, then return)...")
    ducker.acquire()
    import time as _time
    _time.sleep(3.0)
    ducker.release()
    ok = ducker.settled(10.0)
    ducker.shutdown()
    print("restored." if ok else "WARNING: did not settle; run `audio restore`")
    return 0 if ok else 1


def _bank(cfg: Config) -> VoiceBank:
    bank = VoiceBank(cfg.path("voice_bank.dir"))
    bank.refresh()
    return bank


def cmd_voices(cfg: Config, args: argparse.Namespace) -> int:
    if args.action == "bank":
        return _voices_bank(cfg)
    if args.action == "design":
        return _voices_design(cfg, args)
    name = (args.provider or "elevenlabs").lower()
    if name == "openai":
        print("OpenAI voices are fixed per archetype (see server/archetypes.json); "
              "pick --provider elevenlabs or a model adapter (`kotodama_server.py providers`)")
        return 1
    if name != "elevenlabs":
        return _voices_adapter(cfg, args, name)
    el = ElevenLabsTTS(cfg)
    if args.action == "sync" or (not el.catalog.voices and el.api_key):
        if not el.api_key:
            print("set ELEVENLABS_API_KEY (or elevenlabs.api_key in server/config.json) first")
            return 1
        try:
            print(f"synced {el.catalog.sync()} voices -> {el.catalog.path}")
        except (TTSError, OSError) as exc:
            print(f"voice sync failed: {exc}")
            return 1
        if args.action == "sync":
            return 0
    voices = el.catalog.snapshot()
    if not voices:
        print("no voices cached yet: run `voices sync`")
        return 1
    return _voices_show(cfg, args, el.catalog, "elevenlabs")


def _voices_adapter(cfg: Config, args: argparse.Namespace, name: str) -> int:
    adapters = load_adapters(cfg)
    if name not in adapters:
        print(f"unknown provider '{name}' (model adapters: {', '.join(adapters)})")
        return 1
    prov = GenericTTS(cfg, adapters[name], bank=_bank(cfg))
    try:
        prov.load_remote_voices()                    # the CLI can wait for the bank, schema, account voices
        if args.action == "sync":
            print(prov.status_text())
            return 0
        if not prov.catalog.voices:
            hint = (" (set REPLICATE_API_TOKEN so the model's own voice list can be read)"
                    if prov.adapter.uses_schema and not prov.api_key else "")
            print(f"{name} has no voices to cast from{hint}; lines use the model's default voice"
                  + (". Add clips to the voice bank (`voices bank`, `voices design`)" if prov.adapter.uses_bank else ""))
            return 0 if args.action == "list" else 1
        return _voices_show(cfg, args, prov.catalog, name)
    finally:
        prov.close()


def _voices_show(cfg: Config, args: argparse.Namespace, catalog: Any, provider: str) -> int:
    voices = catalog.snapshot()

    if args.action == "list":
        for v in voices:
            print(f"{v.id:<22} {v.summary()}")
        print(f"-- {len(voices)} voices")
        return 0

    # cast: show what auto-casting picks, without touching the casting memory
    arch = Archetypes.load()
    if args.npc:
        lib = Library(arch, cfg.path("library.file"), cfg.path("library.packs_dir"), cfg.path("library.overrides"))
        resolved = ProfileResolver(arch, lib, provider=provider).resolve(
            Speaker(name=args.npc, race=args.race, creature=args.creature), None)
        targets = [(args.npc, resolved.key, resolved.profile)]
    else:
        targets = [(key, key, {f: a.get(f) for f in ("gender", "age", "species", "personality", "tone",
                                                     "accent", "pitch", "pace")})
                   for key, a in arch.items.items()]
        targets = [(label, "@narrator" if label == "narrator" else ident, p) for label, ident, p in targets]
    for label, ident, profile in targets:
        chosen = catalog.get(cast(catalog, profile, ident, memory=None))
        runners = ", ".join(f"{v.name} ({s:g})" for s, v in rank(catalog, profile, ident)[:3])
        print(f"{label:<16} -> {chosen.name if chosen else 'NO MATCH':<22} top: {runners}")
    return 0


def _voices_bank(cfg: Config) -> int:
    bank = _bank(cfg)
    print(f"voice bank: {bank.dir}")
    for clip in bank.clips.values():
        ref = "transcript" if clip.reference_text else "no transcript"
        print(f"  {clip.id:<32} {clip.seconds:4.1f} s  {ref:<13} {clip.voice.summary()}")
    for problem in bank.problems:
        print(f"  [skipped] {problem}")
    if not bank.clips:
        print("  (empty) add 5.5-7.5 s WAV clips here, or run `voices design`")
    return 0


def _voices_design(cfg: Config, args: argparse.Namespace) -> int:
    data = load_models_file()
    designer_name = str(cfg.get("voice_bank.designer", "qwen3"))
    spec = (data.get("designers") or {}).get(designer_name)
    if not isinstance(spec, dict):
        print(f"no designer '{designer_name}' in tts_models.json")
        return 1
    designer = build_adapter(f"design-{designer_name}", spec, data.get("voice_sets", {}))
    key = cfg.api_key("replicate")
    if not key:
        print("voice design runs on Replicate: set REPLICATE_API_TOKEN first")
        return 1
    directory = cfg.path("voice_bank.dir")
    only = [o.strip() for o in args.only.split(",")] if args.only else None
    arch = Archetypes.load()
    if only:
        unknown = [o for o in only if o not in arch.items]
        if unknown:
            print(f"unknown archetype(s): {', '.join(unknown)} (choose from {', '.join(arch.items)})")
            return 1
    jobs = design_jobs(arch.items, args.variants, only, directory)
    if not jobs:
        print(f"nothing to do: every clip already exists in {directory}")
        return 0
    print(f"{len(jobs)} reference clip(s) to design with {designer.model} -> {directory}")
    print("each is one Replicate prediction, billed to your account")
    if not args.yes:
        try:
            answer = input("continue? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("cancelled")
            return 1
    client = ReplicateClient(str(cfg.get("replicate.base_url")), key,
                             wait_s=int(cfg.get("replicate.wait_seconds", 30)),
                             timeout=float(cfg.get("replicate.timeout", 120)), cache_dir=cfg.path("cache.dir"))
    failures = 0
    try:
        for job in jobs:
            inp = render(designer.input, {"text": DESIGN_TEXT, "voice_description": job.description})
            try:
                audio, _ = client.run(designer, inp)
                path = save_designed(directory, job, audio)
                print(f"  {path.name}: {job.description}")
            except (TTSError, OSError) as exc:
                failures += 1
                print(f"  {job.stem}: FAILED ({exc})")
    finally:
        client.close()
    bank = _bank(cfg)
    print(f"voice bank now holds {len(bank.clips)} clip(s)"
          + (f"; {len(bank.problems)} skipped (see `voices bank`)" if bank.problems else ""))
    return 1 if failures else 0


def cmd_providers(cfg: Config, args: argparse.Namespace) -> int:
    problems: list[str] = []
    adapters = load_adapters(cfg, problems)
    active = str(cfg.get("tts.provider", DEFAULT_PROVIDER))
    catalog = provider_catalog(cfg, adapters)
    width = max(len(n) for n in catalog) + 2
    for name, (desc, has_key) in catalog.items():
        print(f"{'*' if name == active else ' '} {name:<{width}}{desc}{'' if has_key else '  (no API key)'}")
    for p in problems:
        print(f"  [skipped] {p}")
    return 0


def cmd_replicate(cfg: Config, args: argparse.Namespace) -> int:
    key = cfg.api_key("replicate")
    if not key:
        print("set REPLICATE_API_TOKEN (or replicate.api_key in server/config.json) first")
        return 1
    ref = args.model.strip()
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", ref):
        print("give the model as owner/name, e.g. resemble-ai/chatterbox-turbo")
        return 1
    client = ReplicateClient(str(cfg.get("replicate.base_url")), key, cache_dir=cfg.path("cache.dir"))
    try:
        data = client.model_json(ref, refresh=True)
    except TTSError as exc:
        print(f"could not read {ref}: {exc}")
        return 1
    finally:
        client.close()
    props, required = input_properties(data)
    if not props:
        print(f"{ref} publishes no input schema")
        return 1
    print(f"{ref} inputs:")
    for k, p in props.items():
        bits = [str(p.get("type") or "?")]
        if p.get("format"):
            bits.append(p["format"])
        if "minimum" in p or "maximum" in p:
            bits.append(f"{p.get('minimum', '')}..{p.get('maximum', '')}")
        if p.get("enum"):
            enum = [str(e) for e in p["enum"]]
            bits.append("one of " + ", ".join(enum[:10]) + (f" (+{len(enum) - 10})" if len(enum) > 10 else ""))
        if "default" in p:
            bits.append(f"default {p['default']!r}")
        desc = str(p.get("description") or "")[:70]
        print(f"  {k + ('*' if k in required else ''):<22} {'; '.join(bits)}{('  - ' + desc) if desc else ''}")
    official = bool(data.get("is_official")) if "is_official" in data else False
    name = re.sub(r"[^a-z0-9_-]", "-", ref.split("/")[1].lower())[:32]
    draft = draft_adapter(ref, props, required, official)
    print(f"\nA starting adapter (put it in server/config.json under \"models\", then edit):\n")
    print(json.dumps({name: draft}, indent=2))
    if "is_official" not in data:
        print("\nnote: if predictions fail asking for a version, this is an official model: set \"official\": true")
    return 0


def _doctor_elevenlabs(cfg: Config, live: bool, check) -> None:
    el = ElevenLabsTTS(cfg)
    if not el.api_key:
        print("[info] ElevenLabs: no key (ELEVENLABS_API_KEY); provider unavailable")
        return
    check("ElevenLabs API key", True, "set")
    try:
        if not el.catalog.voices:
            el.catalog.sync()
        check("ElevenLabs voices", bool(el.catalog.voices), f"{len(el.catalog.voices)} available")
    except (TTSError, OSError) as exc:
        check("ElevenLabs voices", False, str(exc))
        return
    finally:
        el.close()
    if not live:
        print(f"[info] ElevenLabs model {el.configured_model}: `doctor --provider elevenlabs` tries it "
              f"(uses a few credits)")


def _doctor_models(cfg: Config, check, active: str) -> None:
    problems: list[str] = []
    adapters = load_adapters(cfg, problems)
    for p in problems:
        check("model adapter", False, p)
    replicate = {n: a for n, a in adapters.items() if a.transport == "replicate"}
    compat = {n: a for n, a in adapters.items() if a.transport == "openai_compatible"}
    key = cfg.api_key("replicate")
    if replicate and not key:
        print(f"[info] Replicate: no token (REPLICATE_API_TOKEN); {', '.join(replicate)} unavailable")
    elif replicate:
        client = ReplicateClient(str(cfg.get("replicate.base_url")), key, cache_dir=cfg.path("cache.dir"))
        try:
            account = client.account()
            check("Replicate token", True, f"account {account.get('username', '?')}")
            for name, ad in replicate.items():
                try:
                    props, _ = input_properties(client.model_json(ad.model, refresh=True))
                except TTSError as exc:
                    print(f"[warn] {name} ({ad.model}): {exc}")
                    continue
                issues = check_fields(ad, props)
                if not issues:
                    print(f"[ok] {name}: {ad.model} matches its adapter")
                elif name == active:
                    check(name, False, "; ".join(issues))
                else:
                    print(f"[warn] {name}: " + "; ".join(issues))
        except TTSError as exc:
            check("Replicate token", False, str(exc))
        finally:
            client.close()
    minimax = {n: a for n, a in adapters.items() if a.transport == "minimax"}
    mm_key = cfg.api_key("minimax")
    if minimax and not mm_key:
        print(f"[info] MiniMax: no key (MINIMAX_API_KEY); {', '.join(minimax)} unavailable")
    elif minimax:
        first = next(iter(minimax.values()))
        client = MiniMaxClient(first.base_url or str(cfg.get("minimax.base_url")), mm_key,
                               group_id=str(cfg.get("minimax.group_id")) if cfg.get("minimax.group_id") else None)
        try:
            data = client.fetch_voices()
            own = len(data.get("voice_cloning") or []) + len(data.get("voice_generation") or [])
            check("MiniMax key", True, f"{len(data.get('system_voice') or [])} system voices, {own} of your own")
        except TTSError as exc:
            check("MiniMax key", False, str(exc))
        finally:
            client.close()
    for name, ad in minimax.items():
        bad = minimax_unknown_fields(ad.input)
        if bad:
            msg = (f"{', '.join(bad)} aren't MiniMax API request fields (an override written for the Replicate "
                   f"version? those belong under models.minimax-replicate)")
            if name == active:
                check(name, False, msg)
            else:
                print(f"[warn] {name}: {msg}")
    gemini = {n: a for n, a in adapters.items() if a.transport == "gemini"}
    g_key = cfg.api_key("gemini")
    if gemini and not g_key:
        print(f"[info] Gemini: no key (GEMINI_API_KEY); {', '.join(gemini)} unavailable")
    elif gemini:
        first = next(iter(gemini.values()))
        client = GeminiClient(first.base_url or str(cfg.get("gemini.base_url")), g_key)
        try:
            languages = [str(x).lower() for x in (cfg.get("gemini.voices.languages", ["en"]) or [])]
            usable = [v for v in (gemini_voice(i, languages) for i in client.fetch_voices()) if v]
            custom = sum(1 for v in usable if v.category == "custom")
            check("Gemini key", True, f"{len(usable) - custom} voices in your languages, {custom} of your own")
        except TTSError as exc:
            check("Gemini key", False, str(exc))
        finally:
            client.close()
    for name, ad in gemini.items():
        bad = [k for k in ad.input if k not in GEMINI_TEMPLATE_FIELDS]
        if bad:
            msg = f"{', '.join(bad)} aren't used by the Gemini API transport (only style and generation_config are)"
            if name == active:
                check(name, False, msg)
            else:
                print(f"[warn] {name}: {msg}")
    inworld = {n: a for n, a in adapters.items() if a.transport == "inworld"}
    iw_key = cfg.api_key("inworld")
    if inworld and not iw_key:
        print(f"[info] Inworld: no key (INWORLD_API_KEY); {', '.join(inworld)} unavailable")
    elif inworld:
        first = next(iter(inworld.values()))
        iw = InworldClient(first.base_url or str(cfg.get("inworld.base_url")), iw_key)
        try:
            languages = [str(x).lower() for x in (cfg.get("inworld.voices.languages", ["en"]) or [])]
            usable = [v for v in (inworld_voice(i, languages) for i in iw.fetch_voices()) if v]
            custom = sum(1 for v in usable if v.category == "custom")
            check("Inworld key", True, f"{len(usable) - custom} voices in your languages, {custom} of your own")
        except TTSError as exc:
            check("Inworld key", False, str(exc))
        finally:
            iw.close()
    for name, ad in inworld.items():
        bad = [k for k in ad.input if k not in INWORLD_TEMPLATE_FIELDS]
        if bad:
            msg = (f"{', '.join(bad)} aren't sent by the Inworld transport (only "
                   f"{' and '.join(sorted(INWORLD_TEMPLATE_FIELDS))} are)")
            if name == active:
                check(name, False, msg)
            else:
                print(f"[warn] {name}: {msg}")
    for name, ad in compat.items():
        p = urllib.parse.urlparse(ad.base_url)
        try:
            with socket.create_connection((p.hostname, p.port or (443 if p.scheme == "https" else 80)), timeout=2):
                print(f"[ok] {name}: {ad.base_url} reachable")
        except OSError as exc:
            print(f"[{'FAIL' if name == active else 'warn'}] {name}: {ad.base_url} not reachable ({exc})")
    bank = _bank(cfg)
    print(f"[info] voice bank: {len(bank.clips)} clip(s) in {bank.dir}"
          + (f", {len(bank.problems)} skipped (see `voices bank`)" if bank.problems else ""))
    print(f"[info] ffmpeg: {shutil.which('ffmpeg') or 'not found'} (only needed for models that return MP3/FLAC)")


LIVE_LINE = "Welcome, adventurer. Mind the stairs."
LIVE_SPEAKER = Speaker(name="Kotodama Doctor", race=1)       # a Hume man, cast by each provider's own rules
LIVE_WAIT = 180.0                                          # a cold Replicate model can take a while


def _doctor_live(cfg: Config, check, only: str | None = None) -> None:
    """Synthesize one short line with every provider that has its key (or
    only the one named), all at once, and report each one's time to first
    audio. Nothing is played or cached."""
    adapters = load_adapters(cfg)
    catalog = provider_catalog(cfg, adapters)
    if only:
        if only not in catalog:
            check("live test", False, f"unknown provider '{only}' (choose: {', '.join(catalog)})")
            return
        names = [only]
    else:
        names = [n for n, (_, has_key) in catalog.items() if has_key]
        skipped = [n for n, (_, has_key) in catalog.items() if not has_key]
        if skipped:
            print(f"[info] live test skips providers without a key: {', '.join(skipped)}")
        if not names:
            print("[info] live test: no provider has a key")
            return
    arch = Archetypes.load()
    lib = Library(arch, cfg.path("library.file"), cfg.path("library.packs_dir"), cfg.path("library.overrides"))
    bank = _bank(cfg)
    # Resolved up front, one thread: the library isn't shared across threads.
    profiles = {n: ProfileResolver(arch, lib, provider=n).resolve(LIVE_SPEAKER, None).profile for n in names}
    results: queue.Queue = queue.Queue()

    def run(name: str) -> None:
        prov = None
        try:
            prov = make_provider(cfg, name, adapters=adapters, bank=bank)
            if getattr(prov, "needs_key", True) and not getattr(prov, "api_key", None):
                hint = getattr(prov, "key_hint", None) or PROVIDER_KEYS.get(name, "see README")
                raise TTSError(f"no API key ({hint})", code="auth")
            req = prov.voice_request(profiles[name], "@doctor")          # may fetch a voice list: not timed
            started, first, total = time.monotonic(), None, 0
            for chunk in prov.stream(LIVE_LINE, req):
                first = first if first is not None else (time.monotonic() - started) * 1000
                total += len(chunk)
            if not total:
                raise TTSError("no audio came back")
            done = (time.monotonic() - started) * 1000
            note = f", fell back from {prov.configured_model}" if getattr(prov, "fell_back", False) else ""
            results.put((name, True, first, f"{prov.model}{note}, voice {req.label or req.voice or 'default'}: "
                         f"first audio {first:.0f} ms, {total / 2 / prov.sample_rate:.1f} s of audio "
                         f"in {done:.0f} ms"))
        except Exception as exc:  # noqa: BLE001 - every provider gets reported, whatever went wrong
            results.put((name, False, None, str(exc) or type(exc).__name__))
        finally:
            if prov is not None:
                prov.close()

    print(f"[info] live test: \"{LIVE_LINE}\" with {', '.join(names)} "
          f"(uses a little credit; a cold Replicate model can take a minute)")
    for name in names:
        threading.Thread(target=run, args=(name,), daemon=True, name=f"live-{name}").start()
    deadline = time.monotonic() + LIVE_WAIT
    pending, timings = set(names), []
    while pending:
        try:
            name, good, first, detail = results.get(timeout=max(0.05, deadline - time.monotonic()))
        except queue.Empty:
            break
        pending.discard(name)
        check(f"{name} synthesis", good, detail)
        if good:
            timings.append((first, name))
    for name in sorted(pending):
        check(f"{name} synthesis", False, f"no audio after {LIVE_WAIT:.0f} s")
    if len(timings) > 1:
        print("[info] fastest first audio: " + ", ".join(f"{n} {t:.0f} ms" for t, n in sorted(timings)))


def cmd_doctor(cfg: Config, args: argparse.Namespace) -> int:
    ok = True

    def check(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        print(f"[{'ok' if good else 'FAIL'}] {label}{(': ' + detail) if detail else ''}")

    check("python", sys.version_info >= (3, 10), sys.version.split()[0])
    env_path = Path(args.env_file).expanduser() if args.env_file else envfile.default_path()
    from_file = [n for n in os.environ.get(envfile.MARKER, "").split(",") if n]
    if from_file:
        state = "supplied " + ", ".join(from_file)
    elif env_path.exists():
        state = "found; everything in it is already set in your environment"
    else:
        state = "not found (keys must come from your environment)"
    print(f"[info] keys file {env_path}: {state}")
    engine = build_engine(cfg, null_player=False)
    check("audio player", engine.player.available, engine.player.kind or "none found")
    active = engine.tts.name
    print(f"[info] voice provider: {active} (switch in game with //koto provider)")
    has_key = not getattr(engine.tts, "needs_key", True) or bool(getattr(engine.tts, "api_key", None))
    check(f"{active} API key", has_key, "set" if has_key else
          f"missing ({getattr(engine.tts, 'key_hint', None) or PROVIDER_KEYS.get(active, 'see README')})")
    notice = shutdown_notice(cfg.get("tts.base_url"))
    if notice and (active == "openai" or cfg.api_key("tts")):
        print(f"[warn] {notice}")
    live = args.live or bool(args.provider)
    _doctor_elevenlabs(cfg, live, check)
    _doctor_models(cfg, check, active)
    provider = cfg.get("inference.provider")
    print(f"[info] voice casting: {provider}")
    if provider == "anthropic":
        try:
            import anthropic  # noqa: F401
            check("anthropic SDK", True)
        except ImportError:
            check("anthropic SDK", False, "pip install anthropic")
    try:
        streams = parse_sink_inputs(run_pactl("list", "sink-inputs"))
        game = [st for st in streams if is_game_stream(st, list(cfg.get("duck.match", [])))]
        print(f"[{'ok' if game else 'info'}] ducking: "
              + (", ".join(f"#{st.id} {st.label}" for st in game) if game
                 else "no game audio stream right now (start the game; see `audio list`)"))
    except FileNotFoundError:
        print("[info] ducking: pactl not found, so game audio will not be lowered")
    except Exception as exc:  # noqa: BLE001
        print(f"[info] ducking: could not list streams ({exc})")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((cfg.get("listen.host"), int(cfg.get("listen.port"))))
        check("UDP port free", True, f"{cfg.get('listen.host')}:{cfg.get('listen.port')}")
    except OSError as exc:
        check("UDP port free", False, str(exc))
    finally:
        sock.close()
    for name in ("pw-play", "paplay", "ffplay"):
        print(f"[info] {name}: {shutil.which(name) or 'not found'}")
    if live:
        _doctor_live(cfg, check, args.provider)
    else:
        print("[info] `doctor --live` tries every provider that has a key; `--provider NAME` just one")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kotodama_server.py", description="Kotodama voiceover sidecar")
    parser.add_argument("-c", "--config", help="path to config.json (default: server/config.json)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--env-file", help="API keys file to load (default: ~/.config/kotodama.env, "
                                           "or $KOTODAMA_ENV_FILE); shell variables take precedence")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="run the sidecar (default)")

    t = sub.add_parser("test", help="voice one line without the game")
    t.add_argument("--name", help="speaker name (omit for narrator)")
    t.add_argument("--race", type=int, help="FFXI race id (1-8, 29-31)")
    t.add_argument("--creature", help="creature archetype key, e.g. moogle")
    t.add_argument("--kind", default="event", choices=["ambient", "event", "cutscene", "battle"])
    t.add_argument("--zone", type=int)
    t.add_argument("--zone-name")
    t.add_argument("--player", default="Adventurer", help="player name (scrubbed from shared files)")
    t.add_argument("--no-audio", action="store_true", help="synthesize and cache but do not play")
    t.add_argument("--provider", help="override tts.provider for this test (openai, elevenlabs, minimax, ...)")
    t.add_argument("text", nargs="+")

    lib = sub.add_parser("library", help="share NPC voice libraries")
    libsub = lib.add_subparsers(dest="action", required=True)
    imp = libsub.add_parser("import", help="install someone else's library as a read-only pack")
    imp.add_argument("file")
    exp = libsub.add_parser("export", help="write your learned/curated NPC profiles")
    exp.add_argument("-o", "--output")
    exp.add_argument("--source", action="append", help="only entries whose source starts with this (llm, curated, manual)")
    libsub.add_parser("list", help="list local library entries")

    c = sub.add_parser("cache", help="audio cache maintenance")
    csub = c.add_subparsers(dest="action", required=True)
    csub.add_parser("stats")
    pr = csub.add_parser("prune")
    pr.add_argument("--fraction", type=float, default=0.9, help="shrink to this fraction of max_mb")
    an = csub.add_parser("analyze", help="measure the loudness of cached lines for normalization")
    an.add_argument("--all", action="store_true", help="re-measure every line, not just unmeasured ones")

    au = sub.add_parser("audio", help="inspect and test game-audio ducking")
    ausub = au.add_subparsers(dest="action", required=True)
    ausub.add_parser("list", help="show playback streams and which ones are ducked")
    ausub.add_parser("restore", help="put back game volume left lowered by a crashed run")
    ausub.add_parser("duck-test", help="duck the game audio for 3 seconds, then restore it")

    prov_opt = argparse.ArgumentParser(add_help=False)
    prov_opt.add_argument("--provider", help="elevenlabs (default) or a model adapter such as minimax, kokoro")
    vo = sub.add_parser("voices", help="voices to cast from: sync, list, preview casting, voice bank")
    vosub = vo.add_subparsers(dest="action", required=True)
    vosub.add_parser("sync", parents=[prov_opt], help="refresh the voice list (ElevenLabs account, model schema, bank)")
    vosub.add_parser("list", parents=[prov_opt], help="list voices with their labels")
    vc = vosub.add_parser("cast", parents=[prov_opt], help="show which voice each archetype (or one NPC) gets")
    vc.add_argument("--npc", help="an NPC name (uses your library/overrides)")
    vc.add_argument("--race", type=int)
    vc.add_argument("--creature")
    vosub.add_parser("bank", help="list the reference clips in the voice bank (for cloning models)")
    vd = vosub.add_parser("design", help="generate labelled reference clips per race/creature (Replicate)")
    vd.add_argument("--variants", type=int, default=3, help="clips per archetype (default 3)")
    vd.add_argument("--only", help="comma-separated archetypes, e.g. galka,taru_m")
    vd.add_argument("--yes", action="store_true", help="don't ask before spending")

    sub.add_parser("providers", help="list every voice provider and whether its key is set")
    rp = sub.add_parser("replicate", help="Replicate model tools")
    rpsub = rp.add_subparsers(dest="action", required=True)
    rs = rpsub.add_parser("schema", help="show a model's inputs and draft an adapter for it")
    rs.add_argument("model", help="owner/name, e.g. resemble-ai/chatterbox-turbo")

    dr = sub.add_parser("doctor", help="check the setup")
    dr.add_argument("--live", action="store_true",
                    help="also synthesize one short line with every provider that has a key and report its "
                         "time to first audio (uses a little credit)")
    dr.add_argument("--provider", help="live-test only this provider (implies --live)")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    # Before the config: the env file may hold API keys and KOTODAMA_CONFIG.
    env_path, env_names = envfile.load(Path(args.env_file).expanduser() if args.env_file else None)
    if env_names:
        level = logging.INFO if args.command in (None, "serve") else logging.DEBUG
        logging.log(level, "loaded %s from %s", ", ".join(env_names), env_path)
    cfg = Config.load(args.config)

    handlers = {None: cmd_serve, "serve": cmd_serve, "test": cmd_test, "library": cmd_library,
                "cache": cmd_cache, "audio": cmd_audio, "voices": cmd_voices, "doctor": cmd_doctor,
                "providers": cmd_providers, "replicate": cmd_replicate}
    return handlers[args.command](cfg, args)
