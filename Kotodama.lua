-- Kotodama: AI voiceover for FFXI NPC dialog and cutscenes.
--
-- This addon only *listens*: it captures dialog lines, decides whether they
-- should be voiced, and hands them to the Kotodama sidecar
-- (server/kotodama_server.py) over a local UDP socket. The sidecar owns the
-- API keys, the voice cache, the NPC voice library and audio playback.
--
-- Dialog text handling is derived from the Balloon addon
-- (Copyright 2018 Hando, 2021 Yuki, 2022 Ghosty; BSD-3-Clause).

_addon.name = 'Kotodama'
_addon.author = 'Kotodama'
_addon.version = '0.1.0'
_addon.commands = { 'kotodama', 'koto' }

require('luau')

local dialog   = require('lib/dialog')
local bridge   = require('lib/bridge')
local scene    = require('lib/scene')
local cutscenes = require('lib/cutscenes')
local speakers = require('lib/speakers')
local voicelines = require('lib/voicelines')

local ok_res, res = pcall(require, 'resources')
local zones = ok_res and res and res.zones or {}

local defaults = {
    enabled   = true,
    mode      = 'all',     -- all | events | cutscenes
    battle    = false,     -- voice battle chatter (Trusts etc.)
    own_voice = true,      -- voice your own character's lines
    narrator  = false,     -- voice lines that have no speaker name
    interrupt = true,      -- a new line cuts off the previous one
    prefetch  = true,      -- tell the sidecar about NPCs you target / talk to
    volume    = 100,       -- percent, 0-400 (100 = normal; raise if the voice is quiet)
    duck      = true,      -- lower the game's audio while a line is spoken
    duck_level = 40,       -- percent of the game's volume kept while speaking
    normalize = '',        -- even out loudness between voices: '' = server/config.json, or on | off
    provider  = '',        -- voice provider: '' = server/config.json, or openai | elevenlabs
    port      = 47123,
    debug     = false,
    deny      = S{},
}

local settings = {}
local state = {
    seq = 0,
    last_speaker = nil,
    last_line = nil,
    hinted = {},
    unnamed = {},          -- this zone visit's "???" speakers: identity -> { identity, scene, info }
    spoken = {},           -- names that have spoken this zone visit (lowercased)
    prev_line = nil,       -- { text, scene } of the line before
    loaded_at = os.clock(),
    warned_down = false,
    ever_up = false,
}

local MODES = { all = true, events = true, cutscenes = true }
-- Provider names are checked by the sidecar (openai, elevenlabs, and every
-- model adapter it knows: minimax, chatterbox, kokoro, ...); here only the shape.
local PROVIDER_NAME = '^[%l%d][%l%d_%-]*$'
local ONOFF = { on = true, off = false }

-- Output -------------------------------------------------------------------

local function say(msg)
    local ok = pcall(function()
        windower.add_to_chat(207, windower.to_shift_jis('Kotodama: ' .. msg))
    end)
    if not ok then windower.add_to_chat(207, 'Kotodama: ' .. tostring(msg)) end
end

local function debug_log(fmt, ...)
    if not settings.debug then return end
    local ok, line = pcall(string.format, fmt, ...)
    if not ok then line = tostring(fmt) end
    local f = io.open(windower.addon_path .. 'data/debug.log', 'a')
    if f then
        f:write(os.date('%H:%M:%S '), line, '\n')
        f:close()
    end
end

local function hex(s)
    return (s:gsub('.', function(c) return string.format('%02X ', c:byte()) end))
end

-- Settings -----------------------------------------------------------------

local function load_settings()
    windower.create_dir(windower.addon_path .. 'data')
    settings = config.load(defaults)
    if class(settings.deny) ~= 'Set' then
        settings.deny = S{}
    end
    if not MODES[settings.mode] then settings.mode = 'all' end
    config.save(settings)
end

local function save_settings()
    config.save(settings)
end

-- Sidecar link -------------------------------------------------------------

local function player_name()
    local p = windower.ffxi.get_player()
    return p and p.name or nil
end

local function zone_info()
    local info = windower.ffxi.get_info()
    local id = info and info.zone or nil
    local row = id and zones[id] or nil
    return id, row and row.en or nil
end

local function normalize_value()
    -- '' = whatever server/config.json says. (Not an and/or: false can't pass through one.)
    if settings.normalize == 'on' then return true end
    if settings.normalize == 'off' then return false end
    return ''
end

local function send_config()
    bridge.send({
        t = 'config',
        volume = settings.volume,
        provider = settings.provider or '',
        duck = settings.duck,
        duck_level = settings.duck_level,
        normalize = normalize_value(),
        interrupt = settings.interrupt,
        debug = settings.debug,
    })
end

local function send_hello()
    local zid, zname = zone_info()
    state.hello_at = os.clock()
    bridge.send({
        t = 'hello',
        version = _addon.version,
        player = player_name(),
        zone = zid,
        zone_name = zname,
    })
    send_config()
end

local handlers = {
    pong = function(msg)
        -- A sidecar that hasn't heard our hello was restarted faster than the
        -- link timeout: resend hello + config so it gets our settings back.
        -- (Skipped right after a hello, e.g. the one sent on connect.)
        if msg.want_hello and os.clock() - (state.hello_at or -10) > 2 then
            say('voice server restarted.')
            send_hello()
        end
    end,
    log = function(msg)
        if type(msg.msg) == 'string' then say(msg.msg) end
    end,
}

local function on_link_state(up)
    if up then
        state.ever_up = true
        state.warned_down = false
        say('connected to the voice server.')
        send_hello()
    elseif state.ever_up and not state.warned_down then
        state.warned_down = true
        say('lost the voice server. Dialog will stay silent until it is running again.')
    end
end

local function open_link()
    local ok, err = bridge.open(settings.port, handlers, on_link_state)
    if not ok then
        say('could not open the local socket (' .. tostring(err) .. '). Voiceover is disabled.')
    else
        bridge.send({ t = 'hello', version = _addon.version, player = player_name() })
    end
end

-- Filtering ----------------------------------------------------------------

local function denied(name)
    if not name then return false end
    local n = name:lower()
    for pattern in settings.deny:it() do
        local p = pattern:lower()
        if p == n then return true end
        if p:find('*', 1, true) then
            local lp = '^' .. p:gsub('[%^%$%(%)%%%.%[%]%+%-%?]', '%%%0'):gsub('%*', '.*') .. '$'
            if n:find(lp) then return true end
        end
    end
    return false
end

-- Returns nil to speak, or a short reason string for the debug log.
local function skip_reason(kind, name, info)
    if not settings.enabled then return 'disabled' end
    if kind == 'battle' and not settings.battle then return 'battle-off' end
    if info.is_player and not settings.own_voice then return 'own-voice-off' end
    if info.narrator and not settings.narrator then return 'narrator-off' end
    if denied(name) then return 'denied' end
    if settings.mode == 'cutscenes' and kind ~= 'cutscene' then return 'mode-cutscenes' end
    if settings.mode == 'events' and kind == 'ambient' then return 'mode-events' end
    return nil
end

-- Packet census (debug) -------------------------------------------------------
-- When something can't be classified from the logged fields, the packets that
-- flew by during the scene are the next place to look for a signal.

local census = { ['in'] = {}, out = {} }

local function census_note(dir, id)
    if settings.debug and not (dir == 'out' and id == 0x015) then   -- 0x015 is position spam
        census[dir][id] = (census[dir][id] or 0) + 1
    end
end

local function census_summary(counts)
    local ids = {}
    for id in pairs(counts) do ids[#ids + 1] = id end
    table.sort(ids)
    local parts = {}
    for _, id in ipairs(ids) do parts[#parts + 1] = string.format('0x%03X x%d', id, counts[id]) end
    return #parts > 0 and table.concat(parts, ' ') or '-'
end

local function census_flush(label)
    if settings.debug then
        debug_log('  packets during %s  in: %s | out: %s', label, census_summary(census['in']),
            census_summary(census.out))
    end
    census = { ['in'] = {}, out = {} }
end

local function player_status()
    local p = windower.ffxi.get_player()
    return p and p.status or nil
end

-- Dialog pipeline ----------------------------------------------------------

local function send_hint(name, info, reason)
    if not settings.prefetch or not name or denied(name) then return end
    local zid, zname = zone_info()
    local key = name:lower() .. '@' .. tostring(zid)
    if state.hinted[key] then return end
    state.hinted[key] = true
    bridge.send({
        t = 'hint', reason = reason, npc = name, zone = zid, zone_name = zname,
        race = info and info.race, race_name = info and info.race_name,
        model = info and info.model, creature = info and info.creature,
    })
end

-- "zone:event", or just the zone for a scene without an event packet.
local function scene_key(ev)
    local zone = (ev and ev.zone) or zone_info()
    if ev and ev.menu then
        return string.format('%s:%s', tostring(zone), tostring(ev.menu))
    end
    return tostring(zone)
end

-- Every character the game hasn't introduced yet is "???", so each one is
-- named after its scene instead: one voice for the whole cutscene (and its
-- replays), rather than one voice for every "???" in the game. Who they
-- are (race, look) comes from their actor, looked up once per scene so the
-- voice can't change mid-scene.
local function unnamed_speaker(shown, ev)
    local skey = scene_key(ev)
    local identity = string.format('%s (%s)', shown, skey)
    local rec = state.unnamed[identity]
    if not rec then
        local info, n = speakers.find_unnamed(shown, identity)
        if info then
            debug_log('  %s is actor id=%s race=%s look=%s', identity, tostring(info.id),
                tostring(info.race), tostring(info.look))
        elseif settings.debug then
            debug_log('  %s: %d "%s" actors with a body in range; nearby: %s', identity, n, shown,
                speakers.nearby(12))
        end
        rec = { identity = identity, scene = skey, info = info or { name = identity } }
        state.unnamed[identity] = rec
    end
    return identity, rec.info
end

local function mentions(text, name)
    if not text or not name then return false end
    local p = name:lower():gsub('[%^%$%(%)%%%.%[%]%*%+%-%?]', '%%%0')
    return text:lower():find('%f[%w]' .. p .. '%f[^%w]') ~= nil
end

-- A name speaking for the first time may be a "???" from earlier, now
-- introduced: the same actor, someone who looks exactly the same, or (when
-- either look is unknown) the scene's only "???" while this line or the one
-- before says the name. Returns the "???" identity and why.
local function was_unnamed(name, info, text, skey)
    local in_scene, only = 0, nil
    for _, rec in pairs(state.unnamed) do
        local u = rec.info
        if u.id and info.id and u.id == info.id then return rec.identity, 'same actor' end
        if u.look and info.look and u.look == info.look then return rec.identity, 'same look' end
        if rec.scene == skey then in_scene, only = in_scene + 1, rec end
    end
    if in_scene == 1 and not (only.info.look and info.look) then
        if mentions(text, name) then return only.identity, 'says their name' end
        local prev = state.prev_line
        if prev and prev.scene == skey and mentions(prev.text, name) then
            return only.identity, 'named just before'
        end
    end
    return nil
end

local function process_line(raw, mode, forced_kind)
    if not dialog.SPEECH_MODES[mode] then return end
    if dialog.is_blank_auto_prompt(raw) then
        debug_log('skip blank-auto-prompt mode=%d', mode)
        return
    end

    local timed = (mode == 144) or dialog.has_auto_prompt(raw)
    local cleaned = dialog.clean(raw)
    local name, text = dialog.split_speaker(cleaned)
    if not dialog.speakable(text) then
        debug_log('skip unspeakable mode=%d [%s]', mode, hex(raw))
        return
    end

    -- Drop immediate duplicates (the client echoes some lines).
    local dupe_key = (name or '') .. '|' .. text
    local t = os.clock()
    if state.last_line and state.last_line.key == dupe_key and (t - state.last_line.t) < 0.5 then
        return
    end
    state.last_line = { key = dupe_key, t = t }

    local ev = scene.current()
    local skey = scene_key(ev)
    local info, was, was_why
    if dialog.is_unnamed(name) then
        -- Never looked up by name: get_mob_by_name("???") could be a quest marker.
        name, info = unnamed_speaker(name, ev)
    else
        info = speakers.resolve(name, ev)
        if name and not info.is_player and not state.spoken[name:lower()] and next(state.unnamed) then
            was, was_why = was_unnamed(name, info, text, skey)
        end
    end
    if name then state.spoken[name:lower()] = true end
    state.prev_line = { text = text, scene = skey }
    local kind, reason = scene.classify(mode, name, info, timed)
    if forced_kind then kind = forced_kind end

    if reason then
        debug_log('scene: %s -> %s', reason, kind)
        if reason == 'untracked-event' then
            census_flush('untracked scene')
        end
    end
    -- status 4 = the client is in an event. Logged on every line to see whether
    -- it marks the scenes that send no event packet ('untracked-event').
    debug_log('line mode=%d kind=%s timed=%s speaker=%s race=%s model=%s status=%s text=[%s] raw=[%s]',
        mode, kind, tostring(timed), tostring(name), tostring(info.race), tostring(info.model),
        tostring(player_status()), text, hex(raw))
    if was then
        debug_log('  -> the same character as %s (%s)', was, was_why)
    end

    if name then
        state.last_speaker = name
    end

    local why = skip_reason(kind, name, info)
    if why then
        debug_log('  -> not voiced (%s)', why)
        return
    end

    state.seq = state.seq + 1
    local zid, zname = zone_info()
    bridge.send({
        t = 'speak',
        seq = state.seq,
        kind = kind,
        mode = mode,
        timed = timed,
        zone = zid,
        zone_name = zname,
        text = text,
        player = player_name(),
        speaker = {
            name = name,
            narrator = info.narrator or false,
            is_player = info.is_player or false,
            race = info.race,
            race_name = info.race_name,
            model = info.model,
            creature = info.creature,
            was = was,             -- the "???" this character was shown as: keep that voice
        },
    })
end

windower.register_event('incoming text', function(original, modified, mode)
    local ok, err = pcall(process_line, original, mode)
    if not ok then debug_log('process_line error: %s', tostring(err)) end
end)

-- Custom voice lines ---------------------------------------------------------

-- What a placeholder (<t>, <me>, ...) stands for right now, or nil.
local function placeholder_name(key)
    if key == 'me' then return player_name() end
    local mob = windower.ffxi.get_mob_by_target(key)
    return mob and mob.name or nil
end

-- The words of an auto-translate entry, or nil: a standard phrase
-- (resources.auto_translates), an item's short name (resources.items) or a
-- key item (resources.key_items).
local AT_TABLES = {
    [voicelines.AT_PHRASE] = 'auto_translates',
    [voicelines.AT_ITEM] = 'items',
    [voicelines.AT_KEY_ITEM] = 'key_items',
}

local function auto_translate_words(kind, id)
    local tbl = AT_TABLES[kind]
    if not tbl or not ok_res then
        debug_log('auto-translate type %s id %s not handled', tostring(kind), tostring(id))
        return nil
    end
    local ok, entry = pcall(function() return res[tbl][id] end)
    if not ok or type(entry) ~= 'table' then
        debug_log('auto-translate %s id %s not found', tbl, tostring(id))
        return nil
    end
    return entry.en
end

-- Speak text the player asked for (//koto say, //koto line) in the custom
-- voice: the narrator's until //koto voice @custom changes it. The dialog
-- filters (mode, narrator, deny, battle) don't apply; //koto off does.
local function speak_custom(text, label)
    label = label or 'say'
    if not settings.enabled then
        say(label .. ': voiceover is off (//koto on)')
        return false
    end
    -- Auto-translate phrases to their words, the same cleanup as an NPC line
    -- (markup, glyphs), then placeholders.
    debug_log('custom line (%s) raw=[%s]', label, hex(text))
    text = voicelines.auto_translate(text, auto_translate_words)
    local raw = text
    pcall(function() raw = windower.to_shift_jis(text) end)
    local cleaned, err = voicelines.expand(dialog.clean(raw), placeholder_name)
    if not cleaned then
        say(label .. ': ' .. err)
        return false
    end
    if not dialog.speakable(cleaned) then
        say(label .. ': nothing to say')
        return false
    end
    cleaned = cleaned:sub(1, voicelines.MAX_TEXT)

    state.seq = state.seq + 1
    local zid, zname = zone_info()
    debug_log('custom line (%s) text=[%s]', label, cleaned)
    -- narrator = true: an older sidecar that doesn't know the custom role
    -- still speaks it, as narration.
    bridge.send({
        t = 'speak',
        seq = state.seq,
        kind = 'custom',
        mode = 0,
        timed = false,
        zone = zid,
        zone_name = zname,
        text = cleaned,
        player = player_name(),
        speaker = { role = 'custom', narrator = true },
    })
    return true
end

-- Packets: scene tracking and interruption ----------------------------------

-- Zone changes end every scene, and with them who was who.
local function forget_zone()
    scene.reset()
    speakers.clear()
    state.hinted = {}
    state.unnamed, state.spoken, state.prev_line = {}, {}, nil
end

local function stop_audio(reason)
    if settings.interrupt then
        bridge.send({ t = 'stop', reason = reason })
    end
end

windower.register_event('incoming chunk', function(id, original)
    census_note('in', id)
    if id == 0x032 or id == 0x033 or id == 0x034 then
        local npc_id = scene.u32(original, 4)
        local menu_off = (id == 0x034) and 0x2C or 0x0C
        local menu = scene.u16(original, menu_off)
        local zid = zone_info()
        local ev, duplicate = scene.begin(npc_id, id, menu, zid)
        if duplicate then
            debug_log('event begin (repeat) packet=0x%03X npc=%s menu=%s', id, tostring(npc_id), tostring(menu))
        else
            debug_log('event begin packet=0x%03X npc=%s name=%s zone=%s menu=%s initiated=%s known=%s player_status=%s',
                id, tostring(npc_id), tostring(ev.npc_name), tostring(zid), tostring(menu),
                tostring(ev.initiated), tostring(ev.reason == 'known-event'), tostring(player_status()))
            debug_log('  begin packet bytes: %s', hex(original:sub(1, 80)))
            if ev.npc_name then
                local info = speakers.resolve(ev.npc_name, ev)
                send_hint(ev.npc_name, info, 'event')
            end
        end
    elseif id == 0x052 then
        census_flush('event')
        local old = scene.finish()
        debug_log('event release (0x052) type=%s lines=%s cutscene=%s reason=%s',
            tostring(original:byte(5)), old and tostring(old.lines) or '-',
            old and tostring(old.cutscene) or '-', old and tostring(old.reason) or '-')
        stop_audio('release')
    elseif id == 0x00B then
        forget_zone()
        census = { ['in'] = {}, out = {} }
        debug_log('zone out (0x00B)')
        stop_audio('zone')
    elseif id == 0x036 then
        local actor = scene.u32(original, 4)
        scene.on_npc_chat(actor)
        debug_log('npc chat 0x036 actor=%s msg=%s', tostring(actor), tostring(scene.u16(original, 0x0A)))
    end
end)

windower.register_event('outgoing chunk', function(id, original)
    census_note('out', id)
    if id == 0x01A or id == 0x036 then
        scene.on_outgoing(id, original)
        if settings.debug then
            debug_log('outgoing 0x%03X target=%s category=%s', id,
                tostring(scene.u32(original, 4)), tostring(scene.u16(original, 0x0A)))
        end
    elseif id == 0x05B then
        local automated = original:byte(0x0E + 1) == 1
        debug_log('outgoing 0x05B option=%s automated=%s', tostring(scene.u16(original, 8)),
            tostring(automated))
        if not automated then stop_audio('event-choice') end
    end
end)

windower.register_event('zone change', function()
    forget_zone()
    send_hello()
end)

windower.register_event('target change', function(index)
    if not settings.prefetch or not index or index == 0 then return end
    local mob = windower.ffxi.get_mob_by_index(index)
    -- spawn_type 2 = town NPC (see //koto inspect t to verify on your client)
    if mob and mob.name and mob.spawn_type == 2 then
        local name, info = speakers.target()
        if name then send_hint(name, info, 'target') end
    end
end)

windower.register_event('keyboard', function(key_id, pressed, flags, blocked)
    -- Enter (DIK 28) while an event is on screen: cut the line. Controller
    -- advance is covered by the 0x05B/0x052 packets above.
    if key_id == 28 and pressed and not blocked and scene.current()
        and not windower.ffxi.get_info().chat_open then
        stop_audio('enter')
    end
end)

windower.register_event('prerender', function()
    bridge.pump()
    if not bridge.is_up() and not state.warned_down and not state.ever_up
        and os.clock() - state.loaded_at > 8 then
        state.warned_down = true
        say('voice server not found on port ' .. tostring(settings.port)
            .. '. Start it with server\\kotodama.cmd (Windows) or python3 server/kotodama_server.py (Linux)')
    end
end)

-- Lifecycle ----------------------------------------------------------------

local function initialize()
    load_settings()
    cutscenes.load()
    voicelines.load()
    state.loaded_at = os.clock()
    state.warned_down = false
    open_link()
end

windower.register_event('load', initialize)

local function after_login()
    load_settings()
    send_hello()
end

windower.register_event('login', function()
    -- Re-read settings shortly after login so per-character values apply.
    after_login:schedule(5)
end)

windower.register_event('unload', function()
    bridge.send({ t = 'stop', reason = 'unload' })
    bridge.close()
end)

-- Commands -----------------------------------------------------------------

local function show_help()
    local lines = {
        'Kotodama commands (//koto <command>):',
        '  on | off                      enable / disable voiceover',
        '  mode all|events|cutscenes     what to voice (all dialog / any event / cutscenes only)',
        '  battle|own|narrator on|off    battle chatter / your own lines / unnamed narration',
        '  interrupt on|off              new line cuts off the previous one',
        '  prefetch on|off               prepare voices when you target or talk to NPCs',
        '  volume <0-400>                voice loudness in percent (100 = normal)',
        '  duck on|off | duck <0-100>    lower the game audio while speaking / how much is kept',
        '  norm on|off|default           even out loudness between voices (default = server config)',
        '  stop   replay',
        '  provider <name|default>       switch the voice provider live (default = server config)',
        '  providers                     list voice providers (openai, elevenlabs, minimax, kokoro, ...)',
        '  voices | voices sync          voice list status for the active provider / refresh it',
        '  deny <name|t|last>   allow <name|t|last>   deny list   (names accept * wildcards)',
        '  voice <name|t|last>           show the resolved voice profile',
        '  voice <name|t|last> key=value ...   set overrides: gender age species personality',
        '        tone accent pitch pace voice direction  (use _ for spaces, e.g. tone=dry_wit)',
        '  voice <name|t|last> reset | reinfer | promote',
        '  cutscene | cutscene forget    mark the current/last NPC event as a cutscene (remembered), or undo that',
        '  status | inspect [t] | debug on|off',
        '  say <text>                    speak the text in the custom voice (placeholders: <me> <t> <bt> <pet> <ft> <lastst>)',
        '  line <name> | line list       speak a saved line / list them  (macro: /con koto line <name>)',
        '  line add <name> <text> | line del <name>   save / delete a line',
        '  voice @custom [key=value ...|reset]   the voice for say and line (the narrator\'s until changed)',
        '  test [event|cutscene|ambient|battle] <Name> : <text>',
    }
    for _, l in ipairs(lines) do windower.add_to_chat(207, l) end
end

-- Resolve a target token list into an NPC name. 't' = current target,
-- 'last' = last speaker, anything else is the literal (possibly multi-word) name.
local function resolve_name(tokens)
    if #tokens == 0 then return nil, 'no NPC given (use a name, t or last)' end
    local joined = table.concat(tokens, ' ')
    if joined == 't' then
        local mob = windower.ffxi.get_mob_by_target('t')
        if not mob or not mob.name then return nil, 'nothing targeted' end
        return mob.name
    elseif joined == 'last' then
        if not state.last_speaker then return nil, 'no one has spoken yet' end
        return state.last_speaker
    end
    return joined
end

local function parse_voice_args(args)
    local name_tokens, params, action = {}, {}, nil
    for i = 2, #args do
        local a = args[i]
        local k, v = a:match('^([%a_]+)=(.+)$')
        if k then
            params[k:lower()] = v:gsub('_', ' ')
        elseif a == 'reset' or a == 'reinfer' or a == 'promote' then
            action = a
        else
            name_tokens[#name_tokens + 1] = a
        end
    end
    return name_tokens, params, action
end

local function print_status()
    local sent, received = bridge.stats()
    local ev = scene.current()
    say(string.format('%s | link %s (sent %d, received %d) | mode %s | volume %d%% | duck %s (keep %d%%) | provider %s',
        settings.enabled and 'ON' or 'OFF', bridge.is_up() and 'UP' or 'DOWN', sent, received,
        settings.mode, settings.volume, settings.duck and 'on' or 'off', settings.duck_level,
        (settings.provider or '') ~= '' and settings.provider or 'default'))
    say(string.format('battle=%s own=%s narrator=%s interrupt=%s prefetch=%s debug=%s deny=%d',
        tostring(settings.battle), tostring(settings.own_voice), tostring(settings.narrator),
        tostring(settings.interrupt), tostring(settings.prefetch), tostring(settings.debug),
        settings.deny:length()))
    if ev then
        say(string.format('event: npc=%s cutscene=%s (%s) lines=%d', tostring(ev.npc_name),
            tostring(ev.cutscene), tostring(ev.reason), ev.lines))
    else
        say('event: none')
    end
    say(string.format('%d cutscene events known (library/cutscenes.json)', cutscenes.count()))
    bridge.send({ t = 'cmd', name = 'status' })
end

local function do_inspect(args)
    if args[2] == 't' then
        local mob = windower.ffxi.get_mob_by_target('t')
        if not mob then say('nothing targeted'); return end
        local line = speakers.dump(mob)
        windower.add_to_chat(207, line)
        local f = io.open(windower.addon_path .. 'data/inspect.log', 'a')
        if f then f:write(os.date('%Y-%m-%d %H:%M:%S '), line, '\n'); f:close() end
        say('mob fields written to data/inspect.log')
    else
        local ev = scene.current()
        if ev then
            say(string.format('event npc=%s id=%s packet=0x%03X menu=%s initiated=%s cutscene=%s (%s)',
                tostring(ev.npc_name), tostring(ev.npc_id), ev.packet or 0, tostring(ev.menu),
                tostring(ev.initiated), tostring(ev.cutscene), tostring(ev.reason)))
        else
            say('no active event. Use //koto inspect t to dump the target\'s fields.')
        end
    end
end

windower.register_event('addon command', function(command, ...)
    local args = { command and command:lower() or 'help', ... }
    local cmd = args[1]

    if cmd == 'help' or cmd == nil then
        show_help()

    elseif cmd == 'on' or cmd == 'off' then
        settings.enabled = (cmd == 'on')
        if not settings.enabled then bridge.send({ t = 'stop', reason = 'disabled' }) end
        say('voiceover ' .. cmd)

    elseif cmd == 'mode' then
        local m = args[2] and args[2]:lower()
        if m and MODES[m] then
            settings.mode = m
            say('mode: ' .. m)
        else
            say('mode is ' .. settings.mode .. ' (all | events | cutscenes)')
        end

    elseif cmd == 'battle' or cmd == 'own' or cmd == 'narrator' or cmd == 'interrupt' or cmd == 'prefetch' then
        local key = ({ own = 'own_voice' })[cmd] or cmd
        local v = args[2] and ONOFF[args[2]:lower()]
        if v == nil then
            settings[key] = not settings[key]
        else
            settings[key] = v
        end
        say(cmd .. ': ' .. (settings[key] and 'on' or 'off'))
        if cmd == 'interrupt' then send_config() end

    elseif cmd == 'volume' then
        local v = tonumber(args[2])
        if v then
            settings.volume = math.max(0, math.min(400, math.floor(v)))
            send_config()
        end
        say('volume: ' .. settings.volume .. '%')

    elseif cmd == 'duck' then
        local arg = args[2] and args[2]:lower()
        local level = tonumber(arg)
        if level then
            settings.duck_level = math.max(0, math.min(100, math.floor(level)))
            settings.duck = true
        elseif arg == 'on' or arg == 'off' then
            settings.duck = (arg == 'on')
        elseif arg ~= nil then
            say('usage: //koto duck on|off|<0-100>')
        end
        send_config()
        say(settings.duck
            and ('ducking on: game audio drops to ' .. settings.duck_level .. '% while a line is spoken')
            or 'ducking off')

    elseif cmd == 'norm' or cmd == 'normalize' then
        local arg = args[2] and args[2]:lower()
        if arg == 'on' or arg == 'off' then
            settings.normalize = arg
        elseif arg == 'default' then
            settings.normalize = ''
        elseif arg ~= nil then
            say('usage: //koto norm on|off|default')
        end
        send_config()
        say((settings.normalize or '') == '' and 'loudness normalization: as set in server/config.json'
            or ('loudness normalization ' .. settings.normalize))

    elseif cmd == 'provider' then
        local p = args[2] and args[2]:lower()
        if p == nil then
            say('voice provider: ' .. ((settings.provider or '') ~= '' and settings.provider or 'default (server config)'))
        elseif p == 'default' then
            settings.provider = ''
            send_config()
            say('voice provider: default (server config)')
        elseif p:match(PROVIDER_NAME) and #p <= 32 then
            settings.provider = p
            send_config()    -- the sidecar confirms, or explains why it can't switch
        else
            say('usage: //koto provider <name|default>   (//koto providers lists them)')
        end

    elseif cmd == 'providers' then
        bridge.send({ t = 'cmd', name = 'providers_info' })

    elseif cmd == 'voices' then
        bridge.send({ t = 'cmd', name = (args[2] == 'sync') and 'voices_sync' or 'voices_info' })

    elseif cmd == 'stop' then
        bridge.send({ t = 'stop', reason = 'command' })

    elseif cmd == 'replay' then
        bridge.send({ t = 'replay' })

    elseif cmd == 'deny' or cmd == 'allow' then
        if cmd == 'deny' and args[2] == 'list' then
            local names = {}
            for n in settings.deny:it() do names[#names + 1] = n end
            table.sort(names)
            say(#names == 0 and 'deny list is empty' or ('deny list: ' .. table.concat(names, ', ')))
        else
            local tokens = {}
            for i = 2, #args do tokens[#tokens + 1] = args[i] end
            local name, err = resolve_name(tokens)
            if not name then
                say(err)
            elseif cmd == 'deny' then
                settings.deny:add(name:lower())
                bridge.send({ t = 'stop', reason = 'deny' })
                say('denied: ' .. name)
            else
                settings.deny:remove(name:lower())
                say('allowed: ' .. name)
            end
        end

    elseif cmd == 'voice' then
        local name_tokens, params, action = parse_voice_args(args)
        local name, err = resolve_name(name_tokens)
        if not name then
            say(err)
        else
            local zid = zone_info()
            if action then
                bridge.send({ t = 'cmd', name = 'voice_' .. action, npc = name, zone = zid })
            elseif next(params) then
                bridge.send({ t = 'cmd', name = 'voice_set', npc = name, zone = zid, params = params })
            else
                bridge.send({ t = 'cmd', name = 'voice_show', npc = name, zone = zid })
            end
        end

    elseif cmd == 'cutscene' then
        if args[2] == 'forget' then
            local target, removed = scene.forget_cutscene()
            say(target and (removed and ('forgot ' .. tostring(target.npc_name) .. ' (menu ' .. tostring(target.menu) .. ')')
                or 'that event was not in the cutscene list') or 'no event to forget')
        else
            local target, result = scene.mark_cutscene()
            if target then
                say(string.format('%s (menu %s) is a cutscene: %s', tostring(target.npc_name),
                    tostring(target.menu), result))
            else
                say(result)
            end
        end

    elseif cmd == 'status' then
        print_status()

    elseif cmd == 'inspect' then
        do_inspect(args)

    elseif cmd == 'debug' then
        local v = args[2] and ONOFF[args[2]:lower()]
        if v == nil then v = not settings.debug end
        settings.debug = v
        send_config()
        say('debug ' .. (v and 'on (writing to data/debug.log)' or 'off'))

    elseif cmd == 'say' then
        local text = table.concat({ select(2, unpack(args)) }, ' ')
        if text == '' then
            say('usage: //koto say <text>   (in a macro: /con koto say <text>)')
        else
            speak_custom(text, 'say')
        end
        return

    elseif cmd == 'line' or cmd == 'lines' then
        local sub = args[2] and args[2]:lower()
        if sub == nil or sub == 'list' then
            local names = voicelines.names()
            if #names == 0 then
                say('no saved lines. Save one with //koto line add <name> <text>')
            else
                say(#names .. ' saved line(s):')
                for _, n in ipairs(names) do say('  ' .. n .. ': ' .. voicelines.get(n)) end
            end
        elseif sub == 'add' then
            local name = args[3]
            local text = table.concat({ select(4, unpack(args)) }, ' ')
            local ok, err = voicelines.set(name, text)
            if ok then
                say('saved line ' .. name:lower() .. '. Macro: /con koto line ' .. name:lower())
            else
                say('line add: ' .. err .. '. Usage: //koto line add <name> <text>')
            end
        elseif sub == 'del' then
            if not args[3] then
                say('usage: //koto line del <name>')
            elseif voicelines.remove(args[3]) then
                say('deleted line ' .. args[3]:lower())
            else
                say('no saved line called ' .. tostring(args[3]))
            end
        else
            local text = voicelines.get(sub)
            if text then
                speak_custom(text, 'line ' .. sub)
            else
                say('no saved line called ' .. sub .. ' (//koto line list)')
            end
        end
        return

    elseif cmd == 'test' then
        local rest = {}
        local forced
        for i = 2, #args do
            local a = args[i]
            if i == 2 and (a == 'event' or a == 'cutscene' or a == 'ambient' or a == 'battle') then
                forced = a
            else
                rest[#rest + 1] = a
            end
        end
        local text = table.concat(rest, ' ')
        if text == '' then
            say('usage: //koto test [event|cutscene|ambient|battle] <Name> : <text>')
        else
            local raw = text
            pcall(function() raw = windower.to_shift_jis(text) end)
            state.last_line = nil
            process_line(raw, forced == 'battle' and 142 or 150, forced or 'ambient')
        end

    else
        show_help()
        return
    end

    save_settings()
end)
