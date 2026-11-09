-- Registry of NPC events known to be cutscenes.
--
-- FFXI doesn't flag cutscenes, and some start with several lines from one
-- speaker that look exactly like ordinary dialog (the opening of the
-- Fochacha scene, for instance). We can only tell once a second speaker
-- appears, which is too late for the first lines. So whenever an event is
-- recognised as a cutscene by something solid, we remember its (zone, menu id)
-- and classify it correctly from the first line the next time, on any run.
--
-- library/cutscenes.json is plain, sorted, diff-friendly JSON, safe to share
-- (it contains no player data), and ships with a few confirmed entries.

local json = require('lib/jsonlite')

local M = {}

local PATH = 'library/cutscenes.json'
local FORMAT = 1

local entries = {}      -- "zone:menu" -> { zone=, menu=, npc=, source=, reason= }
local count = 0

local function key(zone, menu)
    return tostring(zone) .. ':' .. tostring(menu)
end

local function full_path()
    return windower.addon_path .. PATH
end

function M.load()
    entries, count = {}, 0
    local f = io.open(full_path(), 'r')
    if not f then return 0 end
    local text = f:read('*a')
    f:close()
    local data = json.decode(text)
    if type(data) ~= 'table' or data.format ~= FORMAT or type(data.events) ~= 'table' then
        return 0
    end
    for _, e in pairs(data.events) do
        if type(e) == 'table' and tonumber(e.zone) and tonumber(e.menu) then
            local zone, menu = tonumber(e.zone), tonumber(e.menu)
            entries[key(zone, menu)] = {
                zone = zone, menu = menu,
                npc = type(e.npc) == 'string' and e.npc or nil,
                source = type(e.source) == 'string' and e.source or 'learned',
                reason = type(e.reason) == 'string' and e.reason or nil,
            }
            count = count + 1
        end
    end
    return count
end

local function field(name, value)
    return json.encode(name) .. ': ' .. json.encode(value)
end

-- Deterministic output: sorted keys, fixed field order.
local function serialize()
    local keys = {}
    for k in pairs(entries) do keys[#keys + 1] = k end
    table.sort(keys, function(a, b)
        local ea, eb = entries[a], entries[b]
        if ea.zone ~= eb.zone then return ea.zone < eb.zone end
        return ea.menu < eb.menu
    end)
    local lines = {}
    for _, k in ipairs(keys) do
        local e = entries[k]
        local parts = {
            field('menu', e.menu), field('npc', e.npc or ''), field('reason', e.reason or ''),
            field('source', e.source), field('zone', e.zone),
        }
        lines[#lines + 1] = '    ' .. json.encode(k) .. ': {' .. table.concat(parts, ', ') .. '}'
    end
    return '{\n  "format": ' .. FORMAT .. ',\n  "events": {\n' .. table.concat(lines, ',\n')
        .. (#lines > 0 and '\n' or '') .. '  }\n}\n'
end

local function save()
    windower.create_dir(windower.addon_path .. 'library')
    local tmp = full_path() .. '.tmp'
    local f = io.open(tmp, 'w')
    if not f then return false end
    f:write(serialize())
    f:close()
    os.remove(full_path())
    return os.rename(tmp, full_path()) and true or false
end

function M.known(zone, menu)
    if not zone or not menu then return nil end
    return entries[key(zone, menu)]
end

function M.count()
    return count
end

-- Returns true if this added a new entry.
function M.learn(zone, menu, npc, reason, source)
    if not zone or not menu then return false end
    local k = key(zone, menu)
    if entries[k] then return false end
    entries[k] = { zone = zone, menu = menu, npc = npc, source = source or 'learned', reason = reason }
    count = count + 1
    save()
    return true
end

function M.forget(zone, menu)
    local k = key(zone, menu)
    if not entries[k] then return false end
    entries[k] = nil
    count = count - 1
    save()
    return true
end

return M
