from __future__ import annotations

import copy
import importlib.resources as resources
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from .ap_bizhelper_config import PATH_SETTINGS_DEFAULTS, load_settings
from .constants import (
    BIZHAWK_ENTRY_LUA_FILENAME,
    BIZHAWK_HELPERS_APPIMAGE_MANIFEST,
    BIZHAWK_HELPERS_APPIMAGE_DIRNAME,
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
    "gamepad_input.py",
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
import os
from pathlib import Path
import sys


_REAL_ENV_BY_ENTRYPOINT = {{
    "shim_main": "{DIALOG_SHIM_REAL_ZENITY_ENV}",
    "kdialog_main": "{DIALOG_SHIM_REAL_KDIALOG_ENV}",
    "portal_file_chooser_main": "{DIALOG_SHIM_REAL_PORTAL_ENV}",
}}

_BOOTSTRAP_LOGGER = None


def _prepend_sys_path(path: Path) -> None:
    if not path.is_dir():
        return
    path_str = path.as_posix()
    if path_str not in sys.path:
        sys.path.insert(0, path_str)


def _prepend_env_path(key: str, value: Path) -> None:
    if not value.is_dir():
        return
    value_str = value.as_posix()
    existing = os.environ.get(key, "")
    if existing:
        os.environ[key] = value_str + os.pathsep + existing
    else:
        os.environ[key] = value_str


def _stage_pyside6_paths() -> None:
    helpers_root = Path(__file__).resolve().parent
    helpers_lib = helpers_root / "{BIZHAWK_HELPERS_LIB_DIRNAME}"
    helpers_appimage = helpers_lib / "{BIZHAWK_HELPERS_APPIMAGE_DIRNAME}"
    _prepend_sys_path(helpers_lib)
    for site_packages in helpers_appimage.glob("usr/lib/python*/site-packages"):
        _prepend_sys_path(site_packages)
    _prepend_env_path("LD_LIBRARY_PATH", helpers_appimage / "usr/lib")
    for plugin_rel in ("usr/lib/qt6/plugins", "usr/lib/qt/plugins"):
        _prepend_env_path("QT_PLUGIN_PATH", helpers_appimage / plugin_rel)


def _bootstrap_logger():
    global _BOOTSTRAP_LOGGER
    if _BOOTSTRAP_LOGGER is not None:
        return _BOOTSTRAP_LOGGER
    from ap_bizhelper.logging_utils import create_component_logger, SHIM_LOG_ENV

    _BOOTSTRAP_LOGGER = create_component_logger(
        "zenity-shim", env_var=SHIM_LOG_ENV, subdir="shim"
    )
    return _BOOTSTRAP_LOGGER


def _fallback_to_real_dialog(argv: list[str], reason: str) -> None:
    logger = _bootstrap_logger()
    if logger:
        logger.log(
            f"PySide6 unavailable, falling back to real dialog: {{reason}}",
            include_context=True,
            location="shim-bootstrap",
            level="ERROR",
        )
    env_key = _REAL_ENV_BY_ENTRYPOINT.get("{entrypoint}", "")
    real_dialog = os.environ.get(env_key, "")
    if real_dialog:
        os.execv(real_dialog, [real_dialog, *argv[1:]])
    sys.stderr.write(f"Dialog shim fallback failed (missing {{env_key}}).\\n")
    sys.exit(127)


def _ensure_pyside6(entrypoint: str) -> None:
    logger = _bootstrap_logger()
    if logger:
        logger.log(
            f"Dialog shim bootstrap for {{entrypoint}}",
            include_context=True,
            location="shim-bootstrap",
        )
    try:
        import PySide6  # noqa: F401
    except Exception as exc:
        missing = getattr(exc, "name", "") or str(exc)
        if logger:
            logger.log(
                f"PySide6 import failed (missing={{missing}}): {{exc}}",
                include_context=True,
                location="shim-bootstrap",
                level="ERROR",
            )
        _fallback_to_real_dialog(sys.argv, missing)


_stage_pyside6_paths()
_ensure_pyside6("{entrypoint}")

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
    staged.update(stage_pyside6_from_appimage(settings))

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
