-- UDP link to the Kotodama sidecar (kotodama_server.py) on this machine.
-- Everything here is non-blocking: sends are fire-and-forget, receives are
-- polled from prerender. A dead sidecar can never stall the game.

local json = require('lib/jsonlite')

local bridge = {}

local PING_EVERY = 5      -- seconds between heartbeats
local DOWN_AFTER = 15     -- seconds of silence before the link is declared down
local POLL_EVERY = 0.1    -- seconds between receive polls
local MAX_DATAGRAM = 8000

local state = {
    sock = nil,
    port = nil,
    up = false,
    warned = false,
    last_rx = 0,
    last_ping = 0,
    last_poll = 0,
    sent = 0,
    received = 0,
    handlers = {},
    on_state = nil,
}

local function now()
    return os.clock()
end

function bridge.open(port, handlers, on_state)
    bridge.close()
    state.handlers = handlers or {}
    state.on_state = on_state
    state.port = port

    local ok, socket = pcall(require, 'socket')
    if not ok then
        return false, 'LuaSocket unavailable: ' .. tostring(socket)
    end

    local udp, err = socket.udp()
    if not udp then return false, err end
    udp:settimeout(0)
    local ok2, err2 = udp:setpeername('127.0.0.1', port)
    if not ok2 then
        udp:close()
        return false, err2
    end
    state.sock = udp
    state.up = false
    state.warned = false
    state.last_rx = 0
    state.last_ping = 0
    return true
end

function bridge.close()
    if state.sock then
        pcall(function() state.sock:close() end)
        state.sock = nil
    end
    state.up = false
end

function bridge.is_up()
    return state.up
end

function bridge.stats()
    return state.sent, state.received
end

function bridge.send(msg)
    if not state.sock then return false end
    local ok, payload = pcall(json.encode, msg)
    if not ok then return false end
    if #payload > MAX_DATAGRAM then return false end
    local sent = state.sock:send(payload)
    if sent then state.sent = state.sent + 1 end
    return sent ~= nil
end

local function set_up(up)
    if state.up == up then return end
    state.up = up
    if state.on_state then state.on_state(up) end
end

local function dispatch(raw)
    local msg = json.decode(raw)
    if type(msg) ~= 'table' or type(msg.t) ~= 'string' then return end
    state.received = state.received + 1
    state.last_rx = now()
    if not state.up then set_up(true) end
    local h = state.handlers[msg.t]
    if h then h(msg) end
end

-- Call from prerender. Cheap when nothing is due.
function bridge.pump()
    if not state.sock then return end
    local t = now()

    if t - state.last_ping >= PING_EVERY then
        state.last_ping = t
        bridge.send({ t = 'ping' })
    end

    if t - state.last_poll >= POLL_EVERY then
        state.last_poll = t
        for _ = 1, 20 do
            local data = state.sock:receive()
            if not data then break end
            dispatch(data)
        end
    end

    if state.up and t - state.last_rx > DOWN_AFTER then
        set_up(false)
    end
end

return bridge
