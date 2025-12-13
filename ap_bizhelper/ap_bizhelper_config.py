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

import json
import os
import shlex
import sys
from pathlib import Path
from typing import Dict, Any, Optional

# These match the paths used by the original Bash script.
CONFIG_DIR = Path(os.path.expanduser("~/.config/ap_bizhelper_test"))
SETTINGS_FILE = CONFIG_DIR / "settings.json"
INSTALL_STATE_FILE = CONFIG_DIR / "install_state.json"
EXT_BEHAVIOR_FILE = CONFIG_DIR / "ext_behavior.json"
EXT_ASSOCIATION_FILE = CONFIG_DIR / "ext_associations.json"

# Keys we expose back to Bash as shell variables.
INSTALL_STATE_KEYS = {
    "AP_APPIMAGE",
    "AP_VERSION",
    "AP_SKIP_VERSION",
    "BIZHAWK_EXE",
    "BIZHAWK_VERSION",
    "BIZHAWK_SKIP_VERSION",
    "BIZHAWK_RUNNER",
    "PROTON_BIN",
    "BIZHAWK_AP_CONNECTOR_VERSION",
    "BIZHAWK_SNI_VERSION",
}

SETTINGS_KEYS = [
    "AP_APPIMAGE",
    "BIZHAWK_EXE",
    "PROTON_BIN",
    "BIZHAWK_RUNNER",
    "SFC_LUA_PATH",
    "ENABLE_GAMEPAD_FILE_DIALOG",
    "QT_FONT_SCALE",
    "QT_MIN_POINT_SIZE",
    "QT_FILE_NAME_FONT_SCALE",
    "QT_FILE_DIALOG_WIDTH",
    "QT_FILE_DIALOG_HEIGHT",
    "QT_FILE_DIALOG_MAXIMIZE",
    "AP_VERSION",
    "AP_SKIP_VERSION",
    "BIZHAWK_VERSION",
    "BIZHAWK_SKIP_VERSION",
    "AP_DESKTOP_SHORTCUT",
    "STEAM_APPID",
]


def _load_install_state() -> Dict[str, Any]:
    return _load_json(INSTALL_STATE_FILE)


def load_settings() -> Dict[str, Any]:
    """Return the persisted settings and install state as one mapping."""

    settings = _load_json(SETTINGS_FILE)
    settings.update(_load_install_state())
    return settings


def save_settings(settings: Dict[str, Any]) -> None:
    """Persist the given settings mapping to disk.

    Settings are split between general parameters and installation state so that
    installation paths/versions can be managed independently.
    """

    existing_settings = _load_json(SETTINGS_FILE)
    existing_install_state = _load_install_state()

    general_updates = {k: v for k, v in settings.items() if k not in INSTALL_STATE_KEYS}
    install_updates = {k: v for k, v in settings.items() if k in INSTALL_STATE_KEYS}

    if general_updates:
        merged_settings = {**existing_settings, **general_updates}
        _save_json(SETTINGS_FILE, merged_settings)

    if install_updates:
        merged_install_state = {**existing_install_state, **install_updates}
        _save_json(INSTALL_STATE_FILE, merged_install_state)


def get_ext_behavior(ext: str) -> Optional[str]:
    """Return the stored behavior for ``ext`` (case-insensitive) or ``None``."""

    ext = ext.strip().lower()
    if not ext:
        return None
    behaviors = _load_json(EXT_BEHAVIOR_FILE)
    value = behaviors.get(ext)
    if value is None or value == "":
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
    mode = str(data.get("mode") or "prompt").lower()
    if mode not in {"prompt", "enabled", "disabled"}:
        return "prompt"
    return mode


def set_association_mode(mode: str) -> None:
    """Persist the association mode (``prompt``, ``enabled``, or ``disabled``)."""

    mode = str(mode or "").strip().lower()
    if mode not in {"prompt", "enabled", "disabled"}:
        return

    data = _load_json(EXT_ASSOCIATION_FILE)
    data["mode"] = mode
    _save_json(EXT_ASSOCIATION_FILE, data)


def _load_association_map() -> Dict[str, str]:
    data = _load_json(EXT_ASSOCIATION_FILE)
    associations = data.get("extensions")
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
    associations = data.get("extensions")
    if not isinstance(associations, dict):
        associations = {}

    associations[ext] = value
    data["extensions"] = associations
    _save_json(EXT_ASSOCIATION_FILE, data)


def clear_ext_association(ext: str) -> None:
    """Remove the stored association for ``ext`` if present."""

    ext = ext.strip().lower()
    if not ext:
        return

    data = _load_json(EXT_ASSOCIATION_FILE)
    associations = data.get("extensions")
    if not isinstance(associations, dict):
        return

    associations.pop(ext, None)
    data["extensions"] = associations
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
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # Corrupt file? Treat as empty; the Bash side will behave as if
        # this is a first run and can repopulate values.
        return {}


def _save_json(path: Path, data: Dict[str, Any]) -> None:
    _ensure_config_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(path)


def cmd_export_shell() -> int:
    """
    Emit shell assignments for all known settings keys.

    Example output (one per line):

        AP_APPIMAGE='/path/to/AppImage'
        BIZHAWK_EXE='/path/to/EmuHawk.exe'
    """
    settings = _load_json(SETTINGS_FILE)
    for key in SETTINGS_KEYS:
        if key in settings and settings[key] not in (None, ""):
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
    settings = _load_json(SETTINGS_FILE)
    install_state = _load_install_state()

    for key in SETTINGS_KEYS:
        if key in os.environ and os.environ.get(key, "") != "":
            if key in INSTALL_STATE_KEYS:
                install_state[key] = os.environ[key]
            else:
                settings[key] = os.environ[key]

    _save_json(SETTINGS_FILE, settings)
    _save_json(INSTALL_STATE_FILE, install_state)
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
    if value is None or value == "":
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
