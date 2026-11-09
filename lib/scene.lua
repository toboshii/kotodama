-- Event / cutscene tracking.
--
-- FFXI has no "this is a cutscene" flag. We infer it from packet flow and
-- from what the dialog itself does:
--
--   incoming 0x032/0x033/0x034  an NPC event starts
--   incoming 0x052              the event is released
--   incoming 0x036              an NPC "barks" a one-liner (shop greetings etc.)
--   incoming 0x00B              zone out, everything ends
--   outgoing 0x01A (category 0) the player talked to an NPC
--   outgoing 0x036              the player traded items to an NPC
--
-- Inside an event it is a cutscene if the player didn't start it, a second
-- speaker joins, the speaker isn't the NPC we talked to, the player's own
-- character speaks, or lines auto-advance. Some scenes (zone-in cutscenes)
-- are never announced by an event packet at all; speech that arrives with no
-- event and no NPC-chat bark in front of it must be one of those.
--
-- Events recognised by a solid signal are remembered in library/cutscenes.json
-- (lib/cutscenes.lua), so the next viewing is a cutscene from its first line.
--
-- Every decision carries a reason string that the debug log prints, so these
-- rules can be tuned from real play (//koto debug on).

local cutscenes = require('lib/cutscenes')

local scene = {}

local INITIATE_WINDOW = 4.0   -- player's talk/trade -> event packet
local DUPLICATE_WINDOW = 3.0  -- the client often sends the same begin packet twice
local BARK_WINDOW = 2.0       -- 0x036 NPC chat -> its text line
local LATE_WINDOW = 3.0       -- event release -> a straggling line from that event

-- Promotions worth remembering. 'timed-line' and 'not-player-initiated' are
-- weaker (menu NPCs and misread talk packets can trigger them), so they
-- classify the current event but never write to the registry.
local PERSIST = {
    ['multi-speaker'] = true,
    ['speaker-differs'] = true,
    ['player-speaks'] = true,
    ['cutscene-emote'] = true,
    ['manual'] = true,
}

local ev = nil             -- current event, or nil
local last_event = nil     -- the event that just ended
local last_initiate = nil
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

-- Outgoing packets ---------------------------------------------------------

function scene.on_outgoing(id, data)
    if id == 0x01A then
        local target, category = u32(data, 4), u16(data, 0x0A)
        if target and category == 0 then
            last_initiate = { id = target, t = now(), via = 'talk' }
        end
    elseif id == 0x036 then
        local target = u32(data, 4)
        if target then
            last_initiate = { id = target, t = now(), via = 'trade' }
        end
    end
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
    local initiated = last_initiate ~= nil
        and last_initiate.id == npc_id
        and (t - last_initiate.t) <= INITIATE_WINDOW

    ev = {
        npc_id = npc_id,
        npc_name = mob and mob.name or nil,
        packet = packet_id,
        menu = menu_id,
        zone = zone,
        started = t,
        last_begin = t,
        begins = 1,
        initiated = initiated,
        cutscene = false,
        reason = nil,
        speakers = {},
        n_speakers = 0,
        lines = 0,
    }

    local known = cutscenes.known(zone, menu_id)
    if known then
        ev.cutscene, ev.reason = true, 'known-event'
    elseif not initiated then
        ev.cutscene, ev.reason = true, 'not-player-initiated'
    end
    return ev, false
end

function scene.finish()
    local old = ev
    if old then
        last_event = {
            npc_name = old.npc_name, zone = old.zone, menu = old.menu,
            cutscene = old.cutscene, reason = old.reason, t = now(),
        }
    end
    ev = nil
    return old
end

-- Zone changes end everything, including the memory of the last event.
function scene.reset()
    ev, last_event, last_bark, last_initiate = nil, nil, nil, nil
end

local function promote(reason)
    if not ev or ev.cutscene then return false end
    ev.cutscene = true
    ev.reason = reason
    if PERSIST[reason] and ev.zone and ev.menu then
        ev.learned = cutscenes.learn(ev.zone, ev.menu, ev.npc_name, reason, 'learned')
    end
    return true
end

-- Classify a line.
--   speaker  = display name or nil (narration)
--   info     = { is_player = bool }
--   timed    = line auto-advances (mode 144 or auto-prompt marker)
-- Returns kind ('ambient' | 'event' | 'cutscene' | 'battle') and a reason
-- when something noteworthy decided it (a promotion, a bark, a late line...).
function scene.classify(mode, speaker, info, timed)
    if mode == 142 then
        return 'battle'
    end

    local t = now()

    if ev then
        ev.lines = ev.lines + 1
        local promoted
        if speaker then
            local key = speaker:lower()
            if not ev.speakers[key] then
                ev.speakers[key] = true
                ev.n_speakers = ev.n_speakers + 1
            end
            if ev.n_speakers >= 2 and promote('multi-speaker') then
                promoted = 'multi-speaker'
            elseif ev.npc_name and key ~= ev.npc_name:lower()
                and not (info and info.is_player) and promote('speaker-differs') then
                promoted = 'speaker-differs'
            end
            if info and info.is_player and promote('player-speaks') then
                promoted = 'player-speaks'
            end
        end
        if (timed or mode == 15) and promote(mode == 15 and 'cutscene-emote' or 'timed-line') then
            promoted = mode == 15 and 'cutscene-emote' or 'timed-line'
        end
        -- Say why a cutscene was known from the start (once, on its first line).
        if not promoted and ev.lines == 1 and ev.cutscene then
            promoted = ev.reason
        end
        return ev.cutscene and 'cutscene' or 'event', promoted
    end

    -- No tracked event from here on.

    -- A one-liner announced by an NPC-chat packet: shop greetings, town criers.
    -- These are timed, but they are not cutscenes.
    if last_bark and (t - last_bark.t) <= BARK_WINDOW then
        return 'ambient', 'npc-chat'
    end

    -- A line that straggles in just after its event was released.
    if last_event and (t - last_event.t) <= LATE_WINDOW
        and (not speaker or (last_event.npc_name and speaker:lower() == last_event.npc_name:lower())) then
        return last_event.cutscene and 'cutscene' or 'event', 'late-line'
    end

    if mode == 15 then
        return 'cutscene', 'cutscene-emote'
    end

    -- Speech with no event, no bark and no event just ended: a scene the
    -- server drove without an event packet, such as a zone-in cutscene.
    if mode == 150 or mode == 144 then
        return 'cutscene', 'untracked-event'
    end

    return 'ambient', nil
end

-- Manual correction (//koto cutscene). Marks the active event, or the one
-- that just ended, and remembers it. Returns the event description or nil.
function scene.mark_cutscene()
    local target = ev or last_event
    if not target then return nil, 'no event to mark (talk to the NPC first)' end
    if ev then
        ev.cutscene = true
        ev.reason = 'manual'
    end
    if not (target.zone and target.menu) then return nil, 'that event has no menu id' end
    local added = cutscenes.learn(target.zone, target.menu, target.npc_name, 'manual', 'manual')
    return target, added and 'saved' or 'already known'
end

function scene.forget_cutscene()
    local target = ev or last_event
    if not target or not (target.zone and target.menu) then return nil end
    if ev then ev.cutscene, ev.reason = false, nil end
    return target, cutscenes.forget(target.zone, target.menu)
end

return scene
