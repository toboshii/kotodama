-- Resolve a spoken name to what the sidecar needs to pick a voice:
-- race id (and name), creature hint from the model id, and whether it is
-- the player. Anything we cannot resolve is sent as a bare name; the
-- sidecar falls back to its NPC library and name keywords.

local speakers = {}

local ok_models, creature_models = pcall(require, 'data/creature_models')
if not ok_models or type(creature_models) ~= 'table' then
    creature_models = {}
end

local ok_res, res = pcall(require, 'resources')
local races = ok_res and res and res.races or {}

local memo = {}   -- lowercased name -> info, only for successful mob lookups

function speakers.clear()
    memo = {}
end

-- How a character looks: race plus every model slot (face, head, body...,
-- or one creature model). The same character in the same scene looks the
-- same, whatever name the game is showing for them.
local function look_of(mob)
    if type(mob.models) ~= 'table' then return nil end
    local parts = { tostring(tonumber(mob.race) or 0) }
    for _, m in ipairs(mob.models) do parts[#parts + 1] = tostring(m) end
    return #parts > 1 and table.concat(parts, ',') or nil
end

local function from_mob(name, mob)
    local info = { name = name, found = true }
    info.id = mob.id
    info.race = tonumber(mob.race) or 0
    local race_row = races[info.race]
    if race_row then info.race_name = race_row.en end

    local models = mob.models
    if type(models) == 'table' then
        info.model = tonumber(models[1])
        if info.model and creature_models[info.model] then
            info.creature = creature_models[info.model]
        end
    end
    info.spawn_type = mob.spawn_type
    info.look = look_of(mob)
    return info
end

local UNNAMED_RANGE = 50     -- yalms

local function distance(mob)
    return mob.distance and math.sqrt(mob.distance) or 0      -- Windower reports it squared
end

-- A body with something to go on: a race, or a creature model we know.
-- Quest markers, which the game also names "???", have neither.
local function has_body(mob)
    if (tonumber(mob.race) or 0) > 0 then return true end
    local models = type(mob.models) == 'table' and mob.models or {}
    return creature_models[tonumber(models[1]) or -1] ~= nil
end

-- The character behind a "???" line: an actor the game hasn't introduced
-- carries the name it shows, "???". Returns their info (named `name`) and
-- how many such actors were in range; nil when none was, or when several
-- were and nothing says which one is talking.
function speakers.find_unnamed(shown, name)
    local mobs = windower.ffxi.get_mob_array and windower.ffxi.get_mob_array() or {}
    local found = {}
    for _, mob in pairs(mobs) do
        if mob.name == shown and has_body(mob) and distance(mob) <= UNNAMED_RANGE then
            found[#found + 1] = mob
        end
    end
    if #found == 1 then
        return from_mob(name, found[1]), 1
    end
    return nil, #found
end

-- The nearest characters, for the debug log when no "???" actor was found.
function speakers.nearby(limit)
    local mobs = windower.ffxi.get_mob_array and windower.ffxi.get_mob_array() or {}
    local list = {}
    for _, mob in pairs(mobs) do
        if mob.name and mob.name ~= '' and has_body(mob) and distance(mob) <= UNNAMED_RANGE then
            list[#list + 1] = mob
        end
    end
    table.sort(list, function(a, b) return distance(a) < distance(b) end)
    local out = {}
    for i = 1, math.min(limit, #list) do
        local m = list[i]
        out[#out + 1] = string.format('%s(id=%s race=%s look=%s %.0fy)', m.name, tostring(m.id),
            tostring(m.race), tostring(look_of(m)), distance(m))
    end
    return table.concat(out, ' ')
end

-- event: the table from scene.current() (may be nil)
function speakers.resolve(name, event)
    if not name then
        return { narrator = true }
    end

    local key = name:lower()
    local player = windower.ffxi.get_player()
    if player and player.name and key == player.name:lower() then
        local me = windower.ffxi.get_mob_by_id(player.id)
        local info = { name = name, is_player = true, found = true }
        if me then
            info.race = tonumber(me.race) or 0
            local race_row = races[info.race]
            if race_row then info.race_name = race_row.en end
        end
        return info
    end

    if memo[key] then
        return memo[key]
    end

    local mob
    if event and event.npc_id and event.npc_name and event.npc_name:lower() == key then
        mob = windower.ffxi.get_mob_by_id(event.npc_id)
    end
    if not mob then
        mob = windower.ffxi.get_mob_by_name(name)
    end

    if mob then
        local info = from_mob(name, mob)
        memo[key] = info
        return info
    end
    return { name = name }
end

-- Resolve the current target ('t') into a name + info for commands/hints.
function speakers.target()
    local mob = windower.ffxi.get_mob_by_target('t')
    if not mob or not mob.name then return nil end
    return mob.name, from_mob(mob.name, mob), mob
end

-- Dump every scalar field of a mob table, for calibration (//koto inspect).
function speakers.dump(mob)
    local keys = {}
    for k in pairs(mob) do keys[#keys + 1] = tostring(k) end
    table.sort(keys)
    local out = {}
    for _, k in ipairs(keys) do
        local v = mob[k]
        if type(v) == 'table' then
            local parts = {}
            for i, x in ipairs(v) do
                if i > 12 then parts[#parts + 1] = '...' break end
                parts[#parts + 1] = tostring(x)
            end
            v = '{' .. table.concat(parts, ',') .. '}'
        end
        out[#out + 1] = string.format('%s=%s', k, tostring(v))
    end
    return table.concat(out, ' ')
end

return speakers
