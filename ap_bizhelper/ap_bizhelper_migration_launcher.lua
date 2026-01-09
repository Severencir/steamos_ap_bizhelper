-- FIND_BASE + check SaveRAM-named listing under Base, and decide migration vs connector.
-- Steam Deck / SteamOS friendly symlink detection (POSIX test -L + readlink).
-- Also detects BROKEN symlinks and treats them like "no symlink" (migration branch).
-- Tries once per second (wall clock) for up to 10 seconds. Uses emu.yield() to avoid freezing.

local Path = luanet.import_type("System.IO.Path")

console.log("=== MIGRATION CHECK v3 START ===")

local function find_entry(systemKey, typeKey)
local cfg = client.getconfig()
if not cfg or not cfg.PathEntries or not cfg.PathEntries.Paths then return nil end

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

            local function sh_quote(s)
            return "'" .. tostring(s):gsub("'", "'\\''") .. "'"
            end

            local function symlink_status_linux(p)
            -- Returns:
            --   status: "OK" | "BROKEN" | "NO"
            --   target: string|nil   (as returned by readlink)
            --   abs:    string|nil   (resolved absolute-ish path used for existence check)
            --
            -- Notes:
            -- - Uses test -L, so it detects symlinks even if broken.
            -- - For relative targets, resolves relative to the symlink's directory.
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
                         * )  a="$d/$t" ;;
                         *  esac
                         *  if [ -e "$a" ]; then
                         *    echo "OK|$t|$a"
                         *  else
                         *    echo "BROKEN|$t|$a"
                         *  fi
                         * else
                         *  echo "NO||"
                         * fi
            ]]
            local cmd = "sh -lc " .. sh_quote(script) .. " -- " .. sh_quote(p)
            local h = io.popen(cmd)
            if not h then return "NO", nil, nil end
                local out = (h:read("*a") or ""):gsub("%s+$", "")
                h:close()

                local status, target, abs = out:match("^(%u+)%|(.-)%|(.*)$")
                if not status then
                    -- handle "NO||" / "BROKEN||" etc
                    status, target, abs = out:match("^(%u+)%|%|(.*)$")
                    if status and target == "" then target = nil end
                        end
                        if status == "NO" then return "NO", nil, nil end
                            if target == "" then target = nil end
                                if abs == "" then abs = nil end
                                    return status or "NO", target, abs
                                    end

                                    local deadline = os.time() + 10
                                    local next_t = os.time() + 1

                                    while os.time() < deadline do
                                        local now = os.time()
                                        if now >= next_t then
                                            next_t = next_t + 1

                                            local sys = emu.getsystemid()
                                            if sys == "NULL" then
                                                console.log("[warn] no ROM/core loaded yet; can't resolve Base")
                                                else
                                                    local base = get_path_for(sys, "Base")
                                                    if base then
                                                        local baseAbs = tostring(Path.GetFullPath(base))

                                                        local saveramPath = get_path_for(sys, "Save RAM")
                                                        if saveramPath then
                                                            -- saveramPath might be "./SaveRAM" -> name should be "SaveRAM"
                                                            local saveramName = tostring(Path.GetFileName(tostring(saveramPath)))
                                                            local candidate = tostring(Path.Combine(baseAbs, saveramName)) -- expected path inside Base

                                                            local status, target, abs = symlink_status_linux(candidate)

                                                            if status == "OK" then
                                                                -- Case 1: Symlink exists AND is not broken
                                                                console.log(string.format("%s - save migration detected", candidate))
                                                                console.log("starting connector")
                                                                console.log("=== MIGRATION CHECK v3 DONE (connector) ===")
                                                                return
                                                                else
                                                                    -- Case 2: No symlink OR broken symlink OR missing listing
                                                                    -- (broken symlink explicitly treated here)
                                                                    console.log(string.format("%s - save migration needed", candidate))
                                                                    if status == "BROKEN" then
                                                                        -- extra info, still same branch
                                                                        console.log(string.format("[warn] broken symlink target=%s resolved=%s", tostring(target), tostring(abs)))
                                                                        end
                                                                        console.log("starting migration helper")
                                                                        console.log("closing emuhawk pending helper relaunch")
                                                                        console.log("=== MIGRATION CHECK v3 DONE (migration) ===")
                                                                        return
                                                                        end
                                                                        else
                                                                            console.log("[warn] couldn't find a 'Save RAM' path entry to compare against")
                                                                            end
                                                                            else
                                                                                console.log(string.format("[warn] sys=%s Base not found in PathEntries yet", sys))
                                                                                end
                                                                                end
                                                                                end

                                                                                emu.yield()
                                                                                end

                                                                                console.log("=== MIGRATION CHECK v3 DONE (timeout) ===")
