"""Command line: serve, test, library, cache, audio, voices, doctor."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import signal
import socket
import sys
import time
from pathlib import Path

from . import __version__
from .config import Config
from .audio import is_game_stream, parse_sink_inputs, run_pactl
from .archetypes import Archetypes
from .engine import PROVIDER_KEYS, build_engine, make_ducker
from .library import Library
from .net import UDPServer
from .profiles import ProfileResolver
from .protocol import Speaker, parse_speak
from .tts_base import TTSError
from .tts_elevenlabs import ElevenLabsTTS
from .voices_elevenlabs import cast, rank


def _setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_serve(cfg: Config, args: argparse.Namespace) -> int:
    engine = build_engine(cfg)
    if not engine.player.available:
        logging.error("no audio player available; install pipewire (pw-play), pulseaudio-utils or ffmpeg")
    if not engine.tts.api_key:
        logging.warning("no %s API key: only already-cached lines will play (set %s)",
                        engine.tts.name, PROVIDER_KEYS.get(engine.tts.name, "the provider key"))
    server = UDPServer(cfg, engine)
    signal.signal(signal.SIGTERM, lambda *_: server.shutdown())
    logging.info("Kotodama sidecar %s ready (player: %s, casting: %s)", __version__, engine.player.kind,
                 engine.inference.inferer.model if engine.inference.enabled else "off")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        engine.close()
    return 0


def cmd_test(cfg: Config, args: argparse.Namespace) -> int:
    def show(msg: dict) -> None:
        if msg.get("t") == "log":
            print(f"[addon] {msg['msg']}")

    engine = build_engine(cfg, reply=show, null_player=args.no_audio, provider=args.provider)
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


def cmd_voices(cfg: Config, args: argparse.Namespace) -> int:
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

    if args.action == "list":
        for v in voices:
            print(f"{v.id:<22} {v.summary()}")
        print(f"-- {len(voices)} voices")
        return 0

    # cast: show what auto-casting picks, without touching the casting memory
    arch = Archetypes.load()
    if args.npc:
        lib = Library(arch, cfg.path("library.file"), cfg.path("library.packs_dir"), cfg.path("library.overrides"))
        resolved = ProfileResolver(arch, lib, provider="elevenlabs").resolve(
            Speaker(name=args.npc, race=args.race, creature=args.creature), None)
        targets = [(args.npc, resolved.key, resolved.profile)]
    else:
        targets = [(key, key, {f: a.get(f) for f in ("gender", "age", "species", "personality", "tone",
                                                     "accent", "pitch", "pace")})
                   for key, a in arch.items.items()]
        targets = [(label, "@narrator" if label == "narrator" else ident, p) for label, ident, p in targets]
    for label, ident, profile in targets:
        chosen = el.catalog.get(cast(el.catalog, profile, ident, memory=None))
        runners = ", ".join(f"{v.name} ({s:g})" for s, v in rank(el.catalog, profile, ident)[:3])
        print(f"{label:<16} -> {chosen.name if chosen else 'NO MATCH':<18} top: {runners}")
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
    if not live:
        print(f"[info] ElevenLabs model {el.configured_model}: run `doctor --live` to try it (uses a few credits)")
        return
    from .tts_base import VoiceRequest
    req = el.voice_request({"voice": el.catalog.voices[0].id, "pace": "normal"}, "@doctor")
    started, first, total = time.monotonic(), None, 0
    try:
        for chunk in el.stream("Hello.", req):
            first = first if first is not None else (time.monotonic() - started) * 1000
            total += len(chunk)
        note = f" (fell back from {el.configured_model})" if el.fell_back else ""
        check("ElevenLabs synthesis", total > 0,
              f"{el.model}{note}: first audio {first:.0f} ms, {total / 2 / el.sample_rate:.2f} s of audio")
    except TTSError as exc:
        check("ElevenLabs synthesis", False, str(exc))
    finally:
        el.close()


def cmd_doctor(cfg: Config, args: argparse.Namespace) -> int:
    ok = True

    def check(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        print(f"[{'ok' if good else 'FAIL'}] {label}{(': ' + detail) if detail else ''}")

    check("python", sys.version_info >= (3, 10), sys.version.split()[0])
    engine = build_engine(cfg, null_player=False)
    check("audio player", engine.player.available, engine.player.kind or "none found")
    active = engine.tts.name
    print(f"[info] voice provider: {active} (switch in game with //koto provider)")
    openai_key = bool(cfg.api_key("tts"))
    check("OpenAI API key", openai_key or active != "openai",
          "set" if openai_key else "missing (OPENAI_API_KEY / tts.api_key)")
    _doctor_elevenlabs(cfg, args.live, check)
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
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kotodama_server.py", description="Kotodama voiceover sidecar")
    parser.add_argument("-c", "--config", help="path to config.json (default: server/config.json)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
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
    t.add_argument("--provider", choices=["openai", "elevenlabs"], help="override tts.provider for this test")
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

    au = sub.add_parser("audio", help="inspect and test game-audio ducking")
    ausub = au.add_subparsers(dest="action", required=True)
    ausub.add_parser("list", help="show playback streams and which ones are ducked")
    ausub.add_parser("restore", help="put back game volume left lowered by a crashed run")
    ausub.add_parser("duck-test", help="duck the game audio for 3 seconds, then restore it")

    vo = sub.add_parser("voices", help="ElevenLabs voices: sync, list, and preview auto-casting")
    vosub = vo.add_subparsers(dest="action", required=True)
    vosub.add_parser("sync", help="fetch the voices your ElevenLabs account can use")
    vosub.add_parser("list", help="list cached voices with their labels")
    vc = vosub.add_parser("cast", help="show which voice each archetype (or one NPC) gets")
    vc.add_argument("--npc", help="an NPC name (uses your library/overrides)")
    vc.add_argument("--race", type=int)
    vc.add_argument("--creature")

    dr = sub.add_parser("doctor", help="check the setup")
    dr.add_argument("--live", action="store_true", help="also synthesize one word with ElevenLabs (uses credits)")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    cfg = Config.load(args.config)

    handlers = {None: cmd_serve, "serve": cmd_serve, "test": cmd_test, "library": cmd_library,
                "cache": cmd_cache, "audio": cmd_audio, "voices": cmd_voices, "doctor": cmd_doctor}
    return handlers[args.command](cfg, args)
