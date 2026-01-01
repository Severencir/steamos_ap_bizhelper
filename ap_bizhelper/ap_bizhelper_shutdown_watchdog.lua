-- ap-bizhelper shutdown watchdog (test build)
local log_path = "ap_bizhelper_lua_shutdown_log.txt"
local flag_paths = {
    "ap_bizhelper_shutdown.flag",
    "./ap_bizhelper_shutdown.flag",
    "ap_bizhelper/ap_bizhelper_shutdown.flag",
}
local burst_count = 12
local post_exit_burst = 4
local tick_interval_seconds = 1
local tick_counter = 0
local iteration_counter = 0
local last_tick_time = nil
local yield_failure_logged = false

local function safe_timestamp()
    if not os or not os.date then
        return nil
    end
    local ok, value = pcall(os.date, "%Y-%m-%d %H:%M:%S")
    if ok then
        return value
    end
    return nil
end

local function log_line(message)
    local file = io.open(log_path, "a")
    if not file then
        return
    end
    file:write(message .. "\n")
    file:close()
end

local function safe_yield()
    if emu and emu.yield then
        local ok = pcall(emu.yield)
        if ok then
            return true
        end
    end
    if emu and emu.frameadvance then
        local ok = pcall(emu.frameadvance)
        if ok then
            return true
        end
    end
    if not yield_failure_logged then
        yield_failure_logged = true
        log_line("yield failed; no emu.yield or emu.frameadvance")
    end
    return false
end

local function file_exists(path)
    local file = io.open(path, "rb")
    if file then
        file:close()
        return true
    end
    return false
end

local function find_shutdown_flag()
    for _, path in ipairs(flag_paths) do
        if file_exists(path) then
            return path
        end
    end
    return nil
end

local function safe_saveram()
    if client and client.saveram then
        pcall(client.saveram)
    end
end

local function safe_exit()
    if client and client.exit then
        pcall(client.exit)
    end
end

local function saveram_burst(count)
    for i = 1, count do
        log_line("saveram attempt " .. tostring(i))
        safe_saveram()
        safe_yield()
    end
end

local function handle_shutdown(path)
    local stamp = safe_timestamp()
    if stamp then
        log_line("FLAG FOUND: " .. path .. " " .. stamp)
    else
        log_line("FLAG FOUND: " .. path)
    end

    pcall(os.remove, path)

    saveram_burst(burst_count)
    log_line("exit request")
    safe_exit()

    for i = 1, post_exit_burst do
        log_line("post-exit saveram attempt " .. tostring(i))
        safe_saveram()
        safe_yield()
    end
end

local function heartbeat_if_needed()
    local now = os and os.time and os.time() or nil
    if last_tick_time == nil then
        last_tick_time = now
    end

    local should_tick = false
    if now and last_tick_time then
        should_tick = (now - last_tick_time) >= tick_interval_seconds
    else
        iteration_counter = iteration_counter + 1
        if iteration_counter % 60 == 0 then
            should_tick = true
        end
    end

    if should_tick then
        tick_counter = tick_counter + 1
        local stamp = safe_timestamp()
        if stamp then
            log_line("tick " .. tostring(tick_counter) .. " " .. stamp)
        else
            log_line("tick " .. tostring(tick_counter))
        end
        last_tick_time = now
    end
end

local function check_shutdown_flag()
    local flag = find_shutdown_flag()
    if flag then
        handle_shutdown(flag)
    end
end

local function on_frame_event()
    check_shutdown_flag()
end

local function on_input_event()
    check_shutdown_flag()
end

local function on_console_close()
    log_line("onconsoleclose saveram")
    safe_saveram()
end

local start_stamp = safe_timestamp()
if start_stamp then
    log_line("=== watchdog start " .. start_stamp .. " ===")
else
    log_line("=== watchdog start ===")
end

if event and event.onconsoleclose then
    pcall(event.onconsoleclose, on_console_close, "ap_bizhelper_shutdown_consoleclose")
end

if event and event.onframeend then
    pcall(event.onframeend, on_frame_event, "ap_bizhelper_shutdown_frameend")
end

if event and event.oninputpoll then
    pcall(event.oninputpoll, on_input_event, "ap_bizhelper_shutdown_inputpoll")
end

while true do
    heartbeat_if_needed()
    check_shutdown_flag()
    safe_yield()
end
