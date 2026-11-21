-- Custom voice lines: text the player asks Kotodama to speak (//koto say,
-- //koto line <name>), from chat or from a macro (/console koto line <name>).
--
-- Saved lines live in data/voicelines.json, shared by every character on this
-- install. They are stored exactly as written, placeholders included, and
-- filled in only when spoken, so "Go, <t>!" names whoever is targeted then.
--
-- FFXI doesn't expand <t>, <me> and friends in text passed through /console,
-- so expand() does it here.

local json = require('lib/jsonlite')

local M = {}

local PATH = 'data/voicelines.json'
local FORMAT = 1

M.MAX_TEXT = 1500                 -- the sidecar's speak.max_chars
M.MAX_NAME = 32
M.RESERVED = { add = true, del = true, list = true }

local lines = {}                  -- name (lowercase) -> text
local count = 0

local function full_path()
    return windower.addon_path .. PATH
end

function M.valid_name(name)
    return type(name) == 'string' and #name <= M.MAX_NAME and name:find('^[%w_%-]+$') ~= nil
        and not M.RESERVED[name:lower()]
end

function M.load()
    lines, count = {}, 0
    local f = io.open(full_path(), 'rb')
    if not f then return 0 end
    local text = f:read('*a')
    f:close()
    local ok, data = pcall(json.decode, text)
    if not ok or type(data) ~= 'table' or data.format ~= FORMAT or type(data.lines) ~= 'table' then
        return 0
    end
    for name, line in pairs(data.lines) do
        if M.valid_name(name) and type(line) == 'string' and line ~= '' then
            local k = name:lower()
            if not lines[k] then count = count + 1 end
            lines[k] = line:sub(1, M.MAX_TEXT)
        end
    end
    return count
end

function M.names()
    local names = {}
    for k in pairs(lines) do names[#names + 1] = k end
    table.sort(names)
    return names
end

-- Deterministic output: sorted names, one per line.
local function serialize()
    local out = {}
    for _, k in ipairs(M.names()) do
        out[#out + 1] = '    ' .. json.encode(k) .. ': ' .. json.encode(lines[k])
    end
    return '{\n  "format": ' .. FORMAT .. ',\n  "lines": {\n' .. table.concat(out, ',\n')
        .. (#out > 0 and '\n' or '') .. '  }\n}\n'
end

local function save()
    windower.create_dir(windower.addon_path .. 'data')
    local tmp = full_path() .. '.tmp'
    -- Binary mode: Windows text mode would write \r\n.
    local f = io.open(tmp, 'wb')
    if not f then return false end
    f:write(serialize())
    f:close()
    os.remove(full_path())
    return os.rename(tmp, full_path()) and true or false
end

function M.get(name)
    return type(name) == 'string' and lines[name:lower()] or nil
end

function M.count()
    return count
end

-- Returns true, or false and why.
function M.set(name, text)
    if not M.valid_name(name) then
        return false, 'line names are letters, digits, _ and - (up to ' .. M.MAX_NAME
            .. '), and not add, del or list'
    end
    if type(text) ~= 'string' or not text:find('%S') then
        return false, 'no text given'
    end
    if #text > M.MAX_TEXT then
        return false, 'too long (' .. #text .. ' characters, at most ' .. M.MAX_TEXT .. ')'
    end
    local k = name:lower()
    if not lines[k] then count = count + 1 end
    lines[k] = text
    if not save() then return false, 'could not write ' .. PATH end
    return true
end

function M.remove(name)
    local k = type(name) == 'string' and name:lower() or nil
    if not k or not lines[k] then return false end
    lines[k] = nil
    count = count - 1
    save()
    return true
end

-- Auto-translate -------------------------------------------------------------

-- An auto-translate phrase in typed text is 0xFD, type, language, then two
-- bytes for the id, then 0xFD: id = high * 256 + low, the key into
-- resources.auto_translates for a standard phrase ("Healing" is 8 * 256 + 60
-- = 2108), into resources.items for an item (FD 07 02 31 A6 FD is item
-- 12710, Bone Mittens), or into resources.key_items for a key item
-- (FD 13 02 04 57 FD is key item 1111, Delkfutt Key).
local AT_PATTERN = '\253(.)(.)(.)(.)\253'
M.AT_PHRASE = 2                   -- the type byte of a standard phrase
M.AT_ITEM = 7                     -- the type byte of an item
M.AT_KEY_ITEM = 0x13              -- the type byte of a key item

-- Replace auto-translate phrases with their words. lookup(type, id) returns
-- the text or nil; a phrase it doesn't know is dropped, never read out as
-- symbols. Spaces are added only where the phrase would touch a word.
function M.auto_translate(text, lookup)
    if not text:find('\253', 1, true) then return text end
    local out, pos = {}, 1
    while true do
        local a, b, kind, _, hi, lo = text:find(AT_PATTERN, pos)
        if not a then break end
        local before = text:sub(pos, a - 1)
        out[#out + 1] = before
        local words = lookup(kind:byte(), hi:byte() * 256 + lo:byte())
        if type(words) == 'string' and words ~= '' then
            local prev = (#before > 0) and before:sub(-1) or (out[#out - 1] or ''):sub(-1)
            if prev:find('[%w%.,!%?]') then words = ' ' .. words end
            if text:sub(b + 1, b + 1):find('%w') then words = words .. ' ' end
            out[#out + 1] = words
        end
        pos = b + 1
    end
    out[#out + 1] = text:sub(pos)
    -- A stray marker from a phrase cut short: drop it rather than say "you".
    return (table.concat(out):gsub('\253', ''))
end

-- Placeholders -------------------------------------------------------------

-- Placeholder -> what's missing when it can't be filled in. Each is looked up
-- with windower.ffxi.get_mob_by_target (except <me>: the player's name).
M.PLACEHOLDERS = { 'me', 't', 'bt', 'pet', 'ft', 'lastst' }
local MISSING = {
    me = 'not logged in',
    t = 'nothing targeted',
    bt = 'no battle target',
    pet = 'no pet',
    ft = 'no focus target',
    lastst = 'no last sub-target',
}

local function supported_list()
    local out = {}
    for _, p in ipairs(M.PLACEHOLDERS) do out[#out + 1] = '<' .. p .. '>' end
    return table.concat(out, ' ')
end

-- Fill in <me>, <t>, ... (any case). lookup(key) returns a name or nil.
-- Returns the text, or nil and why: a line is never spoken half filled in.
function M.expand(text, lookup)
    local err
    local out = text:gsub('<(%w+)>', function(tag)
        if err then return nil end
        local key = tag:lower()
        if key == 'st' then
            err = '<st> asks you to pick a target on screen, which a command can\'t do. Use <t> or <lastst>'
            return nil
        end
        if not MISSING[key] then
            err = 'unknown placeholder <' .. tag .. '> (supported: ' .. supported_list() .. ')'
            return nil
        end
        local name = lookup(key)
        if type(name) ~= 'string' or name == '' then
            err = MISSING[key] .. ' for <' .. tag .. '>'
            return nil
        end
        return name
    end)
    if err then return nil, err end
    return out
end

return M
