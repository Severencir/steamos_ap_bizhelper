from __future__ import annotations

import copy
import importlib.resources as resources
import os
import shutil
from pathlib import Path
from typing import Any, Optional

from .ap_bizhelper_config import PATH_SETTINGS_DEFAULTS, load_settings
from .constants import (
    BIZHAWK_ENTRY_LUA_FILENAME,
    BIZHAWK_HELPERS_LIB_DIRNAME,
    BIZHAWK_HELPERS_ROOT_KEY,
    BIZHAWK_RUNNER_FILENAME,
    BIZHELPER_APPIMAGE_KEY,
    DIALOG_SHIM_KDIALOG_FILENAME,
    DIALOG_SHIM_KDIALOG_SHIM_FILENAME,
    DIALOG_SHIM_PORTAL_FILENAME,
    DIALOG_SHIM_PORTAL_SHIM_FILENAME,
    DIALOG_SHIM_REAL_KDIALOG_ENV,
    DIALOG_SHIM_REAL_PORTAL_ENV,
    DIALOG_SHIM_REAL_ZENITY_ENV,
    DIALOG_SHIM_ZENITY_FILENAME,
    DIALOG_SHIM_ZENITY_SHIM_FILENAME,
    SAVE_HELPER_STAGED_FILENAME,
)
from .logging_utils import (
    RUN_ID_ENV,
    SHIM_LOG_ENV,
    TIMESTAMP_ENV,
    AppLogger,
)


HELPER_LIB_MODULES = (
    "__init__.py",
    "ap_bizhelper_config.py",
    "constants.py",
    "dialogs.py",
    "dialog_shim.py",
    "logging_utils.py",
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


def get_helpers_lib_root(settings: dict[str, Any]) -> Path:
    return get_helpers_root(settings) / BIZHAWK_HELPERS_LIB_DIRNAME


def _stage_script(target: Path, source: Path, *, make_executable: bool) -> bool:
    if target.is_file():
        return True
    if target.exists():
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        except Exception:
            return False
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


def _get_bizhelper_appimage(settings: dict[str, Any]) -> Path | None:
    appimage_value = str(settings.get(BIZHELPER_APPIMAGE_KEY) or "")
    if not appimage_value:
        appimage_value = str(os.environ.get("APPIMAGE") or "")
    if not appimage_value:
        return None
    appimage = Path(appimage_value)
    if not appimage.is_file():
        return None
    return appimage


def _copy_tree(source: Path, target: Path) -> bool:
    if target.is_dir():
        return True
    if target.exists():
        try:
            if target.is_file():
                target.unlink()
            else:
                shutil.rmtree(target)
        except Exception:
            return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)
        return True
    except Exception:
        return False


def _copy_file(source: Path, target: Path) -> bool:
    if target.is_file():
        return True
    if target.exists():
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        except Exception:
            return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)
        return True
    except Exception:
        return False


def stage_helper_lib(settings: dict[str, Any]) -> dict[str, tuple[Path, bool]]:
    helpers_lib = get_helpers_lib_root(settings)
    package_root = helpers_lib / "ap_bizhelper"
    package_root.mkdir(parents=True, exist_ok=True)
    staged: dict[str, tuple[Path, bool]] = {}
    for module in HELPER_LIB_MODULES:
        target = package_root / module
        staged[module] = (
            target,
            stage_helper_resource(module, target, make_executable=False),
        )
    return staged


def stage_bizhawk_helpers(settings: dict[str, Any]) -> dict[str, tuple[Path, bool]]:
    helpers_root = get_helpers_root(settings)
    helpers_root.mkdir(parents=True, exist_ok=True)

    staged = ensure_staged_runtime(settings)

    staged.update(
        {
        BIZHAWK_RUNNER_FILENAME: (
            helpers_root / BIZHAWK_RUNNER_FILENAME,
            stage_helper_resource(
                BIZHAWK_RUNNER_FILENAME,
                helpers_root / BIZHAWK_RUNNER_FILENAME,
                make_executable=True,
            ),
        ),
        BIZHAWK_ENTRY_LUA_FILENAME: (
            helpers_root / BIZHAWK_ENTRY_LUA_FILENAME,
            stage_helper_resource(
                BIZHAWK_ENTRY_LUA_FILENAME,
                helpers_root / BIZHAWK_ENTRY_LUA_FILENAME,
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
    )
    return staged


def _dialog_shim_script(entrypoint: str) -> str:
    return f"""#!/usr/bin/env python3
from ap_bizhelper.dialog_shim import {entrypoint}


if __name__ == "__main__":
    {entrypoint}()
"""


def _stage_dialog_shim_script(target: Path, entrypoint: str) -> bool:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_dialog_shim_script(entrypoint), encoding="utf-8")
        target.chmod(target.stat().st_mode | 0o111)
        return True
    except Exception:
        return False


def _dialog_shim_wrapper_script(shim_name: str) -> str:
    return f"""#!/bin/sh
shim_path="$(dirname "$0")/{shim_name}"
exec "$shim_path" "$@"
"""


def _stage_dialog_shim_wrapper_script(target: Path, shim_name: str) -> bool:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_dialog_shim_wrapper_script(shim_name), encoding="utf-8")
        target.chmod(target.stat().st_mode | 0o111)
        return True
    except Exception:
        return False


def stage_dialog_shim_scripts(settings: dict[str, Any]) -> dict[Path, bool]:
    helpers_root = get_helpers_root(settings)
    helpers_root.mkdir(parents=True, exist_ok=True)
    zenity_shim_path = helpers_root / DIALOG_SHIM_ZENITY_SHIM_FILENAME
    kdialog_shim_path = helpers_root / DIALOG_SHIM_KDIALOG_SHIM_FILENAME
    portal_shim_path = helpers_root / DIALOG_SHIM_PORTAL_SHIM_FILENAME
    return {
        zenity_shim_path: _stage_dialog_shim_script(zenity_shim_path, "shim_main"),
        kdialog_shim_path: _stage_dialog_shim_script(kdialog_shim_path, "kdialog_main"),
        portal_shim_path: _stage_dialog_shim_script(
            portal_shim_path, "portal_file_chooser_main"
        ),
        helpers_root / DIALOG_SHIM_ZENITY_FILENAME: _stage_dialog_shim_wrapper_script(
            helpers_root / DIALOG_SHIM_ZENITY_FILENAME,
            DIALOG_SHIM_ZENITY_SHIM_FILENAME,
        ),
        helpers_root / DIALOG_SHIM_KDIALOG_FILENAME: _stage_dialog_shim_wrapper_script(
            helpers_root / DIALOG_SHIM_KDIALOG_FILENAME,
            DIALOG_SHIM_KDIALOG_SHIM_FILENAME,
        ),
        helpers_root / DIALOG_SHIM_PORTAL_FILENAME: _stage_dialog_shim_wrapper_script(
            helpers_root / DIALOG_SHIM_PORTAL_FILENAME,
            DIALOG_SHIM_PORTAL_SHIM_FILENAME,
        ),
    }


def ensure_staged_runtime(
    settings: dict[str, Any], logger: Optional[AppLogger] = None
) -> dict[str, Path]:
    helpers_root = get_helpers_root(settings)
    helpers_lib = get_helpers_lib_root(settings)
    helpers_root.mkdir(parents=True, exist_ok=True)
    helpers_lib.mkdir(parents=True, exist_ok=True)

    staged: dict[str, tuple[Path, bool]] = {}
    staged.update(stage_helper_lib(settings))

    shim_staged = stage_dialog_shim_scripts(settings)
    if logger:
        for module, (path, ok) in staged.items():
            if not ok:
                logger.log(
                    f"Failed staging helper module: {module} -> {path}",
                    include_context=True,
                    location="staging",
                    level="WARNING",
                )
        for path, ok in shim_staged.items():
            if not ok:
                logger.log(
                    f"Failed staging dialog shim script: {path}",
                    include_context=True,
                    location="staging",
                    level="WARNING",
                )
    return {"helpers_root": helpers_root, "helpers_lib": helpers_lib}


def prepare_dialog_shim_env(logger: Optional[AppLogger] = None) -> Optional[dict[str, str]]:
    """Return environment overrides for dialog shims without mutating disk."""
    try:
        settings = load_settings()
    except Exception:
        return None

    helpers_root = get_helpers_root(settings)
    helpers_lib = get_helpers_lib_root(settings)
    if not helpers_root.is_dir():
        return None

    search_path = os.environ.get("PATH", "")
    cleaned_path = os.pathsep.join(
        [p for p in search_path.split(os.pathsep) if p and Path(p) != helpers_root]
    )

    real_zenity = shutil.which(DIALOG_SHIM_ZENITY_FILENAME, path=cleaned_path)
    real_kdialog = shutil.which(DIALOG_SHIM_KDIALOG_FILENAME, path=cleaned_path)
    real_portal = shutil.which(DIALOG_SHIM_PORTAL_FILENAME, path=cleaned_path)

    pkg_root = Path(__file__).resolve().parent.parent
    pythonpath = os.environ.get("PYTHONPATH", "")
    pythonpath_parts = [pkg_root.as_posix()]
    if helpers_lib.is_dir():
        pythonpath_parts.insert(0, helpers_lib.as_posix())
    env = {
        "PATH": helpers_root.as_posix() + os.pathsep + os.environ.get("PATH", ""),
        "PYTHONPATH": os.pathsep.join(pythonpath_parts)
        + (os.pathsep + pythonpath if pythonpath else ""),
        "AP_BIZHELPER_SHIM_DIR": helpers_root.as_posix(),
        DIALOG_SHIM_REAL_ZENITY_ENV: real_zenity or "",
        DIALOG_SHIM_REAL_KDIALOG_ENV: real_kdialog or "",
        DIALOG_SHIM_REAL_PORTAL_ENV: real_portal or "",
    }
    session_env: dict[str, str] = {}
    if logger:
        session_env = logger.session_environ()
        env[SHIM_LOG_ENV] = str(logger.component_log_path("zenity-shim", subdir="shim"))
    else:
        if RUN_ID_ENV in os.environ:
            session_env[RUN_ID_ENV] = os.environ[RUN_ID_ENV]
        if TIMESTAMP_ENV in os.environ:
            session_env[TIMESTAMP_ENV] = os.environ[TIMESTAMP_ENV]
        if SHIM_LOG_ENV in os.environ:
            env[SHIM_LOG_ENV] = os.environ[SHIM_LOG_ENV]
    env.update({k: v for k, v in session_env.items() if v})
    return env
