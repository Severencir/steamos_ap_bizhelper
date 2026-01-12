from __future__ import annotations

import copy
import importlib.resources as resources
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .ap_bizhelper_config import PATH_SETTINGS_DEFAULTS
from .constants import (
    BIZHAWK_ENTRY_LUA_FILENAME,
    BIZHAWK_HELPERS_APPIMAGE_DIRNAME,
    BIZHAWK_HELPERS_LIB_DIRNAME,
    BIZHAWK_HELPERS_ROOT_KEY,
    BIZHAWK_RUNNER_FILENAME,
    BIZHELPER_APPIMAGE_KEY,
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


def get_helpers_lib_root(settings: dict[str, Any]) -> Path:
    return get_helpers_root(settings) / BIZHAWK_HELPERS_LIB_DIRNAME


def get_helpers_appimage_root(settings: dict[str, Any]) -> Path:
    return get_helpers_lib_root(settings) / BIZHAWK_HELPERS_APPIMAGE_DIRNAME


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
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)
        return True
    except Exception:
        return False


def _copy_file(source: Path, target: Path) -> bool:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)
        return True
    except Exception:
        return False


def _iter_pyside6_packages(appimage_root: Path) -> list[Path]:
    packages: list[Path] = []
    for site_packages in appimage_root.glob("usr/lib/python*/site-packages"):
        for package in ("PySide6", "shiboken6"):
            candidate = site_packages / package
            if candidate.is_dir():
                packages.append(candidate)
    return packages


def _iter_qt_library_files(appimage_root: Path) -> list[Path]:
    lib_root = appimage_root / "usr" / "lib"
    if not lib_root.is_dir():
        return []
    patterns = (
        "libQt6*.so*",
        "libicu*.so*",
        "libpyside6*.so*",
        "libshiboken6*.so*",
    )
    libraries: list[Path] = []
    for pattern in patterns:
        libraries.extend(lib_root.glob(pattern))
    return libraries


def _iter_qt_plugin_dirs(appimage_root: Path) -> list[Path]:
    candidates = [
        appimage_root / "usr" / "lib" / "qt6" / "plugins",
        appimage_root / "usr" / "lib" / "qt" / "plugins",
    ]
    return [candidate for candidate in candidates if candidate.is_dir()]


def stage_pyside6_from_appimage(settings: dict[str, Any]) -> dict[str, tuple[Path, bool]]:
    appimage = _get_bizhelper_appimage(settings)
    if not appimage:
        return {}

    staged: dict[str, tuple[Path, bool]] = {}
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            result = subprocess.run(
                [str(appimage), "--appimage-extract"],
                cwd=tmpdir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            return {}
        if result.returncode != 0:
            return {}

        squashfs_root = Path(tmpdir) / "squashfs-root"
        if not squashfs_root.is_dir():
            return {}

        stage_root = get_helpers_appimage_root(settings)
        packages = _iter_pyside6_packages(squashfs_root)
        if not packages:
            return {}

        for package in packages:
            target = stage_root / package.relative_to(squashfs_root)
            staged[str(package)] = (target, _copy_tree(package, target))

        for library in _iter_qt_library_files(squashfs_root):
            target = stage_root / library.relative_to(squashfs_root)
            staged[str(library)] = (target, _copy_file(library, target))

        for plugin_dir in _iter_qt_plugin_dirs(squashfs_root):
            target = stage_root / plugin_dir.relative_to(squashfs_root)
            staged[str(plugin_dir)] = (target, _copy_tree(plugin_dir, target))

    return staged


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
    helpers_root.mkdir(parents=True, exist_ok=True)

    staged = {
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
    staged.update(stage_helper_lib(settings))
    staged.update(stage_pyside6_from_appimage(settings))
    return staged
