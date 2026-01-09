#!/usr/bin/env python3
import contextlib
import copy
import contextvars
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

APP_NAME = "ap-bizhelper"
AP_BIZHELPER_CONNECTOR_PATH_ENV = "AP_BIZHELPER_CONNECTOR_PATH"
BIZHAWK_ENTRY_LUA_FILENAME = "ap_bizhelper_migration_launcher.lua"
BIZHAWK_EXE_KEY = "BIZHAWK_EXE"
BIZHAWK_LAST_LAUNCH_ARGS_KEY = "BIZHAWK_LAST_LAUNCH_ARGS"
BIZHAWK_LAST_PID_KEY = "BIZHAWK_LAST_PID"
BIZHAWK_RUNTIME_ROOT_KEY = "BIZHAWK_RUNTIME_ROOT"
SAVE_MIGRATION_HELPER_PATH_KEY = "SAVE_MIGRATION_HELPER_PATH"
ENCODING_UTF8 = "utf-8"
LOG_PREFIX = f"[{APP_NAME}]"
RUN_ID_ENV = "AP_BIZHELPER_LOG_RUN_ID"
RUNNER_LOG_ENV = "AP_BIZHELPER_RUNNER_LOG_PATH"
TIMESTAMP_ENV = "AP_BIZHELPER_LOG_TIMESTAMP"

CONFIG_DIR = Path.home() / ".config" / APP_NAME
DATA_DIR = Path.home() / ".local" / "share" / APP_NAME
LOG_ROOT = DATA_DIR / "logs"
INSTALL_STATE_FILE = CONFIG_DIR / "install_state.json"
PATH_SETTINGS_FILE = CONFIG_DIR / "path_settings.json"
STATE_SETTINGS_FILE = CONFIG_DIR / "state_settings.json"
SAVE_HELPER_STAGED_FILENAME = "save_migration_helper.py"
PATH_SETTINGS_DEFAULTS = {
    BIZHAWK_RUNTIME_ROOT_KEY: str(DATA_DIR / "runtime_root"),
    SAVE_MIGRATION_HELPER_PATH_KEY: str(DATA_DIR / SAVE_HELPER_STAGED_FILENAME),
}
STATE_SETTINGS_DEFAULTS = {
    BIZHAWK_LAST_LAUNCH_ARGS_KEY: [],
    BIZHAWK_LAST_PID_KEY: "",
}

BRACKET_CLOSE = "]"
BRACKET_OPEN = "["
CONTEXT_SEPARATOR = " > "
DASH = "-"
LOG_LEVEL_ERROR = "ERROR"
LOG_LEVEL_INFO = "INFO"
LOG_LEVEL_WARNING = "WARNING"
LOG_FILE_SUFFIX = ".log"
SPACE = " "
UNDERSCORE = "_"
_CONTEXT_STACK: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "ap_bizhelper_runner_context", default=()
)

_CONFIG_CACHE: dict[Path, dict[str, Any]] = {}


def _slugify(label: str) -> str:
    return label.strip().replace(" ", UNDERSCORE).replace("/", DASH) or "general"


class AppLogger:
    def __init__(
        self,
        category: str,
        *,
        log_dir: Optional[Path] = None,
        log_path: Optional[Path] = None,
        run_id: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> None:
        self.timestamp = timestamp or datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.run_id = run_id or uuid4().hex[:8]
        self.category = _slugify(category)
        base_dir = log_dir or LOG_ROOT
        base_dir.mkdir(parents=True, exist_ok=True)
        if log_path:
            self.path = Path(log_path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
        else:
            self.path = (
                base_dir
                / f"{self.category}{UNDERSCORE}{self.timestamp}{UNDERSCORE}{self.run_id}{LOG_FILE_SUFFIX}"
            )
        self._sequence = 0

    @contextlib.contextmanager
    def context(self, label: str):
        stack = _CONTEXT_STACK.get()
        token = _CONTEXT_STACK.set((*stack, label))
        try:
            yield
        finally:
            _CONTEXT_STACK.reset(token)

    def _next_entry_id(self) -> str:
        self._sequence += 1
        return f"{self.run_id}{DASH}{self._sequence:04d}"

    def _context_label(self) -> str:
        return CONTEXT_SEPARATOR.join(_CONTEXT_STACK.get())

    def log(
        self,
        message: str,
        *,
        level: str = LOG_LEVEL_INFO,
        location: Optional[str] = None,
        include_context: bool = False,
    ) -> str:
        entry_id = self._next_entry_id()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        context_label = self._context_label()
        location_id = _slugify(
            location or (context_label.split(CONTEXT_SEPARATOR)[-1] if context_label else "root")
        )

        parts = [
            f"{BRACKET_OPEN}{timestamp}{BRACKET_CLOSE}",
            f"{BRACKET_OPEN}{entry_id}{BRACKET_CLOSE}",
            f"{BRACKET_OPEN}{location_id}{BRACKET_CLOSE}",
            f"{BRACKET_OPEN}{level.upper()}{BRACKET_CLOSE}",
        ]
        if include_context and context_label:
            parts.append(f"{BRACKET_OPEN}ctx:{context_label}{BRACKET_CLOSE}")
        parts.append(message)
        line = SPACE.join(parts)
        with self.path.open("a", encoding=ENCODING_UTF8) as log_file:
            log_file.write(line + "\n")
        return entry_id


def create_component_logger(
    category: str,
    *,
    env_var: Optional[str] = None,
    subdir: Optional[str] = None,
) -> AppLogger:
    env_path = Path(os.environ[env_var]) if env_var and os.environ.get(env_var) else None
    return AppLogger(
        category,
        log_dir=LOG_ROOT / subdir if subdir else None,
        log_path=env_path,
        run_id=os.environ.get(RUN_ID_ENV) or None,
        timestamp=os.environ.get(TIMESTAMP_ENV) or None,
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


def _show_error_dialog(msg: str) -> None:
    RUNNER_LOGGER.log(f"Error dialog requested: {msg}", level=LOG_LEVEL_ERROR, include_context=True)
    zenity = shutil.which("zenity")
    if zenity and os.environ.get("DISPLAY"):
        subprocess.run(
            [zenity, "--error", "--title", RUNNER_ERROR_TITLE, "--text", msg],
            check=False,
        )
        return
    sys.stderr.write(f"{RUNNER_ERROR_TITLE}: {msg}\n")

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
    elif var in (BIZHAWK_RUNTIME_ROOT_KEY, SAVE_MIGRATION_HELPER_PATH_KEY):
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
        _show_error_dialog(f"{LOG_PREFIX} BIZHAWK_EXE is not set or not a file; cannot launch BizHawk.")
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
        _show_error_dialog(
            "BizHawk runtime dependencies are missing from runtime_root:\n"
            f"{runtime_root}\n\nMissing: {', '.join(missing)}"
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
        _show_error_dialog(f"Failed to launch SNI: {exc}")
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
                _show_error_dialog(
                    f"{LOG_PREFIX} BIZHAWK_RUNTIME_ROOT is not set; cannot launch BizHawk."
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
            helper_path = get_env_or_config(SAVE_MIGRATION_HELPER_PATH_KEY)
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
                    _show_error_dialog(
                        "Archipelago AppImage mount not found.\n\n"
                        "Please start Archipelago before launching BizHawk so the AppImage mount is available."
                    )
                    return 1

                try:
                    if wants_sni:
                        connector_path = _resolve_sni_connector(mount)
                        if not connector_path:
                            _show_error_dialog("SNI connector not found inside Archipelago AppImage mount.")
                            return 1
                    else:
                        connector_path = _resolve_connector_from_arg(mount, ap_lua_arg)
                except FileNotFoundError as exc:
                    _show_error_dialog(str(exc))
                    return 1

                env[AP_BIZHELPER_CONNECTOR_PATH_ENV] = str(connector_path)

                if wants_sni:
                    sni_path = _resolve_sni(mount)
                    if not sni_path:
                        _show_error_dialog("SNI binary not found inside Archipelago AppImage mount.")
                        return 1
                    _launch_sni(sni_path, env)

                entry_lua = bizhawk_root / BIZHAWK_ENTRY_LUA_FILENAME
                if not entry_lua.is_file():
                    _show_error_dialog(f"Missing BizHawk entry Lua script: {entry_lua}")
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
                _show_error_dialog(
                    "systemd-run is not available; cannot launch BizHawk as a detached transient service."
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
            cmd.extend([
                "--",
                str(bizhawk_exe),
                *final_args,
            ])

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
                _show_error_dialog(
                    f"Failed to launch BizHawk via systemd-run service (rc={result.returncode}).\n\nOutput:\n{output}"
                )
                return 1

            # We no longer have a direct BizHawk PID here (systemd manages the transient service). The save-migration helper
            # falls back to scanning for BizHawk processes, so clearing the PID is fine.
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
