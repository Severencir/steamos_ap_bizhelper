#!/usr/bin/env python3

from __future__ import annotations

import importlib.resources as resources
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .ap_bizhelper_ap import _is_newer_version, _normalize_asset_digest, download_with_progress
from .ap_bizhelper_config import (
    CONFIG_DIR,
    get_path_setting,
    load_settings as _load_shared_settings,
    save_settings as _save_shared_settings,
)
from .constants import (
    APP_COMPONENTS_DIR,
    BIZHAWK_ENTRY_LUA_FILENAME,
    BIZHAWK_DESKTOP_SHORTCUT_KEY,
    BIZHAWK_EXE_KEY,
    BIZHAWK_ENTRY_LUA_PATH_KEY,
    BIZHAWK_INSTALL_DIR_KEY,
    BIZHAWK_LATEST_SEEN_KEY,
    BIZHAWK_RUNNER_KEY,
    BIZHAWK_RUNTIME_DOWNLOAD_KEY,
    BIZHAWK_RUNTIME_ROOT_KEY,
    BIZHAWK_SKIP_VERSION_KEY,
    BIZHAWK_VERSION_KEY,
    DATA_DIR,
    SAVE_HELPER_STAGED_FILENAME,
    SAVE_MIGRATION_HELPER_PATH_KEY,
)
from .dialogs import (
    question_dialog as _qt_question_dialog,
    select_file_dialog as _select_file_dialog,
    error_dialog,
    info_dialog,
)
from .logging_utils import get_app_logger

SETTINGS_FILE = CONFIG_DIR / "settings.json"
APP_LOGGER = get_app_logger()

ARCHIVE_READ = "r:*"
ARCHIVE_TAR_GZ_SUFFIX = ".tar.gz"
ARCHIVE_DESTINATION_ERROR_PREFIX = "Archive entry escapes destination: "
ARCHIVE_MEMBER_READ_PREFIX = "Could not read archive member: "
ARCHIVE_SYMLINK_ERROR = "Archives containing symbolic links are not supported"
BIZHAWK_INSTALL_MARKER = "EmuHawkMono.sh"
BIZHAWK_LINUX_SUFFIX = "linux-x64.tar.gz"
BIZHAWK_UPDATED_PREFIX = "BizHawk updated to "
BIZHAWK_UPDATE_FAILED_PREFIX = "BizHawk update failed: "
BRANCH_DOWNLOAD_URL_KEY = "browser_download_url"
DIGEST_KEY = "digest"
DIALOG_KEY_BIZHAWK_EXE = "bizhawk_exe"
DIALOG_KEY_RUNTIME_ROOT = "bizhawk_runtime_root"
ELLIPSIS = "..."
EMPTY_STRING = ""
ENCODING_UTF8 = "utf-8"
GITHUB_API_LATEST = "https://api.github.com/repos/TASEmulators/BizHawk/releases/latest"
NAME_KEY = "name"
RUNNER_FILENAME = "run_bizhawk.py"
RUNNER_STAGE_FAILED_TEMPLATE = "Failed to stage BizHawk runner helper ({runner})."
SAVE_HELPER_FILENAME = "save_migration_helper.py"
SELECT_EMUHAWK_TITLE = "Select EmuHawkMono.sh"
TAG_NAME_KEY = "tag_name"
TAR_TYPE_HINT = "tar"
YES_VALUE = "yes"
NO_VALUE = "no"
PYSIDE_PACKAGES = ("PySide6", "shiboken6")

ARCH_MIRROR_BASE = "https://geo.mirror.pkgbuild.com"
ARCH_REPO = "extra"

@dataclass(frozen=True)
class RuntimePackage:
    name: str
    filename: str


RUNTIME_PACKAGES = (
    RuntimePackage("mono", "mono-6.12.0.206-1-x86_64.pkg.tar.zst"),
    RuntimePackage("libgdiplus", "libgdiplus-6.2-1-x86_64.pkg.tar.zst"),
    RuntimePackage("lua", "lua-5.4.8-2-x86_64.pkg.tar.zst"),
)


class RuntimeValidationError(RuntimeError):
    pass


def _ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    APP_COMPONENTS_DIR.mkdir(parents=True, exist_ok=True)


def _load_settings() -> Dict[str, Any]:
    return _load_shared_settings()


def _save_settings(settings: Dict[str, Any]) -> None:
    _ensure_dirs()
    _save_shared_settings(settings)


def _github_latest_bizhawk() -> Tuple[str, str, str, str]:
    """
    Return (download_url, version_tag, digest, digest_algorithm) for the latest BizHawk Linux x64 tarball.
    """
    import urllib.request

    req = urllib.request.Request(GITHUB_API_LATEST, headers={"User-Agent": "ap-bizhelper/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read().decode(ENCODING_UTF8)
    j = json.loads(data)

    tag = j.get(TAG_NAME_KEY) or EMPTY_STRING
    assets = j.get("assets") or []

    def _asset_digest(asset: dict[str, Any]) -> Tuple[str, str]:
        digest = asset.get(DIGEST_KEY)
        name = asset.get(NAME_KEY) or "(unknown)"
        if not digest:
            raise RuntimeError(f"BizHawk asset missing digest: {name}")
        try:
            algo, normalized = _normalize_asset_digest(digest)
        except ValueError as exc:
            raise RuntimeError(f"Invalid digest for asset {name}: {exc}") from exc
        return algo, normalized

    for asset in assets:
        name = asset.get(NAME_KEY) or EMPTY_STRING
        if name.endswith(BIZHAWK_LINUX_SUFFIX):
            url = asset.get(BRANCH_DOWNLOAD_URL_KEY)
            if url:
                algo, digest = _asset_digest(asset)
                return url, tag, digest, algo

    raise RuntimeError("Could not find BizHawk linux-x64 tarball in latest release.")


def _validate_tar_member(member: tarfile.TarInfo, dest_dir: Path) -> None:
    if member.issym() or member.islnk():
        raise RuntimeError(ARCHIVE_SYMLINK_ERROR)

    target_path = (dest_dir / member.name).resolve()
    if not str(target_path).startswith(str(dest_dir.resolve())):
        raise RuntimeError(f"{ARCHIVE_DESTINATION_ERROR_PREFIX}{member.name}")


def _extract_tarball(archive: Path, dest_dir: Path) -> None:
    with tarfile.open(archive, ARCHIVE_READ) as tar:
        for member in tar.getmembers():
            _validate_tar_member(member, dest_dir)
        tar.extractall(dest_dir)


def _download_archive(url: str, dest: Path, *, expected_digest: str, digest_algorithm: str) -> None:
    download_with_progress(
        url,
        dest,
        title="BizHawk download",
        text=f"Downloading BizHawk{ELLIPSIS}",
        expected_hash=expected_digest,
        hash_name=digest_algorithm,
        require_hash=True,
    )


def _download_runtime_package(pkg: RuntimePackage, dest: Path) -> None:
    url = f"{ARCH_MIRROR_BASE}/{ARCH_REPO}/os/x86_64/{pkg.filename}"
    download_with_progress(
        url,
        dest,
        title="BizHawk runtime download",
        text=f"Downloading {pkg.name}{ELLIPSIS}",
    )


def _extract_pkg_archive(archive: Path, runtime_root: Path) -> None:
    try:
        import zstandard
    except ImportError:
        zstandard = None

    runtime_root.mkdir(parents=True, exist_ok=True)

    if zstandard is None:
        tar_cmd = shutil.which("tar")
        if not tar_cmd:
            raise RuntimeError("zstandard not available and tar not found for .pkg.tar.zst extraction.")
        subprocess.run(
            [tar_cmd, "--zstd", "-xf", str(archive), "-C", str(runtime_root)],
            check=True,
        )
        return

    with archive.open("rb") as fh:
        dctx = zstandard.ZstdDecompressor()
        with dctx.stream_reader(fh) as reader:
            with tarfile.open(fileobj=reader, mode=ARCHIVE_READ) as tar:
                for member in tar.getmembers():
                    _validate_tar_member(member, runtime_root)
                tar.extractall(runtime_root)


def _runtime_required_paths(runtime_root: Path) -> dict[str, Path]:
    return {
        "mono": runtime_root / "usr" / "bin" / "mono",
        "lua": runtime_root / "usr" / "bin" / "lua",
        "mono_config": runtime_root / "etc" / "mono" / "config",
        "libgdiplus": runtime_root / "usr" / "lib" / "libgdiplus.so",
        "libgdiplus_alt": runtime_root / "usr" / "lib" / "libgdiplus.so.0",
        "libgdiplus64": runtime_root / "usr" / "lib64" / "libgdiplus.so",
        "libgdiplus64_alt": runtime_root / "usr" / "lib64" / "libgdiplus.so.0",
    }


def validate_runtime_root(runtime_root: Path) -> None:
    paths = _runtime_required_paths(runtime_root)
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
        raise RuntimeValidationError(
            "Runtime root is missing required dependencies: " + ", ".join(missing)
        )


def _find_emuhawk_script(root: Path) -> Optional[Path]:
    direct = root / BIZHAWK_INSTALL_MARKER
    if direct.is_file():
        return direct
    for candidate in root.glob("*/" + BIZHAWK_INSTALL_MARKER):
        if candidate.is_file():
            return candidate
    return None


def _stage_script(target: Path, source: Path, *, make_executable: bool) -> bool:
    try:
        data = source.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        target.write_bytes(data)
        if make_executable:
            target.chmod(target.stat().st_mode | 0o111)
        return True
    except Exception:
        return False


def _appimage_site_packages() -> Optional[Path]:
    appdir = os.environ.get("APPDIR")
    if not appdir:
        return None
    lib_root = Path(appdir) / "usr" / "lib"
    if not lib_root.is_dir():
        return None
    for candidate in sorted(lib_root.glob("python*")):
        site_packages = candidate / "site-packages"
        if site_packages.is_dir():
            return site_packages
    return None


def ensure_pyside_components() -> None:
    target_root = APP_COMPONENTS_DIR
    if all((target_root / name).exists() for name in PYSIDE_PACKAGES):
        return

    site_packages = _appimage_site_packages()
    if site_packages is None:
        return

    try:
        target_root.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        APP_LOGGER.log(
            f"Failed to create PySide staging directory {target_root}: {exc}",
            include_context=True,
            level="WARNING",
        )
        return

    for name in PYSIDE_PACKAGES:
        source = site_packages / name
        target = target_root / name
        if target.exists():
            continue
        try:
            if source.is_dir():
                shutil.copytree(source, target)
            elif source.is_file():
                shutil.copy2(source, target)
            else:
                APP_LOGGER.log(
                    f"PySide component not found in AppImage: {source}",
                    include_context=True,
                    level="WARNING",
                )
        except Exception as exc:
            APP_LOGGER.log(
                f"Failed to stage PySide component {name}: {exc}",
                include_context=True,
                level="WARNING",
            )


def ensure_app_components(settings: Dict[str, Any], *, force: bool = False) -> None:
    _ensure_dirs()
    settings_changed = False

    def _stage_resource(
        resource_name: str, target: Path, *, make_executable: bool, failure_template: str
    ) -> None:
        nonlocal settings_changed

        if not force and target.is_file():
            return
        try:
            resource = resources.files(__package__).joinpath(resource_name)
        except (ModuleNotFoundError, AttributeError):
            resource = None
        if resource is None:
            error_dialog(failure_template.format(runner=resource_name))
            return
        with resources.as_file(resource) as source:
            if not source.is_file():
                error_dialog(failure_template.format(runner=resource_name))
                return
            if _stage_script(target, source, make_executable=make_executable):
                settings_changed = True
            else:
                error_dialog(failure_template.format(runner=resource_name))

    runner_path = APP_COMPONENTS_DIR / RUNNER_FILENAME
    _stage_resource(
        RUNNER_FILENAME,
        runner_path,
        make_executable=True,
        failure_template=RUNNER_STAGE_FAILED_TEMPLATE,
    )

    entry_path = APP_COMPONENTS_DIR / BIZHAWK_ENTRY_LUA_FILENAME
    _stage_resource(
        BIZHAWK_ENTRY_LUA_FILENAME,
        entry_path,
        make_executable=False,
        failure_template="Failed to stage BizHawk entry Lua resource ({runner}).",
    )

    helper_path = APP_COMPONENTS_DIR / SAVE_HELPER_STAGED_FILENAME
    _stage_resource(
        SAVE_HELPER_FILENAME,
        helper_path,
        make_executable=True,
        failure_template="Failed to stage save migration helper ({runner}).",
    )

    if settings.get(BIZHAWK_RUNNER_KEY) != str(runner_path):
        settings[BIZHAWK_RUNNER_KEY] = str(runner_path)
        settings_changed = True

    if settings.get(BIZHAWK_ENTRY_LUA_PATH_KEY) != str(entry_path):
        settings[BIZHAWK_ENTRY_LUA_PATH_KEY] = str(entry_path)
        settings_changed = True

    if settings.get(SAVE_MIGRATION_HELPER_PATH_KEY) != str(helper_path):
        settings[SAVE_MIGRATION_HELPER_PATH_KEY] = str(helper_path)
        settings_changed = True

    if settings_changed:
        _save_settings(settings)

    ensure_pyside_components()


def build_runner(settings: Dict[str, Any], bizhawk_root: Path) -> Path:
    ensure_app_components(settings, force=True)
    return APP_COMPONENTS_DIR / RUNNER_FILENAME


def ensure_bizhawk_desktop_shortcut(
    settings: Dict[str, Any], runner: Path, *, enabled: bool
) -> None:
    if not runner.is_file() or not os.access(str(runner), os.X_OK):
        return

    desktop_dir = Path(os.path.expanduser("~/Desktop"))
    shortcut_path = desktop_dir / "BizHawk.sh"
    legacy_shortcuts = [
        desktop_dir / "BizHawk-Proton.sh",
        desktop_dir / "BizHawk-Proton.desktop",
    ]

    if not enabled:
        settings[BIZHAWK_DESKTOP_SHORTCUT_KEY] = NO_VALUE
        _save_settings(settings)
        return

    content = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"exec \"{runner}\" \"$@\"\n"
    )
    try:
        shortcut_path.parent.mkdir(parents=True, exist_ok=True)
        shortcut_path.write_text(content, encoding=ENCODING_UTF8)
        shortcut_path.chmod(0o755)
        for legacy in legacy_shortcuts:
            if legacy.exists():
                legacy.unlink()
        settings[BIZHAWK_DESKTOP_SHORTCUT_KEY] = YES_VALUE
        _save_settings(settings)
    except Exception as exc:  # pragma: no cover - filesystem edge cases
        settings[BIZHAWK_DESKTOP_SHORTCUT_KEY] = NO_VALUE
        _save_settings(settings)
        error_dialog(f"Failed to create BizHawk Desktop shortcut: {exc}")


def auto_detect_bizhawk_exe(settings: Dict[str, Any]) -> Optional[Path]:
    exe_str = str(settings.get(BIZHAWK_EXE_KEY, EMPTY_STRING) or EMPTY_STRING)
    if exe_str:
        candidate = Path(exe_str)
        if candidate.is_file():
            return candidate

    install_dir = get_path_setting(settings, BIZHAWK_INSTALL_DIR_KEY)
    if install_dir:
        candidate = _find_emuhawk_script(install_dir)
        if candidate:
            return candidate

    return None


def select_bizhawk_exe(initial: Optional[Path] = None) -> Optional[Path]:
    selection = _select_file_dialog(
        title=SELECT_EMUHAWK_TITLE,
        initial=initial,
        file_filter="EmuHawkMono.sh",
        dialog_key=DIALOG_KEY_BIZHAWK_EXE,
    )
    if not selection:
        return None
    if not selection.is_file():
        error_dialog("Selected EmuHawkMono.sh does not exist.")
        return None
    return selection


def manual_select_bizhawk(settings: Optional[Dict[str, Any]] = None) -> bool:
    settings = settings if settings is not None else _load_settings()
    exe = select_bizhawk_exe(Path.home())
    if not exe:
        return False
    settings[BIZHAWK_EXE_KEY] = str(exe)
    settings[BIZHAWK_INSTALL_DIR_KEY] = str(exe.parent)
    settings[BIZHAWK_VERSION_KEY] = EMPTY_STRING
    settings[BIZHAWK_SKIP_VERSION_KEY] = EMPTY_STRING
    _save_settings(settings)
    return True


def force_update_bizhawk(settings: Optional[Dict[str, Any]] = None) -> bool:
    settings = settings if settings is not None else _load_settings()
    exe = auto_detect_bizhawk_exe(settings)
    if exe is None:
        return False
    try:
        url, ver, digest, algo = _github_latest_bizhawk()
        new_exe = download_and_extract_bizhawk(url, ver, expected_digest=digest, digest_algorithm=algo)
    except Exception as exc:
        error_dialog(str(exc))
        return False
    settings[BIZHAWK_EXE_KEY] = str(new_exe)
    settings[BIZHAWK_INSTALL_DIR_KEY] = str(new_exe.parent)
    settings[BIZHAWK_VERSION_KEY] = ver
    settings[BIZHAWK_SKIP_VERSION_KEY] = EMPTY_STRING
    settings[BIZHAWK_LATEST_SEEN_KEY] = ver
    _save_settings(settings)
    return True


def _snapshot_bizhawk_install(src_dir: Path) -> Optional[Path]:
    if not src_dir.is_dir():
        return None
    snapshot_dir = Path(tempfile.mkdtemp(prefix="bizhawk-snapshot-"))
    shutil.copytree(src_dir, snapshot_dir / src_dir.name)
    return snapshot_dir / src_dir.name


def _restore_bizhawk_install(snapshot_dir: Optional[Path], dest_dir: Path) -> None:
    if snapshot_dir is None or not snapshot_dir.exists():
        return
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    shutil.copytree(snapshot_dir, dest_dir)


def download_and_extract_bizhawk(
    url: str, version: str, *, expected_digest: str, digest_algorithm: str
) -> Path:
    _ensure_dirs()
    settings = _load_settings()
    install_dir = get_path_setting(settings, BIZHAWK_INSTALL_DIR_KEY)
    install_dir.mkdir(parents=True, exist_ok=True)

    archive_path = install_dir / f"BizHawk-{version}{ARCHIVE_TAR_GZ_SUFFIX}"
    _download_archive(url, archive_path, expected_digest=expected_digest, digest_algorithm=digest_algorithm)

    temp_extract = Path(tempfile.mkdtemp(prefix="bizhawk-extract-"))
    _extract_tarball(archive_path, temp_extract)

    candidates = [path for path in temp_extract.iterdir() if path.is_dir()]
    if len(candidates) == 1:
        extracted_root = candidates[0]
    else:
        extracted_root = temp_extract

    target_root = install_dir / extracted_root.name
    if target_root.exists():
        shutil.rmtree(target_root)
    shutil.move(str(extracted_root), str(target_root))

    exe = _find_emuhawk_script(target_root)
    if exe is None:
        raise RuntimeError("BizHawk archive did not contain EmuHawkMono.sh")

    return exe


def maybe_update_bizhawk(
    settings: Dict[str, Any],
    bizhawk_exe: Path,
    *,
    download_messages: Optional[list[str]] = None,
) -> Tuple[Path, bool]:
    try:
        _ = bizhawk_exe.relative_to(get_path_setting(settings, BIZHAWK_INSTALL_DIR_KEY))
    except ValueError:
        return bizhawk_exe, False

    try:
        url, latest_ver, latest_digest, latest_algo = _github_latest_bizhawk()
    except Exception:
        return bizhawk_exe, False

    current_ver = str(settings.get(BIZHAWK_VERSION_KEY, EMPTY_STRING) or EMPTY_STRING)
    skip_ver = str(settings.get(BIZHAWK_SKIP_VERSION_KEY, EMPTY_STRING) or EMPTY_STRING)
    latest_seen = str(settings.get(BIZHAWK_LATEST_SEEN_KEY, EMPTY_STRING) or EMPTY_STRING)
    should_prompt = _is_newer_version(latest_ver, latest_seen)
    if latest_ver and latest_ver != latest_seen:
        settings[BIZHAWK_LATEST_SEEN_KEY] = latest_ver
        _save_settings(settings)
    if not current_ver or current_ver == latest_ver or skip_ver == latest_ver:
        return bizhawk_exe, False
    if not should_prompt:
        return bizhawk_exe, False

    choice = _qt_question_dialog(
        title="BizHawk update",
        text="A BizHawk update is available. Update now?",
        ok_label="Update now",
        cancel_label="Later",
        extra_label="Skip this version",
    )
    if choice == "cancel":
        return bizhawk_exe, False

    if choice == "extra":
        settings[BIZHAWK_SKIP_VERSION_KEY] = latest_ver
        settings[BIZHAWK_LATEST_SEEN_KEY] = latest_ver
        _save_settings(settings)
        return bizhawk_exe, False

    snapshot_dir = _snapshot_bizhawk_install(bizhawk_exe.parent)
    try:
        new_exe = download_and_extract_bizhawk(
            url, latest_ver, expected_digest=latest_digest, digest_algorithm=latest_algo
        )
    except Exception as e:
        _restore_bizhawk_install(snapshot_dir, bizhawk_exe.parent)
        error_dialog(f"{BIZHAWK_UPDATE_FAILED_PREFIX}{e}")
        return bizhawk_exe, False
    _restore_bizhawk_install(snapshot_dir, new_exe.parent)

    settings[BIZHAWK_EXE_KEY] = str(new_exe)
    settings[BIZHAWK_INSTALL_DIR_KEY] = str(new_exe.parent)
    settings[BIZHAWK_VERSION_KEY] = latest_ver
    settings[BIZHAWK_SKIP_VERSION_KEY] = EMPTY_STRING
    settings[BIZHAWK_LATEST_SEEN_KEY] = latest_ver
    _save_settings(settings)

    runner_str = str(settings.get(BIZHAWK_RUNNER_KEY, EMPTY_STRING) or EMPTY_STRING)
    if runner_str:
        build_runner(settings, new_exe.parent)

    if download_messages is not None:
        download_messages.append(f"Updated BizHawk to {latest_ver}")
    else:
        info_dialog(f"{BIZHAWK_UPDATED_PREFIX}{latest_ver}.")
    return new_exe, True


def ensure_runtime_root(
    settings: Dict[str, Any],
    *,
    download_enabled: bool,
    download_messages: Optional[list[str]] = None,
    prompt_on_missing: bool = True,
) -> Optional[Path]:
    runtime_root = get_path_setting(settings, BIZHAWK_RUNTIME_ROOT_KEY)
    try:
        validate_runtime_root(runtime_root)
        return runtime_root
    except RuntimeValidationError:
        pass

    if not download_enabled and prompt_on_missing:
        choice = _qt_question_dialog(
            title="BizHawk runtime",
            text=(
                "BizHawk runtime downloads are disabled.\n\n"
                "Select an existing runtime_root folder containing mono, libgdiplus, and lua?"
            ),
            ok_label="Select runtime_root",
            cancel_label="Cancel",
        )
        if choice != "ok":
            return None

        selected = _select_file_dialog(
            title="Select runtime_root folder",
            initial=Path.home(),
            dialog_key=DIALOG_KEY_RUNTIME_ROOT,
            select_directories=True,
        )
        if not selected:
            return None

        try:
            validate_runtime_root(selected)
        except RuntimeValidationError as exc:
            error_dialog(str(exc))
            return None

        settings[BIZHAWK_RUNTIME_ROOT_KEY] = str(selected)
        settings[BIZHAWK_RUNTIME_DOWNLOAD_KEY] = False
        _save_settings(settings)
        return selected

    runtime_root.mkdir(parents=True, exist_ok=True)
    for pkg in RUNTIME_PACKAGES:
        archive_path = runtime_root / pkg.filename
        if not archive_path.exists():
            _download_runtime_package(pkg, archive_path)
        _extract_pkg_archive(archive_path, runtime_root)
        if download_messages is not None:
            download_messages.append(f"Staged runtime package: {pkg.name}")

    try:
        validate_runtime_root(runtime_root)
    except RuntimeValidationError as exc:
        error_dialog(str(exc))
        return None

    settings[BIZHAWK_RUNTIME_ROOT_KEY] = str(runtime_root)
    settings[BIZHAWK_RUNTIME_DOWNLOAD_KEY] = True
    _save_settings(settings)
    return runtime_root


def ensure_bizhawk_install(
    *,
    download_selected: bool = True,
    create_shortcut: bool = False,
    download_messages: Optional[list[str]] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Optional[Tuple[Path, Path, bool]]:
    _ensure_dirs()
    provided_settings = settings
    settings = settings if settings is not None else _load_settings()

    def _merge_and_save_settings() -> None:
        nonlocal settings
        if provided_settings is not None and settings is not provided_settings:
            merged = {**provided_settings, **settings}
            provided_settings.clear()
            provided_settings.update(merged)
            settings = provided_settings
        _save_settings(settings)

    downloaded = False

    exe = auto_detect_bizhawk_exe(settings)
    runner = Path(str(settings.get(BIZHAWK_RUNNER_KEY, "") or ""))

    if exe and exe.is_file() and runner.is_file():
        exe, updated = maybe_update_bizhawk(settings, exe, download_messages=download_messages)
        downloaded = downloaded or updated
        runner = Path(str(settings.get(BIZHAWK_RUNNER_KEY, "") or ""))
        if runner.is_file():
            if updated:
                ensure_bizhawk_desktop_shortcut(settings, runner, enabled=create_shortcut)
            return runner, exe, downloaded

    if not exe or not exe.is_file():
        if download_selected:
            try:
                url, ver, digest, digest_algo = _github_latest_bizhawk()
            except Exception as e:
                error_dialog(f"Failed to query latest BizHawk release: {e}")
                return None
            settings[BIZHAWK_LATEST_SEEN_KEY] = ver
            try:
                exe = download_and_extract_bizhawk(
                    url, ver, expected_digest=digest, digest_algorithm=digest_algo
                )
            except Exception as e:
                error_dialog(f"BizHawk download failed or was cancelled: {e}")
                return None
            settings[BIZHAWK_EXE_KEY] = str(exe)
            settings[BIZHAWK_INSTALL_DIR_KEY] = str(exe.parent)
            settings[BIZHAWK_VERSION_KEY] = ver
            settings[BIZHAWK_SKIP_VERSION_KEY] = EMPTY_STRING
            settings[BIZHAWK_LATEST_SEEN_KEY] = ver
            _merge_and_save_settings()
            downloaded = True
            if download_messages is not None:
                download_messages.append(f"Downloaded BizHawk {ver}")
        else:
            choice = _qt_question_dialog(
                title="BizHawk setup",
                text=(
                    "BizHawk was not selected for download.\n\n"
                    "Select an existing EmuHawkMono.sh to continue?"
                ),
                ok_label=SELECT_EMUHAWK_TITLE,
                cancel_label="Cancel",
            )
            if choice != "ok":
                return None

            exe = select_bizhawk_exe(Path.home())
            if not exe:
                return None
            settings[BIZHAWK_EXE_KEY] = str(exe)
            settings[BIZHAWK_INSTALL_DIR_KEY] = str(exe.parent)
            settings[BIZHAWK_VERSION_KEY] = EMPTY_STRING
            settings[BIZHAWK_SKIP_VERSION_KEY] = EMPTY_STRING
            _merge_and_save_settings()

    runner = build_runner(settings, exe.parent)

    exe, updated = maybe_update_bizhawk(settings, exe, download_messages=download_messages)
    downloaded = downloaded or updated

    if downloaded:
        ensure_bizhawk_desktop_shortcut(settings, runner, enabled=create_shortcut)

    return runner, exe, downloaded


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] != "ensure":
        print("Usage: ap_bizhelper_bizhawk.py ensure", file=sys.stderr)
        return 1

    result = ensure_bizhawk_install()
    if result is None:
        return 1

    runner, _, _ = result
    print(str(runner))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
