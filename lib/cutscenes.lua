-- Registry of NPC events known to be cutscenes.
--
-- FFXI doesn't flag cutscenes, and some start with several lines from one
-- speaker that look exactly like ordinary dialog (the opening of the
-- Fochacha scene, for instance). We can only tell once a second speaker
-- appears, which is too late for the first lines. So whenever an event is
-- recognised as a cutscene by something solid, we remember its (zone, menu id)
-- and classify it correctly from the first line the next time, on any run.
--
-- Two files, read in this order:
--   library/cutscenes.default.json  shipped with the addon, never written.
--   library/cutscenes.json          what this install learned, plus a
--                                   "forgotten" marker for each shipped
--                                   entry you undid. Wins over the shipped file.
-- An upgrade replaces only the shipped file, so learned events survive it.
-- Both are plain, sorted, diff-friendly JSON, safe to share (no player data).

local json = require('lib/jsonlite')

local M = {}

local PATH = 'library/cutscenes.json'
local DEFAULT_PATH = 'library/cutscenes.default.json'
local FORMAT = 1
local FORGOTTEN = 'forgotten'

-- "zone:menu" -> { zone=, menu=, npc=, source=, reason= }, one table per file.
-- In `own`, source == FORGOTTEN marks a shipped entry this install turned off.
local shipped, own = {}, {}
local count = 0

local function key(zone, menu)
    return tostring(zone) .. ':' .. tostring(menu)
end

local function full_path(path)
    return windower.addon_path .. (path or PATH)
end

-- The entry in effect for a key: ours if we have one, else the shipped one.
local function lookup(k)
    local e = own[k] or shipped[k]
    if e and e.source ~= FORGOTTEN then return e end
    return nil
end

local function recount()
    count = 0
    for k in pairs(own) do
        if lookup(k) then count = count + 1 end
    end
    for k in pairs(shipped) do
        if not own[k] then count = count + 1 end
    end
end

local function read_events(path, into)
    local f = io.open(full_path(path), 'r')
    if not f then return end
    local text = f:read('*a')
    f:close()
    local data = json.decode(text)
    if type(data) ~= 'table' or data.format ~= FORMAT or type(data.events) ~= 'table' then
        return
    end
    for _, e in pairs(data.events) do
        if type(e) == 'table' and tonumber(e.zone) and tonumber(e.menu) then
            local zone, menu = tonumber(e.zone), tonumber(e.menu)
            into[key(zone, menu)] = {
                zone = zone, menu = menu,
                npc = type(e.npc) == 'string' and e.npc or nil,
                source = type(e.source) == 'string' and e.source or 'learned',
                reason = type(e.reason) == 'string' and e.reason or nil,
            }
        end
    end
end

function M.load()
    shipped, own = {}, {}
    read_events(DEFAULT_PATH, shipped)
    read_events(PATH, own)
    -- A "forgotten" marker means nothing in the shipped file itself.
    for k, e in pairs(shipped) do
        if e.source == FORGOTTEN then shipped[k] = nil end
    end
    recount()
    return count
end

local function field(name, value)
    return json.encode(name) .. ': ' .. json.encode(value)
end

-- Deterministic output: sorted keys, fixed field order. Only this install's
-- entries are written; the shipped ones stay in the shipped file.
local function serialize()
    local keys = {}
    for k in pairs(own) do keys[#keys + 1] = k end
    table.sort(keys, function(a, b)
        local ea, eb = own[a], own[b]
        if ea.zone ~= eb.zone then return ea.zone < eb.zone end
        return ea.menu < eb.menu
    end)
    local lines = {}
    for _, k in ipairs(keys) do
        local e = own[k]
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
    -- Binary mode: Windows text mode writes \r\n, which makes every learned
    -- event rewrite every line of this (shared, version-controlled) file.
    local f = io.open(tmp, 'wb')
    if not f then return false end
    f:write(serialize())
    f:close()
    os.remove(full_path())
    return os.rename(tmp, full_path()) and true or false
end

function M.known(zone, menu)
    if not zone or not menu then return nil end
    return lookup(key(zone, menu))
end

function M.count()
    return count
end

-- Returns true if this added a new entry.
function M.learn(zone, menu, npc, reason, source)
    if not zone or not menu then return false end
    local k = key(zone, menu)
    if lookup(k) then return false end
    own[k] = { zone = zone, menu = menu, npc = npc, source = source or 'learned', reason = reason }
    recount()
    save()
    return true
end

function M.forget(zone, menu)
    local k = key(zone, menu)
    if not lookup(k) then return false end
    local s = shipped[k]
    if s then
        -- The shipped file is never written, so the undo goes in ours.
        own[k] = { zone = s.zone, menu = s.menu, npc = s.npc, source = FORGOTTEN, reason = s.reason }
    else
        own[k] = nil
    end
    recount()
    save()
    return true
end

return M
