#!/usr/bin/env python3
"""Replay a recorded data/debug.log through the current scene heuristics.

    .venv-lua/bin/python tests/lua/replay_log.py data/debug.log [--seed] [--all]

The log's packets, timings and raw text are fed to the real addon in a Lua
state, and each line's new classification is compared with the one recorded.
Use it after changing lib/scene.lua to see exactly which lines move.

  --seed   start with the shipped library/cutscenes.json (default: empty, so
           the heuristics are judged on their own)
  --all    list every line, not only the ones that changed
"""

from __future__ import annotations

import argparse
import collections
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Addon  # noqa: E402

TIME = re.compile(r"^(\d\d):(\d\d):(\d\d) (.*)$")


def seconds(h: str, m: str, s: str) -> int:
    return int(h) * 3600 + int(m) * 60 + int(s)


def zone_of(npc_id: int) -> int:
    return (npc_id - 0x1000000) >> 12      # NPC ids embed their zone


def parse(path: Path):
    """Yield (t, kind, data) for every replayable record in the log."""
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = TIME.match(raw)
        if not m:
            continue
        t, rest = seconds(*m.groups()[:3]), m.group(4)
        if rest.startswith("outgoing 0x01A"):
            mm = re.search(r"target=(\d+) category=(\d+)", rest)
            yield t, "talk", (int(mm.group(1)), int(mm.group(2)))
        elif rest.startswith("outgoing 0x05B"):
            mm = re.search(r"option=(\d+) automated=(\w+)", rest)
            yield t, "option", (int(mm.group(1)), mm.group(2) == "true")
        elif rest.startswith("event begin") and "(repeat)" not in rest:
            mm = re.search(r"packet=(0x\w+) npc=(\d+) name=(.*?) (?:zone=\d+ )?menu=(\d+)", rest)
            yield t, "begin", (int(mm.group(1), 16), int(mm.group(2)), mm.group(3), int(mm.group(4)))
        elif rest.startswith("event release"):
            mm = re.search(r"type=(\d+)", rest)
            yield t, "release", int(mm.group(1))
        elif rest.startswith("npc chat"):
            yield t, "bark", int(re.search(r"actor=(\d+)", rest).group(1))
        elif rest.startswith("zone out"):
            yield t, "zone", None
        elif rest.startswith("line "):
            # status= is absent from logs written before it was added.
            mm = re.search(r"mode=(\d+) kind=(\w+) timed=(\w+) speaker=(.*?) race=(\w+) model=(\w+)"
                           r"(?: status=\w+)? text=\[.*\] raw=\[([0-9A-F ]*)\]\s*$", rest)
            if mm:
                yield t, "line", dict(mode=int(mm.group(1)), old=mm.group(2), speaker=mm.group(4),
                                      race=mm.group(5), model=mm.group(6),
                                      raw=bytes.fromhex(mm.group(7).replace(" ", "")))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", type=Path)
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    records = list(parse(args.log))

    # Teach the stub game who everyone is, from the log itself.
    ids, names = {}, {}
    for _, kind, d in records:
        if kind == "begin":
            ids[d[1]] = d[2]
        elif kind == "line" and d["speaker"] != "nil":
            names[d["speaker"]] = (None if d["race"] == "nil" else int(d["race"]),
                                   None if d["model"] == "nil" else int(d["model"]))

    addon = Addon(seed_cutscenes=args.seed, debug=True, mobs={})
    addon.command("narrator", "on")
    addon.command("battle", "on")
    for npc_id, name in ids.items():
        race, model = names.get(name, (0, 0))
        addon.add_mob(npc_id, name, race or 0, [model or 0])
    addon.clear_sent()

    old_kinds = []
    for t, kind, d in records:
        addon.clock(t)
        if kind == "talk":
            addon.talk_to(d[0]) if d[1] == 0 else None
        elif kind == "begin":
            addon.set_zone(zone_of(d[1]))
            addon.event_begin(d[1], d[3], packet=d[0])
        elif kind == "option":
            addon.event_option(*d)
        elif kind == "release":
            addon.release(d)
        elif kind == "bark":
            addon.npc_chat(d)
        elif kind == "zone":
            addon.zone_out()
        elif kind == "line":
            old_kinds.append((t, d))
            addon.line(d["raw"], d["mode"])

    # Pair each replayed line with the reason the new run logged for it.
    reasons, pending = [], None
    for entry in addon.debug_log().splitlines():
        body = entry.split(" ", 1)[1] if " " in entry else entry
        if body.startswith("scene: "):
            pending = body[len("scene: "):].split(" -> ")[0]
        elif body.startswith("line mode="):
            reasons.append(pending)
            pending = None
    speaks = addon.speaks()

    changed = collections.Counter()
    rows = []
    for i, (t, d) in enumerate(old_kinds):
        new = speaks[i]["kind"] if i < len(speaks) else "(not sent)"
        reason = reasons[i] if i < len(reasons) else None
        text = (speaks[i]["text"] if i < len(speaks) else "")[:58]
        who = d["speaker"] if d["speaker"] != "nil" else "(narration)"
        rows.append((t, who, d["old"], new, reason, text))
        if d["old"] != new:
            changed[(d["old"], new, reason)] += 1

    print(f"{len(records)} records replayed, {len(old_kinds)} spoken lines, "
          f"registry: {'shipped seed' if args.seed else 'empty'}\n")
    shown = [r for r in rows if args.all or r[2] != r[3]]
    for t, who, old, new, reason, text in shown:
        hh, mm, ss = t // 3600, t // 60 % 60, t % 60
        arrow = "   " if old == new else "-> "
        print(f"{hh:02d}:{mm:02d}:{ss:02d}  {who[:18]:<18} {old:<9}{arrow}{new:<9} {reason or '':<16} {text}")
    print("\nsummary of changes (old -> new, reason):")
    if not changed:
        print("  none")
    for (old, new, reason), n in changed.most_common():
        print(f"  {n:>3} x  {old} -> {new}   [{reason}]")
    still = collections.Counter(r[3] for r in rows)
    print("\nnew classification totals:", dict(still))
    return 0


if __name__ == "__main__":
    sys.exit(main())
