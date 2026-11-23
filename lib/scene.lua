-- Event tracking.
--
-- The game tells us when an NPC event starts and ends:
--
--   incoming 0x032/0x033/0x034  an NPC event starts
--   incoming 0x052              the event is released
--   incoming 0x036              an NPC "barks" a one-liner (shop greetings etc.)
--   incoming 0x00B              zone out, everything ends
--
-- A line is 'event' dialog if it arrives inside an event, straggles in just
-- after one, or is speech the server drove without an event packet (zone-in
-- scenes). It is 'ambient' only when an NPC-chat packet announced it, or when
-- it is not a speech line at all. So a line is only put in the smaller class
-- on a real packet, and a wrong guess makes Kotodama speak, not go quiet.
--
-- FFXI has no "this is a cutscene" flag, and Kotodama doesn't try to guess one.
--
-- Every decision outside an event carries a reason string that the debug log
-- prints (//koto debug on).

local scene = {}

local DUPLICATE_WINDOW = 3.0  -- the client often sends the same begin packet twice
local BARK_WINDOW = 2.0       -- 0x036 NPC chat -> its text line
local LATE_WINDOW = 3.0       -- event release -> a straggling line from that event

local ev = nil             -- current event, or nil
local last_event = nil     -- the event that just ended
local last_bark = nil

local function now() return os.clock() end

local function u32(data, off)
    local a, b, c, d = data:byte(off + 1, off + 4)
    if not d then return nil end
    return a + b * 256 + c * 65536 + d * 16777216
end
scene.u32 = u32

local function u16(data, off)
    local a, b = data:byte(off + 1, off + 2)
    if not b then return nil end
    return a + b * 256
end
scene.u16 = u16

function scene.current()
    return ev
end

function scene.previous()
    return last_event
end

-- Incoming packets ---------------------------------------------------------

-- An NPC one-liner is about to appear in the chat log.
function scene.on_npc_chat(actor_id)
    last_bark = { id = actor_id, t = now() }
end

-- Returns the event and whether this packet merely repeated a begin we had
-- already seen (it must not reset what the event has accumulated).
function scene.begin(npc_id, packet_id, menu_id, zone)
    local t = now()
    if ev and ev.npc_id == npc_id and ev.menu == menu_id and (t - ev.last_begin) <= DUPLICATE_WINDOW then
        ev.last_begin = t
        ev.begins = ev.begins + 1
        return ev, true
    end

    local mob = npc_id and windower.ffxi.get_mob_by_id(npc_id) or nil
    ev = {
        npc_id = npc_id,
        npc_name = mob and mob.name or nil,
        packet = packet_id,
        menu = menu_id,
        zone = zone,
        started = t,
        last_begin = t,
        begins = 1,
        lines = 0,
    }
    return ev, false
end

function scene.finish()
    local old = ev
    if old then
        last_event = { npc_name = old.npc_name, zone = old.zone, menu = old.menu, t = now() }
    end
    ev = nil
    return old
end

-- Zone changes end everything, including the memory of the last event.
function scene.reset()
    ev, last_event, last_bark = nil, nil, nil
end

-- Classify a line.
--   mode     = chat mode of the line
--   speaker  = display name or nil (narration)
-- Returns kind ('ambient' | 'event' | 'battle') and, for a line outside an
-- event, the reason that decided it.
function scene.classify(mode, speaker)
    if mode == 142 then
        return 'battle'
    end

    if ev then
        ev.lines = ev.lines + 1
        return 'event'
    end

    local t = now()

    -- A one-liner announced by an NPC-chat packet: shop greetings, town criers.
    if last_bark and (t - last_bark.t) <= BARK_WINDOW then
        return 'ambient', 'npc-chat'
    end

    -- A line that straggles in just after its event was released.
    if last_event and (t - last_event.t) <= LATE_WINDOW
        and (not speaker or (last_event.npc_name and speaker:lower() == last_event.npc_name:lower())) then
        return 'event', 'late-line'
    end

    -- Speech with no event, no bark and no event just ended: a scene the
    -- server drove without an event packet, such as a zone-in cutscene.
    if mode == 150 or mode == 144 or mode == 15 then
        return 'event', 'untracked-event'
    end

    return 'ambient', nil
end

return scene
