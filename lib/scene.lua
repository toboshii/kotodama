-- Event / cutscene tracking.
--
-- FFXI has no "this is a cutscene" flag. We approximate it from packet flow:
--   incoming 0x032/0x033/0x034  an NPC event starts
--   incoming 0x052              the event is released
--   incoming 0x00B              zone out, everything ends
--   outgoing 0x01A (category 0) the player talked to an NPC
--   outgoing 0x036              the player traded items to an NPC
-- An event the player did not start (walk-in trigger, zone-in scene) is a
-- cutscene; so is one with several speakers, a speaking player, or
-- auto-advancing (timed) lines. Everything here is heuristic and every
-- decision is logged with its reason so it can be tuned from real play.

local scene = {}

local INITIATE_WINDOW = 4.0   -- seconds between player's talk/trade and the event packet

local ev = nil          -- current event, or nil
local last_initiate = nil

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

-- Returns the new event table.
function scene.begin(npc_id, packet_id, menu_id)
    local mob = npc_id and windower.ffxi.get_mob_by_id(npc_id) or nil
    local initiated = last_initiate ~= nil
        and last_initiate.id == npc_id
        and (now() - last_initiate.t) <= INITIATE_WINDOW

    ev = {
        npc_id = npc_id,
        npc_name = mob and mob.name or nil,
        packet = packet_id,
        menu = menu_id,
        started = now(),
        initiated = initiated,
        cutscene = not initiated,
        reason = (not initiated) and 'not-player-initiated' or nil,
        speakers = {},
        n_speakers = 0,
        lines = 0,
    }
    return ev
end

function scene.finish()
    local old = ev
    ev = nil
    return old
end

local function promote(reason)
    if ev and not ev.cutscene then
        ev.cutscene = true
        ev.reason = reason
        return true
    end
    return false
end

-- Feed every speakable line through here before classifying it.
--   speaker  = display name or nil (narration)
--   info     = { is_player = bool }
--   timed    = line auto-advances (mode 144 or auto-prompt marker)
-- Returns kind ('ambient' | 'event' | 'cutscene' | 'battle') and, if this
-- line promoted the event to a cutscene, the reason.
function scene.classify(mode, speaker, info, timed)
    if mode == 142 then
        return 'battle'
    end

    local promoted
    if ev then
        ev.lines = ev.lines + 1
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
        return ev.cutscene and 'cutscene' or 'event', promoted
    end

    if timed or mode == 15 then
        return 'cutscene', 'no-event-timed'
    end
    return 'ambient', nil
end

return scene
