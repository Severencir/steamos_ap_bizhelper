-- FIND_BASE + check SaveRAM-named listing under Base, and decide migration vs connector.
-- Steam Deck / SteamOS friendly symlink detection (POSIX test -L + readlink).
-- Also detects BROKEN symlinks and treats them like "no symlink" (migration branch).
-- Tries once per second (wall clock) for up to 10 seconds. Uses emu.yield() to avoid freezing.

local Path = luanet.import_type("System.IO.Path")

local function _default_log_file()
    local home = os.getenv("HOME") or ""
    local base = ""
    if home ~= "" then
        base = home .. "/.local/share/ap-bizhelper/logs/lua_entry"
    else
        base = "./logs/lua_entry"
    end
    pcall(os.execute, string.format("mkdir -p %q", base))

    local ts = os.getenv("AP_BIZHELPER_LOG_TIMESTAMP") or os.date("%Y-%m-%d_%H-%M-%S")
    local run_id = os.getenv("AP_BIZHELPER_LOG_RUN_ID") or "unknown"
    return string.format("%s/bizhawk-lua-entry_%s_%s.log", base, ts, run_id)
end

local LOG_FILE = _default_log_file()
local function log(msg)
    if console ~= nil and type(console.log) == "function" then
        pcall(console.log, "[ap_bizhelper] " .. tostring(msg))
    end

    local ok, file = pcall(io.open, LOG_FILE, "a")
    if not ok or not file then
        return
    end
    pcall(file.write, file, msg .. "\n")
    pcall(file.close, file)
end

log("migration launcher loaded; log file: " .. tostring(LOG_FILE))

local function find_entry(systemKey, typeKey)
    local cfg = client.getconfig()
    if not cfg or not cfg.PathEntries or not cfg.PathEntries.Paths then
        return nil
    end

    local paths = cfg.PathEntries.Paths
    for i = 0, paths.Count - 1 do
        local e = paths[i]
        if e and e.System == systemKey and e.Type == typeKey then
            return tostring(e.Path), systemKey, typeKey
        end
    end
    return nil
end

local function get_path_for(sysid, typeKey)
    return find_entry(sysid, typeKey)
        or find_entry(sysid .. "_NULL", typeKey)
        or find_entry("Global_NULL", typeKey)
end

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

local function _exists(path)
    local cmd = string.format("test -e %q", path)
    return _shell_ok(os.execute(cmd))
end

local function _helper_path()
    local helper_path = os.getenv("SAVE_MIGRATION_HELPER_PATH")
    if helper_path and helper_path ~= "" then
        return helper_path
    end
    return nil
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

local function _run_connector()
    local connector_path = os.getenv("AP_BIZHELPER_CONNECTOR_PATH")
    if not connector_path or connector_path == "" then
        error("AP_BIZHELPER_CONNECTOR_PATH not set")
    end

    log("AP_BIZHELPER_CONNECTOR_PATH=" .. tostring(connector_path))

    local entry_dir = entry_script_dir()
    connector_path = normalize_path(connector_path)
    if not is_abs(connector_path) then
        connector_path = join(entry_dir, connector_path)
    end

    local connector_dir = dirname(connector_path)
    _G.AP_BIZHELPER_CONNECTOR_DIR = connector_dir
    log("connector_dir=" .. tostring(connector_dir))

    package.path = join(connector_dir, "?.lua") .. ";" .. join(connector_dir, "?/init.lua") .. ";" .. package.path
    package.cpath = join(connector_dir, "?.dll") .. ";" .. join(connector_dir, "?.so") .. ";" .. package.cpath

    local old_cwd = get_cwd()
    if old_cwd then
        local ok = set_cwd(connector_dir)
        if not ok then
            log("could not set CWD; luanet not available")
        end
    end

    local connector_file = connector_path:match("([^/]+)$") or connector_path
    log("executing connector: " .. tostring(connector_file))
    local ok, err = pcall(dofile, connector_file)

    if old_cwd then
        pcall(set_cwd, old_cwd)
    end

    if not ok then
        log("connector error: " .. tostring(err))
        error(err)
    end

    log("connector finished successfully")
end

local function sh_quote(s)
    return "'" .. tostring(s):gsub("'", "'\\''") .. "'"
end

local function symlink_status_linux(p)
    local script = [[
        p="$1"
        if [ -L "$p" ]; then
            t="$(readlink "$p" 2>/dev/null || true)"
            d="$(dirname "$p")"
            if [ -z "$t" ]; then
                echo "BROKEN||"
                exit 0
            fi
            case "$t" in
                /*) a="$t" ;;
                *)  a="$d/$t" ;;
            esac
            if [ -e "$a" ]; then
                echo "OK|$t|$a"
            else
                echo "BROKEN|$t|$a"
            fi
        else
            echo "NO||"
        fi
    ]]
    local cmd = "sh -lc " .. sh_quote(script) .. " -- " .. sh_quote(p)
    local h = io.popen(cmd)
    if not h then
        return "NO", nil, nil
    end
    local out = (h:read("*a") or ""):gsub("%s+$", "")
    h:close()

    local status, target, abs = out:match("^(%u+)%|(.-)%|(.*)$")
    if not status then
        status, target, abs = out:match("^(%u+)%|%|(.*)$")
        if status and target == "" then
            target = nil
        end
    end
    if status == "NO" then
        return "NO", nil, nil
    end
    if target == "" then
        target = nil
    end
    if abs == "" then
        abs = nil
    end
    return status or "NO", target, abs
end

log("startup: cwd=" .. tostring(get_cwd() or "(unknown)") .. ", entry_dir=" .. tostring(entry_script_dir()))

local deadline = os.time() + 10
local next_t = os.time() + 1

while os.time() < deadline do
    local now = os.time()
    if now >= next_t then
        next_t = next_t + 1

        local sys = emu.getsystemid()
        if sys == "NULL" then
            log("[warn] no ROM/core loaded yet; can't resolve Base")
        else
            local base = get_path_for(sys, "Base")
            if base then
                local baseAbs = tostring(Path.GetFullPath(base))

                local saveramPath = get_path_for(sys, "Save RAM")
                if saveramPath then
                    local saveramName = tostring(Path.GetFileName(tostring(saveramPath)))
                    local candidate = tostring(Path.Combine(baseAbs, saveramName))

                    local status, target, abs = symlink_status_linux(candidate)

                    if status == "OK" then
                        log(string.format("%s - save migration detected", candidate))
                        log("starting connector")
                        _run_connector()
                        log("=== MIGRATION CHECK DONE (connector) ===")
                        return
                    else
                        log(string.format("%s - save migration needed", candidate))
                        if status == "BROKEN" then
                            log(string.format("[warn] broken symlink target=%s resolved=%s", tostring(target), tostring(abs)))
                        end
                        log("starting migration helper")
                        log("closing emuhawk pending helper relaunch")
                        _launch_helper(sys)
                        if client ~= nil and type(client.exit) == "function" then
                            pcall(client.exit)
                        end
                        log("=== MIGRATION CHECK DONE (migration) ===")
                        return
                    end
                else
                    log("[warn] couldn't find a 'Save RAM' path entry to compare against")
                end
            else
                log(string.format("[warn] sys=%s Base not found in PathEntries yet", sys))
            end
        end
    end

    if emu ~= nil and type(emu.yield) == "function" then
        emu.yield()
    elseif client ~= nil and type(client.sleep) == "function" then
        client.sleep(16)
    end
end

log("=== MIGRATION CHECK DONE (timeout) ===")
