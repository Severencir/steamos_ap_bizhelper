local shutdown_flag = "ap_bizhelper_shutdown.flag"

local function safe_call(fn, ...)
    if not fn then
        return
    end
    pcall(fn, ...)
end

local function check_shutdown_flag()
    local file = io.open(shutdown_flag, "r")
    if file then
        file:close()
        pcall(os.remove, shutdown_flag)
        safe_call(client.saveram)
        safe_call(client.exit)
    end
end

local function register_handler(fn, handler, name)
    if fn then
        pcall(fn, handler, name)
    end
end

register_handler(event and event.onframestart, check_shutdown_flag, "ap_bizhelper_shutdown_flag")
register_handler(event and event.onexit, function()
    safe_call(client.saveram)
end, "ap_bizhelper_shutdown_exit")

safe_call(dofile, "ap_bizhelper/active_connector.lua")

while true do
    emu.frameadvance()
end
