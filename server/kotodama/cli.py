"""Command line: serve, test, library, cache, doctor."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import signal
import socket
import sys
from pathlib import Path

from . import __version__
from .config import Config
from .audio import is_game_stream, parse_sink_inputs, run_pactl
from .engine import build_engine, make_ducker
from .net import UDPServer
from .protocol import parse_speak


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
        logging.warning("no OpenAI API key: only already-cached lines will play "
                        "(set OPENAI_API_KEY or tts.api_key in server/config.json)")
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

    engine = build_engine(cfg, reply=show, null_player=args.no_audio)
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
    print(f"source={job.source} first-audio={'%.0f ms' % job.ttfb_ms if job.ttfb_ms else 'n/a'}")
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


def cmd_doctor(cfg: Config, args: argparse.Namespace) -> int:
    ok = True

    def check(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        print(f"[{'ok' if good else 'FAIL'}] {label}{(': ' + detail) if detail else ''}")

    check("python", sys.version_info >= (3, 10), sys.version.split()[0])
    engine = build_engine(cfg, null_player=False)
    check("audio player", engine.player.available, engine.player.kind or "none found")
    check("OpenAI API key", bool(engine.tts.api_key),
          "set" if engine.tts.api_key else "missing (OPENAI_API_KEY / tts.api_key)")
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

    sub.add_parser("doctor", help="check the setup")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    cfg = Config.load(args.config)

    handlers = {None: cmd_serve, "serve": cmd_serve, "test": cmd_test, "library": cmd_library,
                "cache": cmd_cache, "audio": cmd_audio, "doctor": cmd_doctor}
    return handlers[args.command](cfg, args)
