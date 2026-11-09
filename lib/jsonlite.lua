-- Minimal JSON encoder/decoder for the Kotodama <-> sidecar link.
-- Windower's libs/json.lua is decode-only and quirky, so we carry our own.

local M = {}

local escapes = {
    ['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b', ['\f'] = '\\f',
    ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t',
}

local function esc_char(c)
    return escapes[c] or string.format('\\u%04x', c:byte())
end

local function enc_string(s)
    return '"' .. s:gsub('[%c"\\]', esc_char) .. '"'
end

local function is_array(t)
    local n = 0
    for k in pairs(t) do
        if type(k) ~= 'number' then return false end
        n = n + 1
    end
    return n == #t
end

local function enc(v, out, depth)
    if depth > 8 then error('json: nesting too deep') end
    local tv = type(v)
    if tv == 'string' then
        out[#out + 1] = enc_string(v)
    elseif tv == 'number' then
        if v ~= v or v == math.huge or v == -math.huge then
            out[#out + 1] = 'null'
        elseif v == math.floor(v) and math.abs(v) < 1e15 then
            out[#out + 1] = string.format('%d', v)
        else
            out[#out + 1] = string.format('%.6g', v)
        end
    elseif tv == 'boolean' then
        out[#out + 1] = v and 'true' or 'false'
    elseif tv == 'nil' then
        out[#out + 1] = 'null'
    elseif tv == 'table' then
        if next(v) ~= nil and is_array(v) then
            out[#out + 1] = '['
            for i = 1, #v do
                if i > 1 then out[#out + 1] = ',' end
                enc(v[i], out, depth + 1)
            end
            out[#out + 1] = ']'
        else
            out[#out + 1] = '{'
            local first = true
            for k, val in pairs(v) do
                if type(k) == 'string' and type(val) ~= 'function' then
                    if not first then out[#out + 1] = ',' end
                    first = false
                    out[#out + 1] = enc_string(k)
                    out[#out + 1] = ':'
                    enc(val, out, depth + 1)
                end
            end
            out[#out + 1] = '}'
        end
    else
        out[#out + 1] = 'null'
    end
end

function M.encode(v)
    local out = {}
    enc(v, out, 0)
    return table.concat(out)
end

-- Decoder ------------------------------------------------------------------

local function utf8_char(cp)
    if cp < 0x80 then
        return string.char(cp)
    elseif cp < 0x800 then
        return string.char(0xC0 + math.floor(cp / 0x40), 0x80 + cp % 0x40)
    elseif cp < 0x10000 then
        return string.char(0xE0 + math.floor(cp / 0x1000),
            0x80 + math.floor(cp / 0x40) % 0x40, 0x80 + cp % 0x40)
    end
    return string.char(0xF0 + math.floor(cp / 0x40000),
        0x80 + math.floor(cp / 0x1000) % 0x40,
        0x80 + math.floor(cp / 0x40) % 0x40, 0x80 + cp % 0x40)
end

local simple = { b = '\b', f = '\f', n = '\n', r = '\r', t = '\t', ['"'] = '"', ['\\'] = '\\', ['/'] = '/' }

local function skip_ws(s, i)
    return s:find('[^ \t\r\n]', i) or #s + 1
end

local dec

local function dec_string(s, i)
    local out, j = {}, i + 1
    while true do
        local k = s:find('["\\]', j)
        if not k then error('json: unterminated string') end
        out[#out + 1] = s:sub(j, k - 1)
        if s:sub(k, k) == '"' then
            return table.concat(out), k + 1
        end
        local e = s:sub(k + 1, k + 1)
        if e == 'u' then
            local hex = s:sub(k + 2, k + 5)
            if not hex:match('^%x%x%x%x$') then error('json: bad \\u escape') end
            local cp = tonumber(hex, 16)
            j = k + 6
            if cp >= 0xD800 and cp < 0xDC00 and s:sub(j, j + 1) == '\\u' then
                local lo = tonumber(s:sub(j + 2, j + 5), 16)
                if lo and lo >= 0xDC00 and lo < 0xE000 then
                    cp = 0x10000 + (cp - 0xD800) * 0x400 + (lo - 0xDC00)
                    j = j + 6
                end
            end
            out[#out + 1] = utf8_char(cp)
        else
            if not simple[e] then error('json: bad escape') end
            out[#out + 1] = simple[e]
            j = k + 2
        end
    end
end

function dec(s, i, depth)
    if depth > 16 then error('json: nesting too deep') end
    i = skip_ws(s, i)
    local c = s:sub(i, i)
    if c == '{' then
        local obj = {}
        i = skip_ws(s, i + 1)
        if s:sub(i, i) == '}' then return obj, i + 1 end
        while true do
            i = skip_ws(s, i)
            if s:sub(i, i) ~= '"' then error('json: expected key') end
            local key
            key, i = dec_string(s, i)
            i = skip_ws(s, i)
            if s:sub(i, i) ~= ':' then error('json: expected :') end
            obj[key], i = dec(s, i + 1, depth + 1)
            i = skip_ws(s, i)
            local d = s:sub(i, i)
            if d == '}' then return obj, i + 1 end
            if d ~= ',' then error('json: expected , or }') end
            i = i + 1
        end
    elseif c == '[' then
        local arr = {}
        i = skip_ws(s, i + 1)
        if s:sub(i, i) == ']' then return arr, i + 1 end
        while true do
            arr[#arr + 1], i = dec(s, i, depth + 1)
            i = skip_ws(s, i)
            local d = s:sub(i, i)
            if d == ']' then return arr, i + 1 end
            if d ~= ',' then error('json: expected , or ]') end
            i = i + 1
        end
    elseif c == '"' then
        return dec_string(s, i)
    elseif s:sub(i, i + 3) == 'true' then
        return true, i + 4
    elseif s:sub(i, i + 4) == 'false' then
        return false, i + 5
    elseif s:sub(i, i + 3) == 'null' then
        return nil, i + 4
    end
    local num = s:match('^-?%d+%.?%d*[eE]?[+-]?%d*', i)
    if not num or num == '' then error('json: unexpected character at ' .. i) end
    return tonumber(num), i + #num
end

-- Returns the decoded value, or nil + error message. Never throws.
function M.decode(s)
    local ok, res = pcall(function()
        local v, i = dec(s, 1, 0)
        i = skip_ws(s, i)
        if i <= #s then error('json: trailing data') end
        return v
    end)
    if ok then return res end
    return nil, tostring(res)
end

return M
