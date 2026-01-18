#!/usr/bin/env python3
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Optional


def _prepend_helpers_lib_path() -> None:
    script_path = Path(__file__).resolve()
    helpers_root = script_path.parent
    helpers_lib = helpers_root / "lib"
    if not helpers_lib.is_dir():
        return
    helpers_lib_str = helpers_lib.as_posix()
    if helpers_lib_str not in sys.path:
        sys.path.insert(0, helpers_lib_str)


_prepend_helpers_lib_path()

from ap_bizhelper.ap_bizhelper_config import (  # noqa: E402
    get_path_setting,
    load_settings,
    save_settings,
)
from ap_bizhelper.constants import (  # noqa: E402
    AP_BIZHELPER_CONNECTOR_PATH_ENV,
    AP_BIZHELPER_EMUHAWK_PID_ENV,
    BIZHAWK_ENTRY_LUA_FILENAME,
    BIZHAWK_EXE_KEY,
    BIZHAWK_HELPERS_ROOT_KEY,
    BIZHAWK_LAST_LAUNCH_ARGS_KEY,
    BIZHAWK_LAST_PID_KEY,
    BIZHAWK_MIGRATION_PID_KEY,
    BIZHAWK_RUNTIME_ROOT_KEY,
    LOG_PREFIX,
    SAVE_MIGRATION_HELPER_PATH_KEY,
)
from ap_bizhelper.dialogs import fallback_error_dialog  # noqa: E402
from ap_bizhelper.logging_utils import (  # noqa: E402
    LOG_LEVEL_ERROR,
    LOG_LEVEL_WARNING,
    RUNNER_LOG_ENV,
    create_component_logger,
)

COMMAND_LOCATION = "command"
CONNECTOR_GENERIC = "connector_bizhawk_generic.lua"
CONNECTOR_SNI = "Connector.lua"
CONNECTOR_SNI_FALLBACK = "connector.lua"
ARCHIPELAGO_ROOT_DIRNAME = "Archipelago"
ARCHIPELAGO_OPT_DIRNAME = "opt"
ARCHIPELAGO_SNI_DIRNAME = "SNI"
ARCHIPELAGO_SNI_LUA_DIRNAME = "lua"
DEFAULT_MOUNT_PREFIX = ".mount_"
ENV_CONFIG_LOCATION = "env-config"
LUA_ARG_PREFIX = "--lua="
LUA_EXTENSION = ".lua"
NO_AP_FLAG = "--noap"
OPTION_PREFIX = "-"
RUNNER_ERROR_TITLE = "BizHawk runner error"
RUNNER_MAIN_CONTEXT = "runner-main"
SETTINGS_LOAD_LOCATION = "settings-load"
SETTINGS_LOOKUP_LOCATION = "settings-lookup"
SETTINGS_SAVE_LOCATION = "settings-save"
RUNNER_LOGGER = create_component_logger("bizhawk-runner", env_var=RUNNER_LOG_ENV, subdir="runner")

EMUHAWK_PID_DISCOVERY_ATTEMPTS = 12
EMUHAWK_PID_DISCOVERY_SLEEP_SECONDS = 0.25


def _show_error_dialog(msg: str) -> None:
    RUNNER_LOGGER.log(f"Error dialog requested: {msg}", level=LOG_LEVEL_ERROR, include_context=True)
    fallback_error_dialog(msg, title=RUNNER_ERROR_TITLE, logger=RUNNER_LOGGER)


def _load_settings_safe() -> dict[str, Any]:
    try:
        return load_settings()
    except Exception as exc:
        RUNNER_LOGGER.log(
            f"Failed to load settings: {exc}\n{traceback.format_exc()}",
            level=LOG_LEVEL_ERROR,
            include_context=True,
            location=SETTINGS_LOAD_LOCATION,
        )
        return {}


def get_env_or_config(var: str, settings: dict[str, Any]) -> Optional[str]:
    """Read config from the environment or stored settings."""
    value = os.environ.get(var)
    if value:
        RUNNER_LOGGER.log(
            f"Using environment override for {var}={value}",
            include_context=True,
            location=ENV_CONFIG_LOCATION,
        )
        return value

    if var == BIZHAWK_EXE_KEY:
        value = settings.get(var, "")
    elif var in (
        BIZHAWK_HELPERS_ROOT_KEY,
        BIZHAWK_RUNTIME_ROOT_KEY,
        SAVE_MIGRATION_HELPER_PATH_KEY,
    ):
        value = get_path_setting(settings, var)
    else:
        RUNNER_LOGGER.log(
            f"No settings mapping configured for {var}.",
            level=LOG_LEVEL_ERROR,
            include_context=True,
            location=SETTINGS_LOOKUP_LOCATION,
        )
        return None

    if value not in (None, "", Path()):
        RUNNER_LOGGER.log(
            f"Loaded {var} from settings: {value}",
            include_context=True,
            location=SETTINGS_LOOKUP_LOCATION,
        )
        return str(value)

    RUNNER_LOGGER.log(
        f"No configured value found for {var}.",
        level=LOG_LEVEL_ERROR,
        include_context=True,
        location=SETTINGS_LOOKUP_LOCATION,
    )
    return None


def ensure_bizhawk_exe(settings: dict[str, Any]) -> Path:
    exe = get_env_or_config(BIZHAWK_EXE_KEY, settings)
    if not exe or not Path(exe).is_file():
        fallback_error_dialog(
            f"{LOG_PREFIX} BIZHAWK_EXE is not set or not a file; cannot launch BizHawk.",
            title=RUNNER_ERROR_TITLE,
            logger=RUNNER_LOGGER,
        )
        sys.exit(1)
    RUNNER_LOGGER.log(f"Resolved BizHawk launcher script: {exe}", include_context=True)
    return Path(exe)


def _runtime_root(settings: dict[str, Any]) -> Path:
    value = get_env_or_config(BIZHAWK_RUNTIME_ROOT_KEY, settings)
    if not value:
        return Path()
    return Path(os.path.expanduser(value))


def _helpers_root(settings: dict[str, Any]) -> Path:
    value = get_env_or_config(BIZHAWK_HELPERS_ROOT_KEY, settings)
    if not value:
        return Path()
    return Path(os.path.expanduser(value))


def _runtime_paths(runtime_root: Path) -> dict[str, Path]:
    return {
        "mono": runtime_root / "usr" / "bin" / "mono",
        "lua": runtime_root / "usr" / "bin" / "lua",
        "mono_config": runtime_root / "etc" / "mono" / "config",
        "libgdiplus": runtime_root / "usr" / "lib" / "libgdiplus.so",
        "libgdiplus_alt": runtime_root / "usr" / "lib" / "libgdiplus.so.0",
        "libgdiplus64": runtime_root / "usr" / "lib64" / "libgdiplus.so",
        "libgdiplus64_alt": runtime_root / "usr" / "lib64" / "libgdiplus.so.0",
    }


def _validate_runtime(runtime_root: Path) -> None:
    paths = _runtime_paths(runtime_root)
    missing = []
    if not paths["mono"].is_file():
        missing.append("mono")
    if not paths["lua"].is_file():
        missing.append("lua")
    if not paths["mono_config"].is_file():
        missing.append("mono config")
    libgdiplus_ok = any(
        candidate.is_file()
        for candidate in (
            paths["libgdiplus"],
            paths["libgdiplus_alt"],
            paths["libgdiplus64"],
            paths["libgdiplus64_alt"],
        )
    )
    if not libgdiplus_ok:
        missing.append("libgdiplus")

    if missing:
        fallback_error_dialog(
            "BizHawk runtime dependencies are missing from runtime_root:\n"
            f"{runtime_root}\n\nMissing: {', '.join(missing)}",
            title=RUNNER_ERROR_TITLE,
            logger=RUNNER_LOGGER,
        )
        sys.exit(1)


def _parse_lua_arg(ap_lua_arg: str | None) -> Optional[Path]:
    if not ap_lua_arg:
        return None

    if ap_lua_arg.startswith(LUA_ARG_PREFIX):
        lua_path = ap_lua_arg[len(LUA_ARG_PREFIX) :]
    else:
        lua_path = ap_lua_arg

    return Path(lua_path)


def parse_args(argv):
    """Return (rom_path, ap_lua_arg, emu_args_no_lua, no_ap)."""
    rom_path = None
    ap_lua_arg = None
    emu_args = []
    no_ap = False

    i = 0
    n = len(argv)
    while i < n:
        arg = argv[i]
        i += 1

        if arg == NO_AP_FLAG:
            no_ap = True
            continue
        if arg.startswith(LUA_ARG_PREFIX):
            ap_lua_arg = arg
        elif arg == "--lua":
            if i < n:
                ap_lua_arg = f"{LUA_ARG_PREFIX}{argv[i]}"
                i += 1
        else:
            if rom_path is None and not arg.startswith(OPTION_PREFIX):
                rom_path = arg
            else:
                emu_args.append(arg)

    # Recovery: if we didn't capture ROM earlier, try first non-option in emu_args
    if rom_path is None:
        for idx, a in enumerate(emu_args):
            if not a.startswith(OPTION_PREFIX):
                rom_path = a
                emu_args = emu_args[:idx] + emu_args[idx + 1 :]
                break

    RUNNER_LOGGER.log(
        f"Parsed args rom={rom_path}, ap_lua_arg={ap_lua_arg}, emu_args={emu_args}, no_ap={no_ap}",
        include_context=True,
        location="parse-args",
    )
    return rom_path, ap_lua_arg, emu_args, no_ap


def _archipelago_mount_candidates() -> list[Path]:
    mounts = []
    tmp_dir = Path("/tmp")
    if not tmp_dir.is_dir():
        return mounts
    for entry in tmp_dir.iterdir():
        if not entry.is_dir():
            continue
        if not entry.name.startswith(DEFAULT_MOUNT_PREFIX):
            continue
        mounts.append(entry)
    return mounts


def _archipelago_root_candidates(mount: Path) -> list[Path]:
    return [
        mount / ARCHIPELAGO_OPT_DIRNAME / ARCHIPELAGO_ROOT_DIRNAME,
        mount / ARCHIPELAGO_ROOT_DIRNAME,
    ]


def _expected_connector_path(mount: Path, connector_name: str) -> list[Path]:
    return [candidate / connector_name for candidate in _archipelago_root_candidates(mount)]


def _expected_sni_path(mount: Path) -> list[Path]:
    return [
        candidate / ARCHIPELAGO_SNI_DIRNAME / "sni"
        for candidate in _archipelago_root_candidates(mount)
    ]


def _expected_sni_connector_paths(mount: Path) -> list[Path]:
    return [
        candidate / ARCHIPELAGO_SNI_DIRNAME / ARCHIPELAGO_SNI_LUA_DIRNAME / name
        for candidate in _archipelago_root_candidates(mount)
        for name in (CONNECTOR_SNI, CONNECTOR_SNI_FALLBACK)
    ]


def _find_archipelago_mount() -> Optional[Path]:
    candidates = _archipelago_mount_candidates()
    filtered = []
    for candidate in candidates:
        name = candidate.name.lower()
        has_hint = "archip" in name
        has_connector = any(
            path.is_file() for path in _expected_connector_path(candidate, CONNECTOR_GENERIC)
        )
        has_sni = any(path.is_file() for path in _expected_sni_connector_paths(candidate))
        has_root = any(path.is_dir() for path in _archipelago_root_candidates(candidate))
        if has_hint or has_connector or has_sni or has_root:
            filtered.append(candidate)

    if not filtered:
        return None

    filtered.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return filtered[0]


def _resolve_connector_from_arg(mount: Path, ap_lua_arg: str | None) -> Path:
    lua_path = _parse_lua_arg(ap_lua_arg)
    if not lua_path:
        raise FileNotFoundError(
            "Connector not provided by Archipelago; launch BizHawk from Archipelago for non-SFC ROMs."
        )

    if lua_path.suffix:
        if lua_path.suffix.lower() != LUA_EXTENSION:
            raise FileNotFoundError(f"Connector was not a Lua file: {lua_path}")
        candidate_paths = [lua_path]
    else:
        candidate_paths = [lua_path.with_suffix(LUA_EXTENSION), lua_path]

    candidates: list[Path] = []
    if lua_path.is_absolute():
        candidates.extend(candidate_paths)
    else:
        for candidate_root in _archipelago_root_candidates(mount):
            for path in candidate_paths:
                candidates.append(candidate_root / path)
        for path in candidate_paths:
            candidates.append(Path.cwd() / path)

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"Connector not found: {lua_path}")


def _resolve_sni_connector(mount: Path) -> Optional[Path]:
    for candidate in _expected_sni_connector_paths(mount):
        if candidate.is_file():
            return candidate
    return None


def _resolve_sni(mount: Path) -> Optional[Path]:
    for candidate in _expected_sni_path(mount):
        if candidate.is_file():
            return candidate
    return None


def _resolve_lua_arg_path(ap_lua_arg: str | None) -> Optional[Path]:
    lua_path = _parse_lua_arg(ap_lua_arg)
    if not lua_path:
        return None

    if lua_path.suffix:
        candidate_paths = [lua_path]
    else:
        candidate_paths = [lua_path.with_suffix(LUA_EXTENSION), lua_path]

    if lua_path.is_absolute():
        candidates = candidate_paths
    else:
        candidates = [Path.cwd() / candidate for candidate in candidate_paths]

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _launch_sni(sni_path: Path, env: dict[str, str]) -> None:
    RUNNER_LOGGER.log(f"Launching SNI: {sni_path}", include_context=True)
    try:
        subprocess.Popen(
            [str(sni_path)],
            cwd=str(sni_path.parent),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        fallback_error_dialog(
            f"Failed to launch SNI: {exc}",
            title=RUNNER_ERROR_TITLE,
            logger=RUNNER_LOGGER,
        )
        sys.exit(1)


def _build_runtime_env(runtime_root: Path, bizhawk_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    bin_path = runtime_root / "usr" / "bin"
    lib_path = runtime_root / "usr" / "lib"
    lib64_path = runtime_root / "usr" / "lib64"

    env["PATH"] = f"{bin_path}:{env.get('PATH', '')}"

    lib_paths = [str(lib_path)]
    if lib64_path.is_dir():
        lib_paths.append(str(lib64_path))
    if env.get("LD_LIBRARY_PATH"):
        lib_paths.append(env["LD_LIBRARY_PATH"])
    env["LD_LIBRARY_PATH"] = ":".join(lib_paths)

    env["MONO_CFG_DIR"] = str(runtime_root / "etc")
    env["MONO_CONFIG"] = str(runtime_root / "etc" / "mono" / "config")

    dll_dir = bizhawk_root / "dll"
    if dll_dir.is_dir():
        env["MONO_PATH"] = str(dll_dir)

    return env


def _update_state_settings(updates: dict[str, Any]) -> None:
    settings = _load_settings_safe()
    settings.update(updates)
    try:
        save_settings(settings)
    except Exception as exc:
        RUNNER_LOGGER.log(
            f"Failed to persist settings update {updates}: {exc}",
            level=LOG_LEVEL_WARNING,
            include_context=True,
            location=SETTINGS_SAVE_LOCATION,
        )


def _update_state_setting(key: str, value: Any) -> None:
    _update_state_settings({key: value})


def _stage_cached_launch(args: list[str]) -> None:
    _update_state_setting(BIZHAWK_LAST_LAUNCH_ARGS_KEY, args)


def _record_pid(pid: int) -> None:
    value = str(pid)
    _update_state_settings(
        {
            BIZHAWK_LAST_PID_KEY: value,
            BIZHAWK_MIGRATION_PID_KEY: value,
        }
    )


def _discover_emuhawk_pid(emuhawk_path: Path) -> Optional[int]:
    for attempt in range(EMUHAWK_PID_DISCOVERY_ATTEMPTS):
        try:
            output = subprocess.check_output(
                ["pgrep", "-f", str(emuhawk_path)],
                text=True,
            )
        except subprocess.CalledProcessError:
            output = ""
        except Exception as exc:
            RUNNER_LOGGER.log(
                f"Failed to discover EmuHawk pid: {exc}",
                level=LOG_LEVEL_WARNING,
                include_context=True,
                location="pid-discovery",
            )
            return None
        for line in output.splitlines():
            try:
                return int(line.strip())
            except ValueError:
                continue
        time.sleep(EMUHAWK_PID_DISCOVERY_SLEEP_SECONDS)
        RUNNER_LOGGER.log(
            f"Waiting for EmuHawk pid discovery attempt {attempt + 1}/{EMUHAWK_PID_DISCOVERY_ATTEMPTS}.",
            include_context=True,
            location="pid-discovery",
        )
    return None


def main(argv: list[str]) -> int:
    with RUNNER_LOGGER.context(RUNNER_MAIN_CONTEXT):
        RUNNER_LOGGER.log(
            "BizHawk runner starting.",
            include_context=True,
            location="startup",
        )
        try:
            settings = _load_settings_safe()
            bizhawk_exe = ensure_bizhawk_exe(settings)
            bizhawk_root = bizhawk_exe.parent

            runtime_root = _runtime_root(settings)
            if not runtime_root:
                fallback_error_dialog(
                    f"{LOG_PREFIX} BIZHAWK_RUNTIME_ROOT is not set; cannot launch BizHawk.",
                    title=RUNNER_ERROR_TITLE,
                    logger=RUNNER_LOGGER,
                )
                return 1
            _validate_runtime(runtime_root)

            original_args = list(argv[1:])
            rom_path, ap_lua_arg, emu_args, no_ap = parse_args(original_args)
            needs_archipelago = bool(rom_path or ap_lua_arg) and not no_ap
            rom_ext = Path(rom_path).suffix.lower().lstrip(".") if rom_path else ""
            wants_sni = rom_ext == "sfc"

            env = _build_runtime_env(runtime_root, bizhawk_root)
            entry_lua: Optional[Path] = None
            passthrough_lua_arg: Optional[str] = None
            helper_path = get_env_or_config(SAVE_MIGRATION_HELPER_PATH_KEY, settings)
            if helper_path:
                env[SAVE_MIGRATION_HELPER_PATH_KEY] = str(helper_path)
                RUNNER_LOGGER.log(
                    f"Using save migration helper path: {helper_path}",
                    include_context=True,
                    location=ENV_CONFIG_LOCATION,
                )
            else:
                RUNNER_LOGGER.log(
                    "Save migration helper path not configured; migration launcher may fail.",
                    level=LOG_LEVEL_WARNING,
                    include_context=True,
                    location=ENV_CONFIG_LOCATION,
                )

            if no_ap and ap_lua_arg:
                if _resolve_lua_arg_path(ap_lua_arg):
                    passthrough_lua_arg = ap_lua_arg
                else:
                    RUNNER_LOGGER.log(
                        f"Lua script not found, skipping: {ap_lua_arg}",
                        level=LOG_LEVEL_WARNING,
                        include_context=True,
                        location="lua-arg",
                    )

            if needs_archipelago:
                mount = _find_archipelago_mount()
                if not mount:
                    fallback_error_dialog(
                        "Archipelago AppImage mount not found.\n\n"
                        "Please start Archipelago before launching BizHawk so the AppImage mount is available.",
                        title=RUNNER_ERROR_TITLE,
                        logger=RUNNER_LOGGER,
                    )
                    return 1

                try:
                    if wants_sni:
                        connector_path = _resolve_sni_connector(mount)
                        if not connector_path:
                            fallback_error_dialog(
                                "SNI connector not found inside Archipelago AppImage mount.",
                                title=RUNNER_ERROR_TITLE,
                                logger=RUNNER_LOGGER,
                            )
                            return 1
                    else:
                        connector_path = _resolve_connector_from_arg(mount, ap_lua_arg)
                except FileNotFoundError as exc:
                    fallback_error_dialog(
                        str(exc),
                        title=RUNNER_ERROR_TITLE,
                        logger=RUNNER_LOGGER,
                    )
                    return 1

                env[AP_BIZHELPER_CONNECTOR_PATH_ENV] = str(connector_path)

                if wants_sni:
                    sni_path = _resolve_sni(mount)
                    if not sni_path:
                        fallback_error_dialog(
                            "SNI binary not found inside Archipelago AppImage mount.",
                            title=RUNNER_ERROR_TITLE,
                            logger=RUNNER_LOGGER,
                        )
                        return 1
                    _launch_sni(sni_path, env)

                helpers_root = _helpers_root(settings)
                entry_lua = helpers_root / BIZHAWK_ENTRY_LUA_FILENAME
                if not entry_lua.is_file():
                    fallback_error_dialog(
                        f"Missing BizHawk entry Lua script: {entry_lua}",
                        title=RUNNER_ERROR_TITLE,
                        logger=RUNNER_LOGGER,
                    )
                    return 1

            final_args: list[str] = []
            if rom_path:
                final_args.append(rom_path)
            final_args.extend(emu_args)
            if passthrough_lua_arg:
                final_args.append(passthrough_lua_arg)
            if entry_lua:
                final_args.append(f"--lua={entry_lua}")

            RUNNER_LOGGER.log(
                f"Launching BizHawk via transient systemd service (Steam-detached): {bizhawk_exe} {final_args}",
                include_context=True,
                location=COMMAND_LOCATION,
            )

            _stage_cached_launch(original_args)

            systemd_run = shutil.which("systemd-run")
            if not systemd_run:
                fallback_error_dialog(
                    "systemd-run is not available; cannot launch BizHawk as a detached transient service.",
                    title=RUNNER_ERROR_TITLE,
                    logger=RUNNER_LOGGER,
                )
                return 1

            # NOTE: We intentionally use a transient *service* unit here rather than a scope.
            # A scope's processes are launched by systemd-run itself (i.e. systemd-run is the parent),
            # which keeps BizHawk inside Steam's process tree and makes it vulnerable to Steam's cleanup.
            # A transient service is spawned by the user service manager, giving BizHawk a detached parent
            # process and cgroup.
            #
            # Transient services run in a "clean" environment by default, so we explicitly pass the
            # environment we constructed for BizHawk (runtime root, mono config, connector paths, etc.).
            env_opts: list[str] = []
            for key, value in sorted(env.items()):
                env_opts.extend(["-E", f"{key}={value}"])

            unit = f"ap-bizhawk-{os.getpid()}-{int(time.time())}"
            cmd = [
                systemd_run,
                "--user",
                "--unit",
                unit,
                "--collect",
                "--property=Type=exec",
                "--working-directory",
                str(bizhawk_root),
            ]
            cmd.extend(env_opts)
            launch_wrapper = (
                f"export {AP_BIZHELPER_EMUHAWK_PID_ENV}=$$; exec \"$@\""
            )
            cmd.extend(
                [
                    "--",
                    "/bin/sh",
                    "-lc",
                    launch_wrapper,
                    "--",
                    str(bizhawk_exe),
                    *final_args,
                ]
            )

            # Use the runner's own environment for systemd-run (DBus/session access), while BizHawk gets
            # the explicit env via -E options above.
            result = subprocess.run(
                cmd,
                env=os.environ.copy(),
                capture_output=True,
                text=True,
                check=False,
            )

            output = f"{result.stdout or ''}\n{result.stderr or ''}".strip()
            snippet = " ".join(output.split()) or "<no output>"
            if len(snippet) > 400:
                snippet = snippet[:400] + "..."
            RUNNER_LOGGER.log(
                f"systemd-run service requested (unit={unit}) rc={result.returncode} output={snippet}",
                include_context=True,
                location=COMMAND_LOCATION,
            )

            if result.returncode != 0:
                fallback_error_dialog(
                    f"Failed to launch BizHawk via systemd-run service (rc={result.returncode}).\n\nOutput:\n{output}",
                    title=RUNNER_ERROR_TITLE,
                    logger=RUNNER_LOGGER,
                )
                return 1

            emuhawk_path = bizhawk_root / "EmuHawkMono.sh"
            emuhawk_pid = _discover_emuhawk_pid(emuhawk_path)
            if emuhawk_pid:
                RUNNER_LOGGER.log(
                    f"Discovered EmuHawk pid={emuhawk_pid} for {emuhawk_path}.",
                    include_context=True,
                    location="pid-discovery",
                )
                _record_pid(emuhawk_pid)
            else:
                RUNNER_LOGGER.log(
                    f"Unable to discover EmuHawk pid for {emuhawk_path}.",
                    level=LOG_LEVEL_WARNING,
                    include_context=True,
                    location="pid-discovery",
                )
                _record_pid(0)
            return 0
        except Exception as exc:
            RUNNER_LOGGER.log(
                f"Unhandled exception in BizHawk runner: {exc}\n{traceback.format_exc()}",
                level=LOG_LEVEL_ERROR,
                include_context=True,
                location="runner-exception",
            )
            _show_error_dialog(f"BizHawk runner crashed unexpectedly: {exc}")
            return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
