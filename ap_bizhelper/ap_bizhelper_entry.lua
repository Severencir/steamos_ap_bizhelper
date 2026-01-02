local LOG_FILE = "ap_bizhelper_lua_entry_log.txt"

local function log(msg)
    local ok, file = pcall(io.open, LOG_FILE, "a")
    if not ok or not file then
        return
    end
    pcall(file.write, file, msg .. "\n")
    pcall(file.close, file)
end

local watchdog_ok, watchdog_err = pcall(dofile, "ap_bizhelper_shutdown_watchdog.lua")
if not watchdog_ok then
    log("watchdog load failed: " .. tostring(watchdog_err))
end

local connector_path = os.getenv("AP_BIZHELPER_CONNECTOR_PATH")
if not connector_path or connector_path == "" then
    error("AP_BIZHELPER_CONNECTOR_PATH not set")
end

connector_path = connector_path:gsub("\\", "/")
local connector_dir = connector_path:match("^(.*)/[^/]+$")
if connector_dir and connector_dir ~= "" then
    package.cpath = connector_dir .. "/?.dll;" .. package.cpath
end
dofile(connector_path)
