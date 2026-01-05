#!/usr/bin/env python3
import copy
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Optional

from ap_bizhelper.logging_utils import RUNNER_LOG_ENV, create_component_logger
from ap_bizhelper.constants import (
    AP_BIZHELPER_CONNECTOR_PATH_ENV,
    BIZHAWK_ENTRY_LUA_FILENAME,
    BIZHAWK_EXE_KEY,
    BIZHAWK_LAST_LAUNCH_ARGS_KEY,
    BIZHAWK_LAST_PID_KEY,
    BIZHAWK_RUNTIME_ROOT_KEY,
    LOG_PREFIX,
)
from ap_bizhelper.ap_bizhelper_config import (
    ENCODING_UTF8,
    INSTALL_STATE_FILE,
    PATH_SETTINGS_DEFAULTS,
    PATH_SETTINGS_FILE,
    STATE_SETTINGS_DEFAULTS,
    STATE_SETTINGS_FILE,
)
from ap_bizhelper.dialogs import error_dialog as _shared_error_dialog

COMMAND_LOCATION = "command"
CONNECTOR_GENERIC = "connector_bizhawk_generic.lua"
CONNECTOR_SNI = "connector.lua"
DEFAULT_MOUNT_PREFIX = ".mount_"
ENV_CONFIG_LOCATION = "env-config"
LOG_LEVEL_ERROR = "ERROR"
LUA_ARG_PREFIX = "--lua="
LUA_EXTENSION = ".lua"
OPTION_PREFIX = "-"
RUNNER_ERROR_TITLE = "BizHawk runner error"
RUNNER_MAIN_CONTEXT = "runner-main"
SETTINGS_LOAD_LOCATION = "settings-load"
SETTINGS_LOOKUP_LOCATION = "settings-lookup"
SETTINGS_SAVE_LOCATION = "settings-save"
SNI_DIRNAME = "SNI"

RUNNER_LOGGER = create_component_logger("bizhawk-runner", env_var=RUNNER_LOG_ENV, subdir="runner")
_CONFIG_CACHE: dict[Path, dict[str, Any]] = {}


def error_dialog(msg: str) -> None:
    """Show an error using PySide6 message boxes."""
    RUNNER_LOGGER.log(f"Error dialog requested: {msg}", level=LOG_LEVEL_ERROR, include_context=True)
    _shared_error_dialog(msg, title=RUNNER_ERROR_TITLE, logger=RUNNER_LOGGER)

def _apply_defaults(settings: dict[str, Any], defaults: dict[str, Any]) -> None:
    for key, value in defaults.items():
        if key not in settings:
            settings[key] = copy.deepcopy(value)


def _load_json_file(path: Path) -> dict[str, Any]:
    if path in _CONFIG_CACHE:
        return _CONFIG_CACHE[path]

    if not path.exists():
        RUNNER_LOGGER.log(
            f"Settings file missing: {path}",
            include_context=True,
            location=SETTINGS_LOAD_LOCATION,
        )
        data: dict[str, Any] = {}
        _CONFIG_CACHE[path] = data
        return data

    try:
        with path.open("r", encoding=ENCODING_UTF8) as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            RUNNER_LOGGER.log(
                f"Settings file {path} did not contain a JSON object; treating as empty.",
                level=LOG_LEVEL_ERROR,
                include_context=True,
                location=SETTINGS_LOAD_LOCATION,
            )
            data = {}
    except Exception as exc:
        RUNNER_LOGGER.log(
            f"Failed to read settings file {path}: {exc}\n{traceback.format_exc()}",
            level=LOG_LEVEL_ERROR,
            include_context=True,
            location=SETTINGS_LOAD_LOCATION,
        )
        data = {}

    _CONFIG_CACHE[path] = data
    return data


def _save_json_file(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding=ENCODING_UTF8) as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        tmp.replace(path)
        _CONFIG_CACHE[path] = data
        RUNNER_LOGGER.log(
            f"Saved settings file: {path}",
            include_context=True,
            location=SETTINGS_SAVE_LOCATION,
        )
    except Exception as exc:
        RUNNER_LOGGER.log(
            f"Failed to save settings file {path}: {exc}\n{traceback.format_exc()}",
            level=LOG_LEVEL_ERROR,
            include_context=True,
            location=SETTINGS_SAVE_LOCATION,
        )
        raise


def _read_setting_value(path: Path, key: str, default: Any = None) -> Any:
    settings = _load_json_file(path)
    if not settings:
        settings = {}
    value = settings.get(key, default)
    if value not in (None, ""):
        RUNNER_LOGGER.log(
            f"Loaded {key} from {path}: {value}",
            include_context=True,
            location=SETTINGS_LOOKUP_LOCATION,
        )
        return value

    if default not in (None, ""):
        RUNNER_LOGGER.log(
            f"Using default for {key}: {default}",
            include_context=True,
            location=SETTINGS_LOOKUP_LOCATION,
        )
        return default

    RUNNER_LOGGER.log(
        f"No configured value found for {key} in {path}.",
        level=LOG_LEVEL_ERROR,
        include_context=True,
        location=SETTINGS_LOOKUP_LOCATION,
    )
    return None


def get_env_or_config(var: str) -> Optional[str]:
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
        value = _read_setting_value(INSTALL_STATE_FILE, var)
    elif var == BIZHAWK_RUNTIME_ROOT_KEY:
        default = PATH_SETTINGS_DEFAULTS.get(var, "")
        value = _read_setting_value(PATH_SETTINGS_FILE, var, default=default)
    else:
        RUNNER_LOGGER.log(
            f"No settings file mapping configured for {var}.",
            level=LOG_LEVEL_ERROR,
            include_context=True,
            location=SETTINGS_LOOKUP_LOCATION,
        )
        return None

    return str(value) if value not in (None, "") else None


def ensure_bizhawk_exe() -> Path:
    exe = get_env_or_config(BIZHAWK_EXE_KEY)
    if not exe or not Path(exe).is_file():
        error_dialog(f"{LOG_PREFIX} BIZHAWK_EXE is not set or not a file; cannot launch BizHawk.")
        sys.exit(1)
    RUNNER_LOGGER.log(f"Resolved BizHawk launcher script: {exe}", include_context=True)
    return Path(exe)


def _runtime_root() -> Path:
    value = get_env_or_config(BIZHAWK_RUNTIME_ROOT_KEY)
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
        error_dialog(
            "BizHawk runtime dependencies are missing from runtime_root:\n"
            f"{runtime_root}\n\nMissing: {', '.join(missing)}"
        )
        sys.exit(1)


def _detect_connector_name(ap_lua_arg: str | None) -> Optional[str]:
    """Return the connector filename requested via ``--lua`` (name only)."""

    if not ap_lua_arg:
        return None

    if ap_lua_arg.startswith(LUA_ARG_PREFIX):
        lua_path = ap_lua_arg[len(LUA_ARG_PREFIX) :]
    else:
        lua_path = ap_lua_arg

    name = Path(lua_path).name
    # Always honor the requested name, even if it was provided without a .lua suffix.
    return name if name.endswith(LUA_EXTENSION) else f"{name}{LUA_EXTENSION}"


def parse_args(argv):
    """Return (rom_path, ap_lua_arg, emu_args_no_lua)."""
    rom_path = None
    ap_lua_arg = None
    emu_args = []

    i = 0
    n = len(argv)
    while i < n:
        arg = argv[i]
        i += 1

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
        f"Parsed args rom={rom_path}, ap_lua_arg={ap_lua_arg}, emu_args={emu_args}",
        include_context=True,
        location="parse-args",
    )
    return rom_path, ap_lua_arg, emu_args


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


def _expected_connector_path(mount: Path, connector_name: str) -> Path:
    return mount / "Archipelago" / connector_name


def _expected_sni_path(mount: Path) -> Path:
    return mount / "Archipelago" / SNI_DIRNAME / "sni"


def _find_archipelago_mount() -> Optional[Path]:
    candidates = _archipelago_mount_candidates()
    filtered = []
    for candidate in candidates:
        name = candidate.name.lower()
        has_hint = "archip" in name
        has_connector = _expected_connector_path(candidate, CONNECTOR_GENERIC).is_file()
        if has_hint or has_connector:
            filtered.append(candidate)

    if not filtered:
        return None

    filtered.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return filtered[0]


def _resolve_connector(mount: Path, requested: Optional[str]) -> Path:
    connector_name = requested or CONNECTOR_GENERIC
    expected = _expected_connector_path(mount, connector_name)
    if expected.is_file():
        return expected
    raise FileNotFoundError(f"Connector not found: {expected}")


def _resolve_sni(mount: Path) -> Optional[Path]:
    candidate = _expected_sni_path(mount)
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
        error_dialog(f"Failed to launch SNI: {exc}")
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


def _load_state_settings() -> dict[str, Any]:
    state_settings = _load_json_file(STATE_SETTINGS_FILE)
    if not isinstance(state_settings, dict):
        state_settings = {}
    _apply_defaults(state_settings, STATE_SETTINGS_DEFAULTS)
    return state_settings


def _update_state_setting(key: str, value: Any) -> None:
    state_settings = _load_state_settings()
    state_settings[key] = value
    try:
        _save_json_file(STATE_SETTINGS_FILE, state_settings)
    except Exception:
        pass


def _stage_cached_launch(args: list[str]) -> None:
    _update_state_setting(BIZHAWK_LAST_LAUNCH_ARGS_KEY, args)


def _record_pid(pid: int) -> None:
    _update_state_setting(BIZHAWK_LAST_PID_KEY, str(pid))


def main(argv: list[str]) -> int:
    with RUNNER_LOGGER.context(RUNNER_MAIN_CONTEXT):
        RUNNER_LOGGER.log(
            "BizHawk runner starting.",
            include_context=True,
            location="startup",
        )
        try:
            bizhawk_exe = ensure_bizhawk_exe()
            bizhawk_root = bizhawk_exe.parent

            runtime_root = _runtime_root()
            if not runtime_root:
                error_dialog(
                    f"{LOG_PREFIX} BIZHAWK_RUNTIME_ROOT is not set; cannot launch BizHawk."
                )
                return 1
            _validate_runtime(runtime_root)

            original_args = list(argv[1:])
            rom_path, ap_lua_arg, emu_args = parse_args(original_args)
            connector_name = _detect_connector_name(ap_lua_arg)

            mount = _find_archipelago_mount()
            if not mount:
                error_dialog(
                    "Archipelago AppImage mount not found.\n\n"
                    "Please start Archipelago before launching BizHawk so the AppImage mount is available."
                )
                return 1

            try:
                connector_path = _resolve_connector(mount, connector_name)
            except FileNotFoundError as exc:
                error_dialog(str(exc))
                return 1

            env = _build_runtime_env(runtime_root, bizhawk_root)
            env[AP_BIZHELPER_CONNECTOR_PATH_ENV] = str(connector_path)

            if connector_name == CONNECTOR_SNI:
                sni_path = _resolve_sni(mount)
                if not sni_path:
                    error_dialog("SNI binary not found inside Archipelago AppImage mount.")
                    return 1
                _launch_sni(sni_path, env)

            entry_lua = bizhawk_root / BIZHAWK_ENTRY_LUA_FILENAME
            if not entry_lua.is_file():
                error_dialog(f"Missing BizHawk entry Lua script: {entry_lua}")
                return 1

            final_args: list[str] = []
            if rom_path:
                final_args.append(rom_path)
            final_args.extend(emu_args)
            final_args.append(f"--lua={entry_lua}")

            RUNNER_LOGGER.log(
                f"Launching BizHawk: {bizhawk_exe} {final_args}",
                include_context=True,
                location=COMMAND_LOCATION,
            )

            _stage_cached_launch(original_args)

            proc = subprocess.Popen(
                [str(bizhawk_exe), *final_args],
                cwd=str(bizhawk_root),
                env=env,
            )
            _record_pid(proc.pid)
            time.sleep(0.1)
            return 0
        except Exception as exc:
            RUNNER_LOGGER.log(
                f"Unhandled exception in BizHawk runner: {exc}\n{traceback.format_exc()}",
                level=LOG_LEVEL_ERROR,
                include_context=True,
                location="runner-exception",
            )
            error_dialog(f"BizHawk runner crashed unexpectedly: {exc}")
            return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
