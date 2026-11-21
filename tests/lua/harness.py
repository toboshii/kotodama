"""Run the real addon under Lua 5.1 (via the optional `lupa` package) with a
stubbed Windower, a fake UDP socket and a controllable clock.

    python3 -m venv .venv-lua && .venv-lua/bin/pip install lupa
    .venv-lua/bin/python -m unittest discover -s tests/lua

Strings cross the Lua boundary as bytes, so FFXI's Shift-JIS control codes
survive untouched.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

try:
    from lupa.lua51 import LuaRuntime
except ImportError:      # pragma: no cover - lupa is optional
    LuaRuntime = None

ROOT = Path(__file__).resolve().parents[2]

# Mobs the stubbed game knows about: id -> (name, race, models).
DEFAULT_MOBS = {
    1: ("Adventurer", 4, [1]),
    100: ("Ayame", 2, [5]),
    200: ("Prishe", 4, [6]),
}

STUBS = r"""
_addon = {}
CHAT, SENT, HANDLERS, FAKE_QUEUE = {}, {}, {}, {}
CLOCK = 0
os.clock = function() return CLOCK end

local Set = {}
Set.__index = Set
function S(t) local s = setmetatable({items = {}}, Set); for _, v in ipairs(t or {}) do s.items[v] = true end; return s end
function Set:add(v) self.items[v] = true end
function Set:remove(v) self.items[v] = nil end
function Set:length() local n = 0 for _ in pairs(self.items) do n = n + 1 end return n end
function Set:it() local keys = {} for k in pairs(self.items) do keys[#keys+1] = k end
  table.sort(keys); local i = 0 return function() i = i + 1 return keys[i] end end
function class(x) return getmetatable(x) == Set and 'Set' or type(x) end

config = {
  load = function(defaults) local s = {} for k, v in pairs(defaults) do s[k] = v end s.deny = S{} return s end,
  save = function() end,
}
package.preload['luau'] = function() return true end
package.preload['resources'] = function()
  return { zones = {[230] = {en = "Southern San d'Oria"}, [235] = {en = 'Bastok Markets'}, [50] = {en = 'Aht Urhgan Whitegate'}},
           races = {[1] = {en = 'Hume M'}, [2] = {en = 'Hume F'}, [4] = {en = 'Elvaan F'}, [5] = {en = 'Tarutaru M'},
                    [6] = {en = 'Tarutaru F'}, [7] = {en = 'Mithra'}, [8] = {en = 'Galka'}},
           auto_translates = {[1286] = {id = 1286, en = 'Healing!'}, [2108] = {id = 2108, en = 'Healing'},
                              [257] = {id = 257, en = 'Nice to meet you.'}},
           items = {[12710] = {id = 12710, en = 'Bone Mittens', enl = 'pair of bone mittens'}} }
end
package.preload['socket'] = function()
  return { udp = function()
    return {
      settimeout = function() end,
      setpeername = function() return 1 end,
      send = function(_, data) SENT[#SENT+1] = data return #data end,
      receive = function() return table.remove(FAKE_QUEUE, 1) end,
      close = function() end,
    } end }
end

MOBS_BY_ID, MOBS_BY_NAME, TARGET, ZONE, PLAYER_STATUS = {}, {}, nil, 230, 0
TARGETS = {}          -- get_mob_by_target(key) for keys other than 't': bt, pet, ft, lastst
function add_mob(id, name, race, models, spawn_type, distance)
  local m = {id = id, name = name, race = race, models = models, spawn_type = spawn_type or 2,
             distance = distance and distance * distance or nil}     -- Windower reports it squared
  MOBS_BY_ID[id] = m; MOBS_BY_NAME[name] = m
end
function remove_mob(id)
  local m = MOBS_BY_ID[id]
  MOBS_BY_ID[id] = nil
  if m and MOBS_BY_NAME[m.name] == m then MOBS_BY_NAME[m.name] = nil end
end

windower = {
  addon_path = ADDON_PATH,
  create_dir = function(path) os.execute('mkdir -p "' .. path .. '"') end,
  to_shift_jis = function(s) return s end,
  from_shift_jis = PY_FROM_SJIS,
  add_to_chat = function(_, msg) CHAT[#CHAT+1] = msg end,
  register_event = function(...)
    local args = {...}
    local fn = table.remove(args)
    for _, name in ipairs(args) do HANDLERS[name] = fn end
  end,
  ffxi = {
    get_info = function() return {logged_in = true, zone = ZONE, chat_open = false} end,
    get_player = function() return {name = 'Adventurer', id = 1, status = PLAYER_STATUS} end,
    get_mob_by_id = function(id) return MOBS_BY_ID[id] end,
    get_mob_by_index = function(i) return MOBS_BY_ID[i] end,
    get_mob_by_name = function(n) return MOBS_BY_NAME[n] end,
    get_mob_array = function() return MOBS_BY_ID end,
    get_mob_by_target = function(key)
      if key == nil or key == 't' then return TARGET end
      return TARGETS[key]
    end,
  },
}
function function_schedule() end
"""


def _sjis_to_utf8(data: bytes) -> bytes:
    return data.decode("cp932", errors="replace").encode("utf-8")


class Addon:
    """One loaded copy of Kotodama.lua in its own Lua state and scratch directory."""

    def __init__(self, *, seed_cutscenes: bool = False, debug: bool = False, mobs=None):
        if LuaRuntime is None:
            raise RuntimeError("lupa is not installed")
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "data").mkdir()
        (self.dir / "library").mkdir()
        if seed_cutscenes:
            shutil.copy(ROOT / "library" / "cutscenes.json", self.dir / "library" / "cutscenes.json")

        self.lua = LuaRuntime(encoding=None, unpack_returned_tuples=True)
        self.lua.execute(f"package.path = '{ROOT}/?.lua;' .. package.path".encode())
        g = self.lua.globals()
        g.ADDON_PATH = (str(self.dir) + "/").encode()
        g.PY_FROM_SJIS = _sjis_to_utf8
        self.lua.execute(STUBS.encode())
        for mob_id, (name, race, models) in (mobs or DEFAULT_MOBS).items():
            self.add_mob(mob_id, name, race, models)
        self.lua.execute(f"dofile('{ROOT}/Kotodama.lua')".encode())
        self.fire("load")
        if debug:
            self.command("debug", "on")

    # -- environment ------------------------------------------------------

    def close(self) -> None:
        self._tmp.cleanup()

    def add_mob(self, mob_id: int, name: str, race: int, models=(0,), spawn_type: int = 2,
                distance: float | None = None) -> None:
        self.lua.eval(b"add_mob")(mob_id, name.encode(), race, self.lua.table_from(list(models)), spawn_type,
                                  distance)

    def remove_mob(self, mob_id: int) -> None:
        self.lua.eval(b"remove_mob")(mob_id)

    def clock(self, seconds: float) -> None:
        self.lua.globals().CLOCK = seconds

    def advance(self, seconds: float) -> None:
        self.clock(self.lua.globals().CLOCK + seconds)

    def set_zone(self, zone: int) -> None:
        self.lua.globals().ZONE = zone

    def set_status(self, status: int) -> None:
        self.lua.globals().PLAYER_STATUS = status

    def set_target(self, mob_id: int | None) -> None:
        self.lua.globals().TARGET = None if mob_id is None else self.lua.globals().MOBS_BY_ID[mob_id]

    def set_target_of(self, key: str, mob_id: int | None) -> None:
        """What get_mob_by_target(key) returns, for keys other than 't' (bt, pet, ft, lastst)."""
        g = self.lua.globals()
        g.TARGETS[key.encode()] = None if mob_id is None else g.MOBS_BY_ID[mob_id]

    # -- events -----------------------------------------------------------

    def fire(self, name: str, *args):
        return self.lua.globals().HANDLERS[name.encode()](*args)

    def line(self, raw: bytes | str, mode: int = 150) -> None:
        raw = raw.encode() if isinstance(raw, str) else raw
        self.fire("incoming text", raw, raw, mode)

    def incoming(self, packet_id: int, data: bytes) -> None:
        self.fire("incoming chunk", packet_id, data)

    def outgoing(self, packet_id: int, data: bytes) -> None:
        self.fire("outgoing chunk", packet_id, data)

    def command(self, *args: str) -> None:
        self.fire("addon command", *[a.encode() for a in args])

    def prerender(self) -> None:
        self.fire("prerender")

    def queue_from_sidecar(self, msg: dict) -> None:
        self.lua.globals().FAKE_QUEUE[len(self.lua.globals().FAKE_QUEUE) + 1] = json.dumps(msg).encode()

    # -- packets ----------------------------------------------------------

    @staticmethod
    def u32(n: int) -> bytes:
        return n.to_bytes(4, "little")

    def event_begin(self, npc_id: int, menu: int, packet: int = 0x032) -> None:
        body = bytearray(0x30)
        body[0] = packet & 0xFF
        body[4:8] = self.u32(npc_id)
        menu_off = 0x2C if packet == 0x034 else 0x0C
        body[menu_off:menu_off + 2] = menu.to_bytes(2, "little")
        self.incoming(packet, bytes(body))

    def release(self, kind: int = 1) -> None:
        self.incoming(0x052, bytes([0x52, 0, 0, 0, kind, 0, 0, 0]))

    def npc_chat(self, actor_id: int, message_id: int = 7000) -> None:
        body = bytearray(0x10)
        body[0] = 0x36
        body[4:8] = self.u32(actor_id)
        body[0x0A:0x0C] = message_id.to_bytes(2, "little")
        self.incoming(0x036, bytes(body))

    def zone_out(self) -> None:
        self.incoming(0x00B, bytes([0x0B, 0, 0, 0]) + bytes(12))

    def talk_to(self, npc_id: int) -> None:
        body = bytearray(0x1C)
        body[0] = 0x1A
        body[4:8] = self.u32(npc_id)
        self.outgoing(0x01A, bytes(body))

    def event_option(self, option: int = 0, automated: bool = False) -> None:
        body = bytearray(0x14)
        body[0] = 0x5B
        body[8:10] = option.to_bytes(2, "little")
        body[0x0E] = 1 if automated else 0
        self.outgoing(0x05B, bytes(body))

    # -- results ----------------------------------------------------------

    def sent(self, kind: str | None = None) -> list[dict]:
        out = []
        sent = self.lua.globals().SENT
        for i in range(1, len(sent) + 1):
            msg = json.loads(sent[i].decode("utf-8"))
            if kind is None or msg["t"] == kind:
                out.append(msg)
        return out

    def clear_sent(self) -> None:
        self.lua.execute(b"SENT = {}")

    def speaks(self) -> list[dict]:
        return self.sent("speak")

    def chat(self) -> list[str]:
        chat = self.lua.globals().CHAT
        return [chat[i].decode("utf-8", "replace") for i in range(1, len(chat) + 1)]

    def debug_log(self) -> str:
        path = self.dir / "data" / "debug.log"
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""

    def registry_file(self) -> dict | None:
        path = self.dir / "library" / "cutscenes.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
