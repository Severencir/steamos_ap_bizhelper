from __future__ import annotations

import copy
import importlib.resources as resources
import os
from pathlib import Path
from typing import Any

from .ap_bizhelper_config import PATH_SETTINGS_DEFAULTS
from .constants import (
    BIZHAWK_ENTRY_LUA_FILENAME,
    BIZHAWK_HELPERS_BIN_DIRNAME,
    BIZHAWK_HELPERS_LIB_DIRNAME,
    BIZHAWK_HELPERS_ROOT_KEY,
    BIZHAWK_RUNNER_FILENAME,
    SAVE_HELPER_STAGED_FILENAME,
)


def _apply_defaults(settings: dict[str, Any], defaults: dict[str, Any]) -> None:
    for key, value in defaults.items():
        if key not in settings:
            settings[key] = copy.deepcopy(value)


def get_helpers_root(settings: dict[str, Any]) -> Path:
    _apply_defaults(
        settings,
        {BIZHAWK_HELPERS_ROOT_KEY: PATH_SETTINGS_DEFAULTS[BIZHAWK_HELPERS_ROOT_KEY]},
    )
    return Path(os.path.expanduser(str(settings[BIZHAWK_HELPERS_ROOT_KEY])))


def get_helpers_bin_root(settings: dict[str, Any]) -> Path:
    return get_helpers_root(settings) / BIZHAWK_HELPERS_BIN_DIRNAME


def get_helpers_lib_root(settings: dict[str, Any]) -> Path:
    return get_helpers_root(settings) / BIZHAWK_HELPERS_LIB_DIRNAME


def _stage_script(target: Path, source: Path, *, make_executable: bool) -> bool:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        data = source.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        target.write_bytes(data)
        if make_executable:
            target.chmod(target.stat().st_mode | 0o111)
        return True
    except Exception:
        return False


def stage_helper_resource(resource_name: str, target: Path, *, make_executable: bool) -> bool:
    try:
        helper_resource = resources.files(__package__).joinpath(resource_name)
    except (ModuleNotFoundError, AttributeError):
        return False

    with resources.as_file(helper_resource) as helper_source:
        if not helper_source.is_file():
            return False
        return _stage_script(target, helper_source, make_executable=make_executable)


def stage_helper_lib(settings: dict[str, Any]) -> dict[str, tuple[Path, bool]]:
    helpers_lib = get_helpers_lib_root(settings)
    package_root = helpers_lib / "ap_bizhelper"
    package_root.mkdir(parents=True, exist_ok=True)
    modules = [
        "__init__.py",
        "ap_bizhelper_config.py",
        "constants.py",
        "dialogs.py",
        "dialog_shim.py",
        "logging_utils.py",
    ]
    staged: dict[str, tuple[Path, bool]] = {}
    for module in modules:
        target = package_root / module
        staged[module] = (
            target,
            stage_helper_resource(module, target, make_executable=False),
        )
    return staged


def stage_bizhawk_helpers(settings: dict[str, Any]) -> dict[str, tuple[Path, bool]]:
    helpers_root = get_helpers_root(settings)
    helpers_bin = helpers_root / BIZHAWK_HELPERS_BIN_DIRNAME
    helpers_root.mkdir(parents=True, exist_ok=True)
    helpers_bin.mkdir(parents=True, exist_ok=True)

    staged = {
        BIZHAWK_RUNNER_FILENAME: (
            helpers_bin / BIZHAWK_RUNNER_FILENAME,
            stage_helper_resource(
                BIZHAWK_RUNNER_FILENAME,
                helpers_bin / BIZHAWK_RUNNER_FILENAME,
                make_executable=True,
            ),
        ),
        BIZHAWK_ENTRY_LUA_FILENAME: (
            helpers_bin / BIZHAWK_ENTRY_LUA_FILENAME,
            stage_helper_resource(
                BIZHAWK_ENTRY_LUA_FILENAME,
                helpers_bin / BIZHAWK_ENTRY_LUA_FILENAME,
                make_executable=False,
            ),
        ),
        SAVE_HELPER_STAGED_FILENAME: (
            helpers_root / SAVE_HELPER_STAGED_FILENAME,
            stage_helper_resource(
                SAVE_HELPER_STAGED_FILENAME,
                helpers_root / SAVE_HELPER_STAGED_FILENAME,
                make_executable=True,
            ),
        ),
    }
    staged.update(stage_helper_lib(settings))
    return staged
