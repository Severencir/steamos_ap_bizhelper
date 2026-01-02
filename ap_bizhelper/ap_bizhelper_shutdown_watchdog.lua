local SHUTDOWN_FLAG = "ap_bizhelper_shutdown.flag"
local LOG_FILE = "ap_bizhelper_lua_shutdown_log.txt"

local function file_exists(path)
    local file = io.open(path, "rb")
    if file then
        file:close()
        return true
    end
    return false
end

local function log(msg)
    local ok, file = pcall(io.open, LOG_FILE, "a")
    if not ok or not file then
        return
    end
    pcall(file.write, file, msg .. "\n")
    pcall(file.close, file)
end

local function try_saveram_exit(reason)
    log("shutdown: " .. reason)
    pcall(client.saveram)
    pcall(client.exit)
end

event.onconsoleclose(function()
    log("onconsoleclose: saveram")
    pcall(client.saveram)
end, "ap_flush_on_close")

event.onframeend(function()
    if file_exists(SHUTDOWN_FLAG) then
        pcall(os.remove, SHUTDOWN_FLAG)
        try_saveram_exit("flag")
    end
end, "ap_shutdown_flag_watch")
