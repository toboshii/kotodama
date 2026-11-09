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
    return info
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
