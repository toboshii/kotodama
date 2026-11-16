-- Dialog capture helpers: turn a raw FFXI chat line into plain speakable text.
--
-- The control-code handling is derived from the Balloon addon
-- (Copyright 2018 Hando, 2021 Yuki, 2022 Ghosty; BSD-3-Clause), whose
-- process_balloon / SubCharacters* functions document the FFXI text markup.
-- Unlike Balloon we never modify the line, we only read it.

local dialog = {}

-- Chat modes that carry NPC speech (see Balloon).
dialog.MODE = {
    MESSAGE = 150,
    SYSTEM = 151,
    TIMED_MESSAGE = 144,
    TIMED_BATTLE = 142,
    CUTSCENE_EMOTE = 15,
}

dialog.SPEECH_MODES = {
    [150] = true, [151] = true, [144] = true, [142] = true, [15] = true,
}

local function plain_replace(s, find, repl)
    local out, pos = {}, 1
    while true do
        local a, b = s:find(find, pos, true)
        if not a then break end
        out[#out + 1] = s:sub(pos, a - 1)
        out[#out + 1] = repl
        pos = b + 1
    end
    out[#out + 1] = s:sub(pos)
    return table.concat(out)
end

-- Byte sequences (Shift-JIS / FFXI private use), replaced before conversion.
local PRE_SUBS = {
    -- escaped water / light glyphs first (0x25 is an escape byte)
    { '\239\37\36', ' Water ' },
    { '\239\37\37', ' Light ' },
    { '\239\31', ' Fire ' },
    { '\239\32', ' Ice ' },
    { '\239\33', ' Wind ' },
    { '\239\34', ' Earth ' },
    { '\239\35', ' Lightning ' },
    { '\239\38', ' Dark ' },
    { '\129\64', ' ' },          -- ideographic space
    { '\129\244', ' ' },         -- musical note
    { '\136\105', 'e' },         -- e acute
    { '\135\178', '"' },         -- centered left double quote
    { '\135\179', '"' },         -- centered right double quote
}

-- Markers that tell us about scene flow.
local AUTO_PROMPT_MARK = '\127[\52-\54]'   -- 0x7F 0x34..0x36: auto-advancing prompt
local PROMPT_MARK = '\127[\49-\51\55]'     -- 0x7F 0x31..0x33,0x37: wait-for-button prompt

function dialog.has_auto_prompt(raw)
    return raw:find(AUTO_PROMPT_MARK) ~= nil
end

function dialog.has_prompt(raw)
    return raw:find(PROMPT_MARK) ~= nil
end

-- A line that is nothing but an auto-prompt is a scene-clear marker.
function dialog.is_blank_auto_prompt(raw)
    local stripped = raw:gsub('\30.', ''):gsub('\31.', ''):gsub('\127[\52-\54][\1-\6]?', ''):gsub('[%c%s]', '')
    return stripped == '' and dialog.has_auto_prompt(raw)
end

-- Raw (Shift-JIS) text -> UTF-8 text with markup removed. Newline bytes
-- (0x07) become spaces so a multi-line box reads as one sentence run.
function dialog.clean(raw)
    local s = raw
    for _, p in ipairs(PRE_SUBS) do
        s = plain_replace(s, p[1], p[2])
    end

    s = s:gsub('\30.', '')                 -- 0x1E xx  color codes
    s = s:gsub('\31.', '')                 -- 0x1F xx  color codes
    s = s:gsub('\127[\49-\51\55]', '')     -- wait prompts
    s = s:gsub('\127[\52-\54][\1-\6]?', '')-- auto prompts (+ timing byte)
    s = s:gsub('\7', ' ')
    s = s:gsub('[%z\1-\6\8-\31\127]', '')  -- any other control byte

    local ok, utf8 = pcall(windower.from_shift_jis, s)
    if ok and type(utf8) == 'string' then
        s = utf8
    end

    -- Typographic leftovers -> speakable forms.
    s = plain_replace(s, '\226\128\156', '"')   -- “
    s = plain_replace(s, '\226\128\157', '"')   -- ”
    s = plain_replace(s, '\226\152\134', '')    -- ☆
    s = plain_replace(s, '\226\152\133', '')    -- ★
    s = plain_replace(s, '\226\153\170', '')    -- ♪

    s = s:gsub('%s+', ' ')
    s = s:gsub('^ ', ''):gsub(' $', '')
    return s
end

-- Split "Name : text" into name, text. Names are short and contain no
-- sentence punctuation; anything else is narration (name = nil).
-- A few NPC names carry a title after a comma, always initials ("Rabid Wolf, I.M.",
-- "Leonoyne, T.K."). Any other comma means the colon belongs to a sentence.
local function plausible_name(name)
    if #name > 31 or name:find('^%s') or name:find('[!?]') then return false end
    if name:find(',', 1, true) then
        return name:match('^[^,]+, %u%.[%u%.]*$') ~= nil
    end
    return true
end

-- "???" is how the game names a character it hasn't introduced yet.
function dialog.is_unnamed(name)
    return name ~= nil and name:find('^%?+$') ~= nil
end

function dialog.split_speaker(text)
    local name, rest = text:match('^([^:]+) : (.*)$')
    if name and (dialog.is_unnamed(name) or plausible_name(name)) then
        return name, rest
    end
    return nil, text
end

-- True if the text has anything worth speaking.
function dialog.speakable(text)
    return text ~= nil and text:find('[%a\128-\255]') ~= nil and #text >= 2
end

return dialog
