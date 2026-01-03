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

local function normalize_path(path)
    return (path:gsub("\\", "/"))
end

local function dirname(path)
    local normalized = normalize_path(path)
    local dir = normalized:match("^(.*)/[^/]*$") or "."
    if dir == "" then
        dir = "."
    end
    return dir
end

local function prepend_path(current, prefix)
    if not prefix or prefix == "" then
        return current
    end
    if current and current ~= "" then
        return prefix .. ";" .. current
    end
    return prefix
end

local function prepend_env_path(current, prefix, separator)
    if not prefix or prefix == "" then
        return current
    end
    if current and current ~= "" then
        return prefix .. separator .. current
    end
    return prefix
end

local connector_path = os.getenv("AP_BIZHELPER_CONNECTOR_PATH")
if not connector_path or connector_path == "" then
    error("AP_BIZHELPER_CONNECTOR_PATH not set")
end

connector_path = normalize_path(connector_path)
local connector_dir = dirname(connector_path)
local lua_path_prefix = connector_dir .. "/?.lua;" .. connector_dir .. "/?/init.lua"
local lua_cpath_prefix = connector_dir .. "/?.dll;" .. connector_dir .. "/?.so"

package.path = prepend_path(package.path, lua_path_prefix)
package.cpath = prepend_path(package.cpath, lua_cpath_prefix)

if os.setenv then
    local current_path = os.getenv("PATH") or ""
    local path_separator = current_path:find(";") and ";" or ":"
    os.setenv("PATH", prepend_env_path(current_path, connector_dir, path_separator))
    os.setenv("LUA_PATH", prepend_path(os.getenv("LUA_PATH"), lua_path_prefix))
    os.setenv("LUA_CPATH", prepend_path(os.getenv("LUA_CPATH"), lua_cpath_prefix))
end

log(
    "connector_dir=" .. connector_dir .. " package.path=" .. tostring(package.path) .. " package.cpath="
        .. tostring(package.cpath)
)

dofile(connector_path)
