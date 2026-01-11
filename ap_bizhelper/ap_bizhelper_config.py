#!/usr/bin/env python3
"""
Configuration and per-extension behavior helper for ap-bizhelper.

This script is designed to be called from the Bash wrapper. It owns the
persistent config format so that future refactors can move more logic
into Python without changing the on-disk representation again.

Commands:

  export-shell
      Print shell assignments for known settings keys, suitable for eval
      in Bash. Only keys that exist in the config are emitted.

  save-from-env
      Read known settings keys from the current environment and persist
      them to the settings file.

  get-ext EXT
      Print the behavior value for extension EXT (e.g. "apbp") if it
      exists, and exit 0. If no behavior is stored, print nothing and
      exit 1.

  set-ext EXT VALUE
      Set the behavior for extension EXT to VALUE and persist it.
"""

from __future__ import annotations

import copy
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from .constants import (
    AP_APPIMAGE_KEY,
    AP_APPIMAGE_DEFAULT,
    AP_DESKTOP_SHORTCUT_KEY,
    AP_LATEST_SEEN_VERSION_KEY,
    AP_SKIP_VERSION_KEY,
    AP_WAIT_FOR_EXIT_KEY,
    AP_WAIT_FOR_EXIT_POLL_SECONDS_KEY,
    AP_VERSION_KEY,
    BIZHELPER_APPIMAGE_KEY,
    BIZHELPER_APPIMAGE_DEFAULT,
    BIZHAWK_DESKTOP_SHORTCUT_KEY,
    BIZHAWK_CLEAR_LD_PRELOAD_KEY,
    BIZHAWK_EXE_KEY,
    BIZHAWK_ENTRY_LUA_PATH_KEY,
    BIZHAWK_ENTRY_LUA_FILENAME,
    BIZHAWK_INSTALL_DIR_KEY,
    BIZHAWK_LAST_LAUNCH_ARGS_KEY,
    BIZHAWK_LAST_PID_KEY,
    BIZHAWK_LATEST_SEEN_KEY,
    BIZHAWK_RUNNER_KEY,
    BIZHAWK_SKIP_VERSION_KEY,
    BIZHAWK_VERSION_KEY,
    BIZHAWK_RUNTIME_ROOT_KEY,
    BIZHAWK_RUNTIME_DOWNLOAD_KEY,
    BIZHAWK_SAVERAM_DIR_KEY,
    CONFIG_DIR,
    DATA_DIR,
    DESKTOP_DIR_KEY,
    DOWNLOADS_DIR_KEY,
    LAST_FILE_DIALOG_DIR_KEY,
    LAST_FILE_DIALOG_DIRS_KEY,
    LAST_ROM_DIR_KEY,
    PENDING_RELAUNCH_ARGS_KEY,
    ROM_HASH_CACHE_KEY,
    ROM_ROOTS_KEY,
    SAVE_HELPER_STAGED_FILENAME,
    SAVE_MIGRATION_HELPER_PATH_KEY,
    SFC_LUA_PATH_KEY,
    STAGED_COMPONENTS_DIR,
    STEAM_APPID_KEY,
    STEAM_ROOT_PATH_KEY,
    USE_CACHED_RELAUNCH_ARGS_KEY,
)
SETTINGS_FILE = CONFIG_DIR / "settings.json"
INSTALL_STATE_FILE = CONFIG_DIR / "install_state.json"
STATE_SETTINGS_FILE = CONFIG_DIR / "state_settings.json"
EXT_BEHAVIOR_FILE = CONFIG_DIR / "ext_behavior.json"
EXT_ASSOCIATION_FILE = CONFIG_DIR / "ext_associations.json"
APWORLD_CACHE_FILE = CONFIG_DIR / "apworld_cache.json"
PATH_SETTINGS_FILE = CONFIG_DIR / "path_settings.json"
BIZHAWK_SAVERAM_DIR = Path(os.path.expanduser("~/Documents/bizhawk-saveram"))
PATH_SETTINGS_DEFAULTS = {
    DESKTOP_DIR_KEY: str(Path.home() / "Desktop"),
    DOWNLOADS_DIR_KEY: str(Path.home() / "Downloads"),
    BIZHAWK_INSTALL_DIR_KEY: str(DATA_DIR / "bizhawk_install"),
    BIZHAWK_RUNTIME_ROOT_KEY: str(DATA_DIR / "runtime_root"),
    BIZHAWK_SAVERAM_DIR_KEY: str(BIZHAWK_SAVERAM_DIR),
    SAVE_MIGRATION_HELPER_PATH_KEY: str(STAGED_COMPONENTS_DIR / SAVE_HELPER_STAGED_FILENAME),
    STEAM_ROOT_PATH_KEY: str(Path(os.path.expanduser("~/.steam/steam"))),
    SFC_LUA_PATH_KEY: "",
    LAST_FILE_DIALOG_DIR_KEY: "",
    LAST_FILE_DIALOG_DIRS_KEY: {},
    LAST_ROM_DIR_KEY: "",
    ROM_ROOTS_KEY: [],
}
INSTALL_STATE_DEFAULTS = {
    AP_APPIMAGE_KEY: str(AP_APPIMAGE_DEFAULT),
    BIZHELPER_APPIMAGE_KEY: str(BIZHELPER_APPIMAGE_DEFAULT),
    BIZHAWK_ENTRY_LUA_PATH_KEY: str(STAGED_COMPONENTS_DIR / BIZHAWK_ENTRY_LUA_FILENAME),
}
STATE_SETTINGS_DEFAULTS = {
    BIZHAWK_LAST_LAUNCH_ARGS_KEY: [],
    BIZHAWK_LAST_PID_KEY: "",
    PENDING_RELAUNCH_ARGS_KEY: [],
    ROM_HASH_CACHE_KEY: {},
    STEAM_APPID_KEY: "",
    USE_CACHED_RELAUNCH_ARGS_KEY: False,
}
SAFE_SETTINGS_DEFAULTS = {
    AP_WAIT_FOR_EXIT_KEY: True,
    AP_WAIT_FOR_EXIT_POLL_SECONDS_KEY: 5,
    BIZHAWK_RUNTIME_DOWNLOAD_KEY: True,
    BIZHAWK_CLEAR_LD_PRELOAD_KEY: True,
}
DISABLED_MODE = "disabled"
ENABLED_MODE = "enabled"
EMPTY_STRING = ""
ENCODING_UTF8 = "utf-8"
EXTENSIONS_KEY = "extensions"
MODE_KEY = "mode"
PROMPT_MODE = "prompt"
# Keys we expose back to Bash as shell variables.
INSTALL_STATE_KEYS = {
    AP_APPIMAGE_KEY,
    AP_VERSION_KEY,
    AP_SKIP_VERSION_KEY,
    AP_LATEST_SEEN_VERSION_KEY,
    BIZHELPER_APPIMAGE_KEY,
    BIZHAWK_EXE_KEY,
    BIZHAWK_ENTRY_LUA_PATH_KEY,
    BIZHAWK_VERSION_KEY,
    BIZHAWK_SKIP_VERSION_KEY,
    BIZHAWK_LATEST_SEEN_KEY,
    BIZHAWK_RUNNER_KEY,
}

SAFE_SETTINGS_KEYS = [
    "QT_FONT_SCALE",
    "QT_MIN_POINT_SIZE",
    "QT_FILE_NAME_FONT_SCALE",
    "QT_FILE_ICON_SIZE",
    "QT_FILE_DIALOG_WIDTH",
    "QT_FILE_DIALOG_HEIGHT",
    "QT_FILE_DIALOG_MAXIMIZE",
    "QT_FILE_DIALOG_NAME_WIDTH",
    "QT_FILE_DIALOG_TYPE_WIDTH",
    "QT_FILE_DIALOG_SIZE_WIDTH",
    "QT_FILE_DIALOG_DATE_WIDTH",
    "QT_FILE_DIALOG_SIDEBAR_WIDTH",
    "QT_FILE_DIALOG_SIDEBAR_ICON_SIZE",
    AP_DESKTOP_SHORTCUT_KEY,
    AP_WAIT_FOR_EXIT_KEY,
    AP_WAIT_FOR_EXIT_POLL_SECONDS_KEY,
    BIZHAWK_DESKTOP_SHORTCUT_KEY,
    BIZHAWK_CLEAR_LD_PRELOAD_KEY,
    BIZHAWK_RUNTIME_DOWNLOAD_KEY,
]
PATH_SETTINGS_KEYS = [
    DESKTOP_DIR_KEY,
    DOWNLOADS_DIR_KEY,
    BIZHAWK_INSTALL_DIR_KEY,
    BIZHAWK_RUNTIME_ROOT_KEY,
    BIZHAWK_SAVERAM_DIR_KEY,
    STEAM_ROOT_PATH_KEY,
    SFC_LUA_PATH_KEY,
    LAST_FILE_DIALOG_DIR_KEY,
    LAST_FILE_DIALOG_DIRS_KEY,
    LAST_ROM_DIR_KEY,
    ROM_ROOTS_KEY,
    SAVE_MIGRATION_HELPER_PATH_KEY,
]
STATE_SETTINGS_KEYS = [
    BIZHAWK_LAST_LAUNCH_ARGS_KEY,
    BIZHAWK_LAST_PID_KEY,
    PENDING_RELAUNCH_ARGS_KEY,
    ROM_HASH_CACHE_KEY,
    STEAM_APPID_KEY,
    USE_CACHED_RELAUNCH_ARGS_KEY,
]
SAFE_SETTINGS_SET = set(SAFE_SETTINGS_KEYS)
PATH_SETTINGS_SET = set(PATH_SETTINGS_KEYS)
STATE_SETTINGS_SET = set(STATE_SETTINGS_KEYS)
SETTINGS_KEYS = [
    *SAFE_SETTINGS_KEYS,
    *STATE_SETTINGS_KEYS,
    *sorted(INSTALL_STATE_KEYS),
]


def _load_install_state() -> Dict[str, Any]:
    return _load_json(INSTALL_STATE_FILE)


def _load_path_settings() -> Dict[str, Any]:
    return _load_json(PATH_SETTINGS_FILE)


def _load_state_settings() -> Dict[str, Any]:
    return _load_json(STATE_SETTINGS_FILE)


def load_settings() -> Dict[str, Any]:
    """Return the persisted settings and install state as one mapping."""

    settings = _load_json(SETTINGS_FILE)
    needs_save = _apply_defaults(settings, SAFE_SETTINGS_DEFAULTS)
    path_settings = _load_path_settings()
    needs_save = _apply_defaults(path_settings, PATH_SETTINGS_DEFAULTS) or needs_save
    state_settings = _load_state_settings()
    needs_save = _apply_defaults(state_settings, STATE_SETTINGS_DEFAULTS) or needs_save
    install_state = _load_install_state()
    needs_save = _apply_defaults(install_state, INSTALL_STATE_DEFAULTS) or needs_save
    merged = {**settings, **path_settings, **state_settings, **install_state}
    if needs_save:
        save_settings(merged)
    return merged


def load_apworld_cache() -> Dict[str, Any]:
    """Return the persisted APWorld cache mapping."""

    return _load_json(APWORLD_CACHE_FILE)


def save_settings(settings: Dict[str, Any]) -> None:
    """Persist the given settings mapping to disk.

    Settings are split between user-safe preferences, paths, install state,
    and internal state so each category can be managed independently.
    """

    general_settings = {k: v for k, v in settings.items() if k in SAFE_SETTINGS_SET}
    path_settings = {k: v for k, v in settings.items() if k in PATH_SETTINGS_KEYS}
    install_state = {k: v for k, v in settings.items() if k in INSTALL_STATE_KEYS}
    state_settings = {
        k: v
        for k, v in settings.items()
        if k in STATE_SETTINGS_SET
        or k not in SAFE_SETTINGS_SET | PATH_SETTINGS_SET | INSTALL_STATE_KEYS
    }

    # Persist the filtered mappings directly so keys removed from ``settings``
    # are also removed from disk.
    _save_json(SETTINGS_FILE, general_settings)
    _save_json(PATH_SETTINGS_FILE, path_settings)
    _save_json(INSTALL_STATE_FILE, install_state)
    _save_json(STATE_SETTINGS_FILE, state_settings)


def get_path_setting(settings: Dict[str, Any], key: str) -> Path:
    value = settings.get(key)
    if value in (None, EMPTY_STRING):
        value = PATH_SETTINGS_DEFAULTS.get(key, EMPTY_STRING)
    return Path(os.path.expanduser(str(value))) if value else Path()


def get_default_path_setting(key: str) -> Path:
    value = PATH_SETTINGS_DEFAULTS.get(key, EMPTY_STRING)
    return Path(os.path.expanduser(str(value))) if value else Path()


def save_apworld_cache(cache: Dict[str, Any]) -> None:
    """Persist the APWorld cache mapping."""

    _save_json(APWORLD_CACHE_FILE, cache)


def get_ext_behavior(ext: str) -> Optional[str]:
    """Return the stored behavior for ``ext`` (case-insensitive) or ``None``."""

    ext = ext.strip().lower()
    if not ext:
        return None
    behaviors = _load_json(EXT_BEHAVIOR_FILE)
    value = behaviors.get(ext)
    if value is None or value == EMPTY_STRING:
        return None
    return str(value)


def set_ext_behavior(ext: str, value: str) -> None:
    """Set and persist the behavior for ``ext`` (case-insensitive)."""

    ext = ext.strip().lower()
    if not ext:
        return
    behaviors = _load_json(EXT_BEHAVIOR_FILE)
    behaviors[ext] = value
    _save_json(EXT_BEHAVIOR_FILE, behaviors)


def get_association_mode() -> str:
    """Return the persisted association mode (``prompt`` by default)."""

    data = _load_json(EXT_ASSOCIATION_FILE)
    mode = str(data.get(MODE_KEY) or PROMPT_MODE).lower()
    if mode not in {PROMPT_MODE, ENABLED_MODE, DISABLED_MODE}:
        return PROMPT_MODE
    return mode


def set_association_mode(mode: str) -> None:
    """Persist the association mode (``prompt``, ``enabled``, or ``disabled``)."""

    mode = str(mode or EMPTY_STRING).strip().lower()
    if mode not in {PROMPT_MODE, ENABLED_MODE, DISABLED_MODE}:
        return

    data = _load_json(EXT_ASSOCIATION_FILE)
    data[MODE_KEY] = mode
    _save_json(EXT_ASSOCIATION_FILE, data)


def _load_association_map() -> Dict[str, str]:
    data = _load_json(EXT_ASSOCIATION_FILE)
    associations = data.get(EXTENSIONS_KEY)
    if not isinstance(associations, dict):
        return {}
    return {str(k).strip().lower(): str(v) for k, v in associations.items() if str(k).strip()}


def get_ext_association(ext: str) -> Optional[str]:
    """Return the persisted association state for ``ext`` or ``None``."""

    ext = ext.strip().lower()
    if not ext:
        return None
    return _load_association_map().get(ext)


def set_ext_association(ext: str, value: str) -> None:
    """Persist the association state for ``ext`` (case-insensitive)."""

    ext = ext.strip().lower()
    if not ext:
        return

    data = _load_json(EXT_ASSOCIATION_FILE)
    associations = data.get(EXTENSIONS_KEY)
    if not isinstance(associations, dict):
        associations = {}

    associations[ext] = value
    data[EXTENSIONS_KEY] = associations
    _save_json(EXT_ASSOCIATION_FILE, data)


def clear_ext_association(ext: str) -> None:
    """Remove the stored association for ``ext`` if present."""

    ext = ext.strip().lower()
    if not ext:
        return

    data = _load_json(EXT_ASSOCIATION_FILE)
    associations = data.get(EXTENSIONS_KEY)
    if not isinstance(associations, dict):
        return

    associations.pop(ext, None)
    data[EXTENSIONS_KEY] = associations
    _save_json(EXT_ASSOCIATION_FILE, data)


def get_all_associations() -> Dict[str, str]:
    """Return the full mapping of stored extension associations."""

    return _load_association_map()


def _ensure_config_dir() -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        # Config directory creation failures will manifest later when we
        # try to write files; no need to be noisy here.
        pass


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding=ENCODING_UTF8) as f:
            return json.load(f)
    except Exception:
        # Corrupt file? Treat as empty; the Bash side will behave as if
        # this is a first run and can repopulate values.
        return {}


def _save_json(path: Path, data: Dict[str, Any]) -> None:
    _ensure_config_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding=ENCODING_UTF8) as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(path)


def _apply_defaults(settings: Dict[str, Any], defaults: Dict[str, Any]) -> bool:
    updated = False
    for key, value in defaults.items():
        if key not in settings:
            settings[key] = copy.deepcopy(value)
            updated = True
    return updated


def cmd_export_shell() -> int:
    """
    Emit shell assignments for all known settings keys.

    Example output (one per line):

        AP_APPIMAGE='/path/to/AppImage'
        BIZHAWK_EXE='/path/to/EmuHawk.exe'
    """
    settings = load_settings()
    for key in SETTINGS_KEYS + PATH_SETTINGS_KEYS:
        if key in settings and settings[key] not in (None, EMPTY_STRING):
            value = str(settings[key])
            # Use shlex.quote to make it safe for eval in Bash.
            print(f"{key}={shlex.quote(value)}")
    return 0


def cmd_save_from_env() -> int:
    """
    Persist settings from the current environment.

    For each known key we take the current environment value and, if it is
    non-empty and explicitly set in the environment, write it to the
    settings file. This prevents clobbering previously stored values
    (for example BIZHAWK_EXE) with empty strings when shell variables
    are unset.
    """
    combined_settings = load_settings()
    settings = {
        k: v
        for k, v in combined_settings.items()
        if k not in INSTALL_STATE_KEYS and k not in PATH_SETTINGS_KEYS
    }
    path_settings = {k: v for k, v in combined_settings.items() if k in PATH_SETTINGS_KEYS}
    install_state = {k: v for k, v in combined_settings.items() if k in INSTALL_STATE_KEYS}

    for key in SETTINGS_KEYS:
        if key in os.environ and os.environ.get(key, EMPTY_STRING) != EMPTY_STRING:
            if key in INSTALL_STATE_KEYS:
                install_state[key] = os.environ[key]
            else:
                settings[key] = os.environ[key]

    for key in PATH_SETTINGS_KEYS:
        if key in os.environ and os.environ.get(key, EMPTY_STRING) != EMPTY_STRING:
            path_settings[key] = os.environ[key]

    save_settings({**settings, **path_settings, **install_state})
    return 0


def cmd_get_ext(ext: str) -> int:
    """
    Print the stored behaviour for an extension, if any.

    On success (value exists) prints it and exits 0.
    If no value, prints nothing and exits 1.
    """
    ext = ext.strip().lower()
    if not ext:
        return 1
    behaviors = _load_json(EXT_BEHAVIOR_FILE)
    value = behaviors.get(ext)
    if value is None or value == EMPTY_STRING:
        return 1
    print(str(value))
    return 0


def cmd_set_ext(ext: str, value: str) -> int:
    """
    Set the behaviour for an extension and persist it.
    """
    ext = ext.strip().lower()
    if not ext:
        return 1
    behaviors = _load_json(EXT_BEHAVIOR_FILE)
    behaviors[ext] = value
    _save_json(EXT_BEHAVIOR_FILE, behaviors)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            "Usage: ap_bizhelper_config.py <command> [args...]\n"
            "Commands: export-shell, save-from-env, get-ext EXT, set-ext EXT VALUE,\n"
            "          get-association-mode, set-association-mode MODE,\n"
            "          get-ext-association EXT, set-ext-association EXT VALUE,\n"
            "          clear-ext-association EXT",
            file=sys.stderr,
        )
        return 1

    cmd = argv[1]
    if cmd == "export-shell":
        return cmd_export_shell()
    if cmd == "save-from-env":
        return cmd_save_from_env()
    if cmd == "get-ext":
        if len(argv) != 3:
            print("Usage: get-ext <extension>", file=sys.stderr)
            return 1
        return cmd_get_ext(argv[2])
    if cmd == "set-ext":
        if len(argv) != 4:
            print("Usage: set-ext <extension> <value>", file=sys.stderr)
            return 1
        return cmd_set_ext(argv[2], argv[3])
    if cmd == "get-association-mode":
        print(get_association_mode())
        return 0
    if cmd == "set-association-mode":
        if len(argv) != 3:
            print("Usage: set-association-mode <prompt|enabled|disabled>", file=sys.stderr)
            return 1
        set_association_mode(argv[2])
        return 0
    if cmd == "get-ext-association":
        if len(argv) != 3:
            print("Usage: get-ext-association <extension>", file=sys.stderr)
            return 1
        assoc = get_ext_association(argv[2])
        if assoc is None:
            return 1
        print(assoc)
        return 0
    if cmd == "set-ext-association":
        if len(argv) != 4:
            print("Usage: set-ext-association <extension> <value>", file=sys.stderr)
            return 1
        set_ext_association(argv[2], argv[3])
        return 0
    if cmd == "clear-ext-association":
        if len(argv) != 3:
            print("Usage: clear-ext-association <extension>", file=sys.stderr)
            return 1
        clear_ext_association(argv[2])
        return 0

    print(
        f"Unknown command: {cmd}\n"
        "Commands: export-shell, save-from-env, get-ext EXT, set-ext EXT VALUE,\n"
        "          get-association-mode, set-association-mode MODE,\n"
        "          get-ext-association EXT, set-ext-association EXT VALUE,\n"
        "          clear-ext-association EXT",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
