local LOG_FILE = "ap_bizhelper_lua_entry_log.txt"

local function log(msg)
    local ok, file = pcall(io.open, LOG_FILE, "a")
    if not ok or not file then
        return
    end
    pcall(file.write, file, msg .. "\n")
    pcall(file.close, file)
end

-- -------- Path helpers --------
local function normalize_path(p)
    return (p or ""):gsub("\\", "/")
end

local function is_abs(p)
    p = normalize_path(p)
    return p:match("^/") or p:match("^[A-Za-z]:/") or p:match("^//")
end

local function dirname(p)
    p = normalize_path(p)
    return p:match("^(.*)/[^/]*$") or "."
end

local function join(a, b)
    a, b = normalize_path(a), normalize_path(b)
    if a == "" or a == "." then
        return b
    end
    if a:sub(-1) == "/" then
        return a .. b
    end
    return a .. "/" .. b
end

local function entry_script_dir()
    local src = debug.getinfo(1, "S").source
    if type(src) ~= "string" then
        return "."
    end
    if src:sub(1, 1) == "@" then
        src = src:sub(2)
    end
    src = normalize_path(src)
    return dirname(src)
end

local function get_cwd()
    if type(luanet) == "table" and type(luanet.import_type) == "function" then
        local Env = luanet.import_type("System.Environment")
        return tostring(Env.CurrentDirectory)
    end
    return nil
end

local function set_cwd(p)
    if type(luanet) == "table" and type(luanet.import_type) == "function" then
        local Env = luanet.import_type("System.Environment")
        -- .NET is happiest with backslashes on Windows.
        Env.CurrentDirectory = normalize_path(p):gsub("/", "\\")
        return true
    end
    return false
end

local function _shell_ok(result)
    if result == true or result == 0 then
        return true
    end
    if type(result) == "number" then
        return result == 0
    end
    return false
end

local function _read_first_line(path)
    local ok, file = pcall(io.open, path, "r")
    if not ok or not file then
        return nil
    end
    local line = file:read("*l")
    file:close()
    if not line or line == "" then
        return nil
    end
    return line
end

local function _exists(path)
    local cmd = string.format("test -e %q", path)
    return _shell_ok(os.execute(cmd))
end

local function _is_symlink(path)
    local cmd = string.format("test -L %q", path)
    return _shell_ok(os.execute(cmd))
end

local function _readlink(path)
    local cmd = string.format("readlink -f %q", path)
    local pipe = io.popen(cmd, "r")
    if not pipe then
        return nil
    end
    local out = pipe:read("*l")
    pipe:close()
    if not out or out == "" then
        return nil
    end
    return out
end

local function _system_dir_name()
    if type(client) == "table" and type(client.getsystemid) == "function" then
        local id = client.getsystemid()
        if id and id ~= "" then
            return tostring(id)
        end
    end
    if type(gameinfo) == "table" and type(gameinfo.getsystemid) == "function" then
        local id = gameinfo.getsystemid()
        if id and id ~= "" then
            return tostring(id)
        end
    end
    if type(emu) == "table" and type(emu.getsystemid) == "function" then
        local id = emu.getsystemid()
        if id and id ~= "" then
            return tostring(id)
        end
    end
    return nil
end

local function _helper_path()
    local home = os.getenv("HOME") or ""
    if home == "" then
        return nil
    end
    local config_dir = join(home, ".config/ap_bizhelper")
    local helper_path = _read_first_line(join(config_dir, "helper_path.txt"))
    return helper_path
end

local function _launch_helper(system_dir)
    local helper = _helper_path()
    if not helper then
        error("Save migration helper path not configured")
    end
    local cmd = string.format("%q %q &", helper, system_dir)
    log("launching save migration helper: " .. cmd)
    os.execute(cmd)
end

local entry_dir = entry_script_dir()
local system_dir = _system_dir_name()
if not system_dir then
    error("Could not determine BizHawk system dir name")
end

local save_ram = join(join(entry_dir, system_dir), "SaveRAM")
local save_ram_is_symlink = _is_symlink(save_ram)
local save_ram_target = save_ram_is_symlink and _readlink(save_ram) or nil

if not save_ram_is_symlink or not save_ram_target or not _exists(save_ram_target) then
    log("SaveRAM not linked or invalid; invoking migration helper for " .. system_dir)
    _launch_helper(system_dir)
    if type(client) == "table" and type(client.exit) == "function" then
        pcall(client.exit)
    end
    return
end

local connector_path = os.getenv("AP_BIZHELPER_CONNECTOR_PATH")
if not connector_path or connector_path == "" then
    error("AP_BIZHELPER_CONNECTOR_PATH not set")
end

connector_path = normalize_path(connector_path)
if not is_abs(connector_path) then
    -- Interpret a relative connector path as relative to the entry script directory.
    connector_path = join(entry_dir, connector_path)
end

local connector_dir = dirname(connector_path)
_G.AP_BIZHELPER_CONNECTOR_DIR = connector_dir

-- Ensure require() can find Lua modules and native modules next to the connector.
package.path = join(connector_dir, "?.lua") .. ";" .. join(connector_dir, "?/init.lua") .. ";" .. package.path
package.cpath = join(connector_dir, "?.dll") .. ";" .. join(connector_dir, "?.so") .. ";" .. package.cpath

-- Temporarily set process CWD to the connector directory so that any relative
-- file access (Lua or native DLL dependency loads) behaves as if the connector
-- was launched directly.
local old_cwd = get_cwd()
if old_cwd then
    local ok = set_cwd(connector_dir)
    if not ok then
        log("could not set CWD; luanet not available")
    end
end

-- Execute the connector by filename after switching CWD to its directory.
-- This matches the common "run from within the connector folder" expectation.
local connector_file = connector_path:match("([^/]+)$") or connector_path
local ok, err = pcall(dofile, connector_file)

if old_cwd then
    pcall(set_cwd, old_cwd)
end

if not ok then
    error(err)
end
