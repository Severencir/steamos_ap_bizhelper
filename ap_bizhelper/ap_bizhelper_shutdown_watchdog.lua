local SHUTDOWN_FLAG = "ap_bizhelper_shutdown.flag"

local function file_exists(path)
    local file = io.open(path, "rb")
    if file then
        file:close()
        return true
    end
    return false
end

local function flush_and_exit()
    pcall(client.saveram)
    pcall(client.exit)
end

event.onconsoleclose(function()
    pcall(client.saveram)
end, "ap_flush_on_close")

event.onframeend(function()
    if file_exists(SHUTDOWN_FLAG) then
        pcall(os.remove, SHUTDOWN_FLAG)
        flush_and_exit()
    end
end, "ap_shutdown_flag_watch")
