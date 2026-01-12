from __future__ import annotations

import copy
import importlib.resources as resources
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .ap_bizhelper_config import PATH_SETTINGS_DEFAULTS
from .constants import (
    BIZHAWK_ENTRY_LUA_FILENAME,
    BIZHAWK_HELPERS_APPIMAGE_MANIFEST,
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


def _helpers_appimage_manifest_path(stage_root: Path) -> Path:
    return stage_root / BIZHAWK_HELPERS_APPIMAGE_MANIFEST


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


def _iter_tree_entries(root: Path) -> tuple[list[Path], list[Path]]:
    files: list[Path] = []
    dirs: list[Path] = [root]
    for entry in root.rglob("*"):
        if entry.is_dir():
            dirs.append(entry)
        else:
            files.append(entry)
    return files, dirs


def _load_appimage_manifest(manifest_path: Path) -> dict[str, list[str]] | None:
    if not manifest_path.is_file():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    files = data.get("files")
    dirs = data.get("dirs")
    if not isinstance(files, list) or not isinstance(dirs, list):
        return None
    if not all(isinstance(item, str) for item in files + dirs):
        return None
    return {"files": files, "dirs": dirs}


def _appimage_stage_complete(stage_root: Path, manifest: dict[str, list[str]]) -> bool:
    for entry in manifest.get("dirs", []):
        if not (stage_root / entry).is_dir():
            return False
    for entry in manifest.get("files", []):
        if not (stage_root / entry).is_file():
            return False
    return True


def _write_appimage_manifest(stage_root: Path, manifest: dict[str, list[str]]) -> None:
    manifest_path = _helpers_appimage_manifest_path(stage_root)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def stage_pyside6_from_appimage(settings: dict[str, Any]) -> dict[str, tuple[Path, bool]]:
    appimage = _get_bizhelper_appimage(settings)
    if not appimage:
        return {}

    stage_root = get_helpers_appimage_root(settings)
    manifest_path = _helpers_appimage_manifest_path(stage_root)
    manifest = _load_appimage_manifest(manifest_path)
    if manifest and _appimage_stage_complete(stage_root, manifest):
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

        packages = _iter_pyside6_packages(squashfs_root)
        if not packages:
            return {}

        manifest_files: list[Path] = []
        manifest_dirs: list[Path] = []
        for package in packages:
            target = stage_root / package.relative_to(squashfs_root)
            staged[str(package)] = (target, _copy_tree(package, target))
            files, dirs = _iter_tree_entries(package)
            manifest_files.extend(files)
            manifest_dirs.extend(dirs)

        for library in _iter_qt_library_files(squashfs_root):
            target = stage_root / library.relative_to(squashfs_root)
            staged[str(library)] = (target, _copy_file(library, target))
            manifest_files.append(library)

        for plugin_dir in _iter_qt_plugin_dirs(squashfs_root):
            target = stage_root / plugin_dir.relative_to(squashfs_root)
            staged[str(plugin_dir)] = (target, _copy_tree(plugin_dir, target))
            files, dirs = _iter_tree_entries(plugin_dir)
            manifest_files.extend(files)
            manifest_dirs.extend(dirs)

        manifest_payload = {
            "files": sorted({str(path.relative_to(squashfs_root)) for path in manifest_files}),
            "dirs": sorted({str(path.relative_to(squashfs_root)) for path in manifest_dirs}),
        }
        _write_appimage_manifest(stage_root, manifest_payload)

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
