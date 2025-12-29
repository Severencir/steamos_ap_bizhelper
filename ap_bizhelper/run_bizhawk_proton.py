#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from ap_bizhelper.ap_bizhelper_config import load_settings as _load_shared_settings
except ImportError:  # pragma: no cover - fallback when executed outside the package
    from .ap_bizhelper_config import load_settings as _load_shared_settings

from ap_bizhelper.logging_utils import RUNNER_LOG_ENV, create_component_logger
from ap_bizhelper.constants import (
    BIZHAWK_EXE_KEY,
    LOG_PREFIX,
    PROTON_BIN_KEY,
    PROTON_PREFIX,
    STEAM_ROOT_DIR,
)
from ap_bizhelper.dialogs import (
    enable_dialog_gamepad as _enable_dialog_gamepad,
    ensure_qt_app as _ensure_qt_app,
    ensure_qt_available as _ensure_qt_available,
    error_dialog as _shared_error_dialog,
)

COMMAND_LOCATION = "command"
CONNECTOR_GENERIC = "connector_bizhawk_generic.lua"
CONNECTOR_SNI = "connector.lua"
CONNECTORS_DIRNAME = "connectors"
DOLPHIN_CMD = "dolphin"
ENV_COMPAT_CLIENT_PATH = "STEAM_COMPAT_CLIENT_INSTALL_PATH"
ENV_COMPAT_DATA_PATH = "STEAM_COMPAT_DATA_PATH"
ENV_CONFIG_LOCATION = "env-config"
EXEC_LOCATION = "exec"
LOG_LEVEL_ERROR = "ERROR"
LUA_ARG_PREFIX = "--lua="
LUA_EXTENSION = ".lua"
GLOB_WILDCARD = "*"
LUA_LOCATION = "lua"
OPTION_PREFIX = "-"
PROTON_PREFIX_KEY = "PROTON_PREFIX"
RUNNER_ERROR_TITLE = "BizHawk runner error"
RUNNER_MAIN_CONTEXT = "runner-main"
SNI_DIRNAME = "sni"
STEAM_ROOT_KEY = "STEAM_ROOT"
XDG_OPEN_CMD = "xdg-open"


def _load_settings():
    return _load_shared_settings()


def error_dialog(msg: str) -> None:
    """Show an error using PySide6 message boxes."""
    RUNNER_LOGGER.log(f"Error dialog requested: {msg}", level=LOG_LEVEL_ERROR, include_context=True)
    _shared_error_dialog(msg, title=RUNNER_ERROR_TITLE, logger=RUNNER_LOGGER)


_SETTINGS_CACHE = None
RUNNER_LOGGER = create_component_logger("bizhawk-runner", env_var=RUNNER_LOG_ENV, subdir="runner")


def get_env_or_config(var: str):
    """Read config from the environment or stored settings."""
    value = os.environ.get(var)
    if value:
        RUNNER_LOGGER.log(
            f"Using environment override for {var}={value}",
            include_context=True,
            location=ENV_CONFIG_LOCATION,
        )
        return value

    global _SETTINGS_CACHE
    if _SETTINGS_CACHE is None:
        _SETTINGS_CACHE = _load_settings()

    value = _SETTINGS_CACHE.get(var)
    if value:
        RUNNER_LOGGER.log(
            f"Loaded {var} from cached settings: {value}",
            include_context=True,
            location=ENV_CONFIG_LOCATION,
        )
    return str(value) if value else None


def ensure_bizhawk_exe() -> Path:
    exe = get_env_or_config(BIZHAWK_EXE_KEY)
    if not exe or not Path(exe).is_file():
        error_dialog(f"{LOG_PREFIX} BIZHAWK_EXE is not set or not a file; cannot launch BizHawk.")
        sys.exit(1)
    RUNNER_LOGGER.log(f"Resolved BizHawk executable: {exe}", include_context=True)
    return Path(exe)


def configure_proton_env():
    proton_bin = get_env_or_config(PROTON_BIN_KEY) or "proton"
    proton_prefix = get_env_or_config(PROTON_PREFIX_KEY) or str(PROTON_PREFIX)
    steam_root = get_env_or_config(STEAM_ROOT_KEY) or str(
        STEAM_ROOT_DIR
    )

    os.environ[ENV_COMPAT_DATA_PATH] = proton_prefix
    os.environ[ENV_COMPAT_CLIENT_PATH] = steam_root

    RUNNER_LOGGER.log(
        f"Configured Proton env: proton_bin={proton_bin}, prefix={proton_prefix}, steam_root={steam_root}",
        include_context=True,
    )

    return proton_bin, proton_prefix, steam_root


def parse_args(argv):
    """Return (rom_path, ap_lua_arg, emu_args_no_lua).

    ap_lua_arg is the *original* --lua=... (if any), stripped from final args.
    """
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


def _detect_connector_name(ap_lua_arg: str | None) -> str | None:
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


def _open_dolphin(target: Path) -> None:
    if shutil.which(DOLPHIN_CMD):
        try:
            subprocess.Popen([DOLPHIN_CMD, str(target)])
            return
        except Exception:
            pass
    if shutil.which(XDG_OPEN_CMD):
        try:
            subprocess.Popen([XDG_OPEN_CMD, str(target)])
        except Exception:
            pass


def _connector_windows_path(bizhawk_dir: Path, connector_path: Path) -> str:
    try:
        relative = connector_path.relative_to(bizhawk_dir)
    except ValueError:
        relative = connector_path
    return str(relative).replace("/", "\\")


def _find_sni_connector(sni_dir: Path) -> Path | None:
    if not sni_dir.is_dir():
        return None
    preferred = [
        p for p in sni_dir.glob(f"{GLOB_WILDCARD}{LUA_EXTENSION}") if p.name.lower() == CONNECTOR_SNI
    ]
    if preferred:
        return preferred[0]
    candidates = [
        p for p in sni_dir.glob(f"{GLOB_WILDCARD}{LUA_EXTENSION}") if p.is_file()
    ]
    return candidates[0] if candidates else None


def _missing_connector(connectors_dir: Path, connector_name: str) -> None:
    connectors_dir.mkdir(parents=True, exist_ok=True)
    error_dialog(
        f"{LOG_PREFIX} Could not find the required BizHawk connector\n"
        f"Expected to locate {connector_name} inside:\n{connectors_dir}\n\n"
        "Drag the correct connector into this directory and try again."
    )
    _open_dolphin(connectors_dir)
    sys.exit(1)


def decide_lua_arg(bizhawk_dir: Path, rom_path: str, ap_lua_arg: str | None) -> str:
    """Decide the final --lua=... argument or raise on failure.

    - For .sfc (SNES): always use the bundled SNI connector from bizhawk_dir/sni.
    - For other ROMs: use the connector requested by --lua if present, otherwise
      connector_bizhawk_generic.lua from bizhawk_dir/connectors.
    - On missing connectors, show a dialog and open the connectors directory.
    """

    ext = Path(rom_path).suffix.lower().lstrip(".")
    connectors_dir = bizhawk_dir / CONNECTORS_DIRNAME
    sni_dir = bizhawk_dir / SNI_DIRNAME

    if ext == "sfc":
        connector_path = _find_sni_connector(sni_dir)
        if connector_path is None:
            _missing_connector(sni_dir, CONNECTOR_SNI)
        if ap_lua_arg:
            print(
                f"{LOG_PREFIX} Ignoring AP-supplied --lua for SNES ROM; using local SNI connector."
            )
        lua_ap_path = _connector_windows_path(bizhawk_dir, connector_path)
        print(f"{LOG_PREFIX} Using SNI Lua connector for SNES ROM: {lua_ap_path}")
        RUNNER_LOGGER.log(
            f"Selected SNI connector for SNES ROM at {lua_ap_path}",
            include_context=True,
            location=LUA_LOCATION,
        )
        return f"{LUA_ARG_PREFIX}{lua_ap_path}"

    connector_name = _detect_connector_name(ap_lua_arg) or CONNECTOR_GENERIC
    connector_path = connectors_dir / connector_name
    if not connector_path.is_file():
        _missing_connector(connectors_dir, connector_name)

    lua_ap_path = _connector_windows_path(bizhawk_dir, connector_path)
    print(f"{LOG_PREFIX} Using BizHawk Lua connector: {lua_ap_path}")
    RUNNER_LOGGER.log(
        f"Using connector {connector_name} at {lua_ap_path}",
        include_context=True,
        location=LUA_LOCATION,
    )
    return f"{LUA_ARG_PREFIX}{lua_ap_path}"


def build_bizhawk_command(argv):
    """Transform incoming args into a Proton BizHawk command."""

    bizhawk_exe = ensure_bizhawk_exe()
    bizhawk_dir = bizhawk_exe.parent
    proton_bin, _, _ = configure_proton_env()

    rom_path, ap_lua_arg, emu_args = parse_args(argv)

    if rom_path is None:
        final_args = emu_args
        print(f"{LOG_PREFIX} No ROM detected; launching BizHawk without AP connector.")
    else:
        lua_arg = decide_lua_arg(bizhawk_dir, rom_path, ap_lua_arg)
        final_args = [rom_path, lua_arg] + emu_args

        print(f"{LOG_PREFIX} Running BizHawk via Proton:")
        print(f"{LOG_PREFIX} BIZHAWK_EXE: {bizhawk_exe}")
        print(f"{LOG_PREFIX} ROM:         {rom_path}")
        print(f"{LOG_PREFIX} Lua:         {lua_arg}")

    bizhawk_exe_rel = bizhawk_exe.name

    command = [proton_bin, "run", bizhawk_exe_rel, *final_args]
    RUNNER_LOGGER.log(
        f"Built BizHawk command: {command} (cwd={bizhawk_dir})",
        include_context=True,
        location=COMMAND_LOCATION,
    )
    return proton_bin, bizhawk_dir, command


def main(argv):
    with RUNNER_LOGGER.context(RUNNER_MAIN_CONTEXT):
        RUNNER_LOGGER.log(
            f"Starting BizHawk runner with argv: {argv}", include_context=True, mirror_console=True
        )
        proton_bin, bizhawk_dir, cmd = build_bizhawk_command(argv)
        os.chdir(bizhawk_dir)
        RUNNER_LOGGER.log(
            f"Executing via execvp: {proton_bin} {cmd}",
            include_context=True,
            location=EXEC_LOCATION,
        )
        os.execvp(proton_bin, cmd)


if __name__ == "__main__":
    main(sys.argv[1:])
