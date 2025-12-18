#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import importlib.resources as resources
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .ap_bizhelper_ap import download_with_progress
from .dialogs import (
    question_dialog as _qt_question_dialog,
    select_file_dialog as _select_file_dialog,
    error_dialog,
    info_dialog,
)
from .ap_bizhelper_config import (
    load_settings as _load_shared_settings,
    save_settings as _save_shared_settings,
)

CONFIG_DIR = Path(os.path.expanduser("~/.config/ap_bizhelper"))
SETTINGS_FILE = CONFIG_DIR / "settings.json"
DATA_DIR = Path(os.path.expanduser("~/.local/share/ap_bizhelper"))

BIZHAWK_WIN_DIR = DATA_DIR / "bizhawk_win"
PROTON_PREFIX = DATA_DIR / "proton_prefix"

GITHUB_API_LATEST = "https://api.github.com/repos/TASEmulators/BizHawk/releases/latest"
ARCHIPELAGO_RELEASE_API = "https://api.github.com/repos/ArchipelagoMW/Archipelago/releases"
SNI_DOWNLOAD_URL = (
    "https://github.com/alttpo/sni/releases/download/v0.0.102a/"
    "sni-v0.0.102a-windows-amd64.zip"
)
SNI_VERSION = "v0.0.102a"


def _ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BIZHAWK_WIN_DIR.mkdir(parents=True, exist_ok=True)
    PROTON_PREFIX.mkdir(parents=True, exist_ok=True)


def _load_settings() -> Dict[str, Any]:
    return _load_shared_settings()


def _save_settings(settings: Dict[str, Any]) -> None:
    _ensure_dirs()
    _save_shared_settings(settings)


def _github_latest_bizhawk() -> Tuple[str, str]:
    """
    Return (download_url, version_tag) for the latest BizHawk Windows x64 zip.
    """
    import urllib.request

    req = urllib.request.Request(GITHUB_API_LATEST, headers={"User-Agent": "ap-bizhelper/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read().decode("utf-8")
    j = json.loads(data)

    tag = j.get("tag_name") or ""
    assets = j.get("assets") or []

    # Prefer assets whose name clearly ends with 'win-x64.zip'
    for asset in assets:
        name = asset.get("name") or ""
        if name.endswith("win-x64.zip"):
            url = asset.get("browser_download_url")
            if url:
                return url, tag

    # Fallback: look for anything containing 'win-x64' and ending in .zip
    pattern = re.compile(r"win-x64.*\.zip$")
    for asset in assets:
        name = asset.get("name") or ""
        if pattern.search(name):
            url = asset.get("browser_download_url")
            if url:
                return url, tag

    raise RuntimeError("Could not find BizHawk win-x64 zip asset in latest release.")


def _archipelago_release(tag: Optional[str] = None) -> Tuple[str, str]:
    """Return (download_url, version_tag) for an Archipelago source archive."""

    import urllib.request

    url = f"{ARCHIPELAGO_RELEASE_API}/latest" if not tag else f"{ARCHIPELAGO_RELEASE_API}/tags/{tag}"
    req = urllib.request.Request(url, headers={"User-Agent": "ap-bizhelper/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read().decode("utf-8")
    j = json.loads(data)

    tar_url = j.get("tarball_url") or j.get("zipball_url")
    if not tar_url:
        raise RuntimeError("Could not locate Archipelago source archive download URL.")

    tag_name = j.get("tag_name") or (tag or "")
    return tar_url, tag_name


def _preserve_bizhawk_config() -> Optional[Path]:
    try:
        preserved_config = next(BIZHAWK_WIN_DIR.rglob("config.ini"))
        if not preserved_config.is_file():
            return None
    except StopIteration:
        return None

    with tempfile.NamedTemporaryFile(delete=False, suffix=".ini") as cfg_tmp:
        shutil.copy2(preserved_config, cfg_tmp.name)
        return Path(cfg_tmp.name)


def _extract_bizhawk_archive(archive: Path, version: str, preserved_config: Optional[Path]) -> Path:
    """Extract a BizHawk archive into the managed directory."""

    _ensure_dirs()

    # Clear existing directory
    for child in BIZHAWK_WIN_DIR.iterdir():
        try:
            if child.is_dir():
                for root, dirs, files in os.walk(child, topdown=False):
                    for f in files:
                        try:
                            os.remove(os.path.join(root, f))
                        except Exception:
                            pass
                    for d in dirs:
                        try:
                            os.rmdir(os.path.join(root, d))
                        except Exception:
                            pass
                child.rmdir()
            else:
                child.unlink()
        except Exception:
            pass

    with zipfile.ZipFile(archive, "r") as zf:
        _safe_extract_zip(zf, BIZHAWK_WIN_DIR)

    exe = auto_detect_bizhawk_exe({})
    if exe is None:
        raise RuntimeError("Could not find EmuHawk.exe after extracting BizHawk.")
    _stage_bizhawk_config(exe, preserved_config)
    return exe


def download_and_extract_bizhawk(url: str, version: str) -> Path:
    """
    Download the BizHawk Windows zip and extract it into BIZHAWK_WIN_DIR.

    Returns the detected EmuHawk.exe path.
    """
    import zipfile
    import tempfile

    _ensure_dirs()

    preserved_config = _preserve_bizhawk_config()

    # Download zip with shared progress helper
    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmpf:
        tmp_path = Path(tmpf.name)
    try:
        download_with_progress(
            url,
            tmp_path,
            title="BizHawk download",
            text=f"Downloading BizHawk {version}...",
        )

        return _extract_bizhawk_archive(tmp_path, version, preserved_config)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass


def _copy_tree(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)


def _validated_member_path(member_name: str, dest_root: Path) -> Path:
    candidate = Path(member_name)
    if candidate.is_absolute():
        raise RuntimeError(f"Archive entry uses absolute path: {member_name}")
    if any(part in ("..", "") for part in candidate.parts):
        raise RuntimeError(f"Archive entry escapes destination: {member_name}")

    resolved = (dest_root / candidate).resolve()
    if not resolved.is_relative_to(dest_root):
        raise RuntimeError(f"Archive entry escapes destination: {member_name}")

    return resolved


def _safe_extract_tar(tf: tarfile.TarFile, dest_dir: Path) -> None:
    dest_root = dest_dir.resolve()
    for member in tf.getmembers():
        if member.islnk() or member.issym():
            raise RuntimeError("Archives containing symbolic links are not supported")

        target_path = _validated_member_path(member.name, dest_root)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        if member.isdir():
            target_path.mkdir(parents=True, exist_ok=True)
            continue

        fileobj = tf.extractfile(member)
        if fileobj is None:
            raise RuntimeError(f"Could not read archive member: {member.name}")
        with fileobj, target_path.open("wb") as out:
            shutil.copyfileobj(fileobj, out)


def _zipinfo_is_symlink(info: zipfile.ZipInfo) -> bool:
    return (info.external_attr >> 16) & 0o170000 == 0o120000


def _safe_extract_zip(zf: zipfile.ZipFile, dest_dir: Path) -> None:
    dest_root = dest_dir.resolve()
    for info in zf.infolist():
        if _zipinfo_is_symlink(info):
            raise RuntimeError("Archives containing symbolic links are not supported")

        target_path = _validated_member_path(info.filename, dest_root)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        if info.is_dir():
            target_path.mkdir(parents=True, exist_ok=True)
            continue

        with zf.open(info) as src, target_path.open("wb") as out:
            shutil.copyfileobj(src, out)


def _extract_archive(archive: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive, "r:*") as tf:
            _safe_extract_tar(tf, dest_dir)
    elif zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive, "r") as zf:
            _safe_extract_zip(zf, dest_dir)
    else:
        raise RuntimeError("Unsupported archive format for connectors.")
    return dest_dir


def _apply_archipelago_connector_archive(archive: Path, bizhawk_dir: Path) -> None:
    with tempfile.TemporaryDirectory() as td:
        extracted_root = _extract_archive(archive, Path(td))
        lua_dir: Optional[Path] = None
        for data_dir in extracted_root.rglob("data"):
            candidate = data_dir / "lua"
            if candidate.is_dir():
                lua_dir = candidate
                break
        if lua_dir is None:
            raise RuntimeError("Archipelago source archive did not contain data/lua directory")
        _copy_tree(lua_dir, bizhawk_dir / "connectors")


def _stage_archipelago_connectors(
    bizhawk_dir: Path, *, ap_version: Optional[str], download_messages: Optional[list[str]]
) -> str:
    """Download Archipelago source and copy data/lua into bizhawk_dir/connectors."""

    url, tag = _archipelago_release(ap_version or None)
    suffix = ".tar.gz" if "tar" in url else ".zip"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmpf:
        tmp_path = Path(tmpf.name)

    try:
        download_with_progress(
            url,
            tmp_path,
            title="Archipelago connectors",
            text=f"Downloading Archipelago connectors ({tag})...",
        )

        _apply_archipelago_connector_archive(tmp_path, bizhawk_dir)
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass

    if download_messages is not None:
        download_messages.append(f"Updated BizHawk connectors to Archipelago {tag}")
    return tag


def _stage_sni_connectors(bizhawk_dir: Path, download_messages: Optional[list[str]]) -> None:
    """Download SNI release and copy lua folder into bizhawk_dir/sni."""

    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmpf:
        tmp_path = Path(tmpf.name)

    try:
        download_with_progress(
            SNI_DOWNLOAD_URL,
            tmp_path,
            title="SNI connectors",
            text=f"Downloading SNI connectors ({SNI_VERSION})...",
        )
        with tempfile.TemporaryDirectory() as td:
            extracted_root = _extract_archive(tmp_path, Path(td))
            lua_dir = next((p for p in extracted_root.rglob("lua") if p.is_dir()), None)
            if lua_dir is None:
                raise RuntimeError("SNI release did not contain a lua directory")
            _copy_tree(lua_dir, bizhawk_dir / "sni")
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass

    if download_messages is not None:
        download_messages.append("Updated BizHawk SNI connectors")


def _has_archipelago_connector(connectors_dir: Path) -> bool:
    connector_path = connectors_dir / "connector_bizhawk_generic.lua"
    return connector_path.is_file()


def _has_sni_connector(sni_dir: Path) -> bool:
    if not sni_dir.is_dir():
        return False
    return any(p.is_file() for p in sni_dir.glob("*.lua"))


def connectors_need_download(
    settings: Dict[str, Any], bizhawk_exe: Optional[Path], *, ap_version: Optional[str]
) -> bool:
    if bizhawk_exe is None or not bizhawk_exe.is_file():
        return False

    desired_ap_version = ap_version or ""
    current_ap_version = str(settings.get("BIZHAWK_AP_CONNECTOR_VERSION", "") or "")
    connectors_dir = bizhawk_exe.parent / "connectors"
    if desired_ap_version != current_ap_version or not _has_archipelago_connector(connectors_dir):
        return True

    current_sni_version = str(settings.get("BIZHAWK_SNI_VERSION", "") or "")
    sni_dir = bizhawk_exe.parent / "sni"
    if current_sni_version != SNI_VERSION or not _has_sni_connector(sni_dir):
        return True

    return False


def ensure_connectors(
    settings: Dict[str, Any],
    bizhawk_exe: Path,
    *,
    ap_version: Optional[str],
    download_messages: Optional[list[str]],
) -> bool:
    """Ensure connector directories are present for BizHawk installs."""

    bizhawk_dir = bizhawk_exe.parent
    updated = False

    desired_ap_version = ap_version or ""
    current_ap_version = str(settings.get("BIZHAWK_AP_CONNECTOR_VERSION", "") or "")
    connectors_dir = bizhawk_dir / "connectors"
    if desired_ap_version != current_ap_version or not _has_archipelago_connector(connectors_dir):
        tag = _stage_archipelago_connectors(
            bizhawk_dir, ap_version=ap_version, download_messages=download_messages
        )
        settings["BIZHAWK_AP_CONNECTOR_VERSION"] = tag
        updated = True

    current_sni_version = str(settings.get("BIZHAWK_SNI_VERSION", "") or "")
    sni_dir = bizhawk_dir / "sni"
    if current_sni_version != SNI_VERSION or not _has_sni_connector(sni_dir):
        _stage_sni_connectors(bizhawk_dir, download_messages)
        settings["BIZHAWK_SNI_VERSION"] = SNI_VERSION
        updated = True

    if updated:
        _save_settings(settings)
    return updated


def _stage_bizhawk_config(exe: Path, preserved_config: Optional[Path]) -> None:
    """Copy a default BizHawk config alongside ``exe`` if one is absent."""

    target_cfg = exe.parent / "config.ini"
    if target_cfg.exists():
        if preserved_config is not None and preserved_config.exists():
            preserved_config.unlink()
        return

    try:
        if preserved_config is not None and preserved_config.is_file():
            shutil.copy2(preserved_config, target_cfg)
            return

        try:
            cfg_resource = resources.files(__package__).joinpath("config.ini")
        except (ModuleNotFoundError, AttributeError):
            cfg_resource = None

        if cfg_resource is not None:
            with resources.as_file(cfg_resource) as candidate:
                if candidate.is_file():
                    shutil.copy2(candidate, target_cfg)
                    return

        candidate = Path(__file__).with_name("config.ini")
        if candidate.is_file():
            shutil.copy2(candidate, target_cfg)
    finally:
        if preserved_config is not None and preserved_config.exists():
            preserved_config.unlink()


def auto_detect_bizhawk_exe(settings: Dict[str, Any]) -> Optional[Path]:
    """
    Try to determine the EmuHawk.exe path from settings or by scanning BIZHAWK_WIN_DIR.
    """
    exe_str = str(settings.get("BIZHAWK_EXE", "") or "")
    if exe_str:
        exe = Path(exe_str)
        if exe.is_file():
            return exe

    if not BIZHAWK_WIN_DIR.is_dir():
        return None

    candidates = list(BIZHAWK_WIN_DIR.rglob("EmuHawk.exe"))
    if not candidates:
        return None

    exe = sorted(candidates)[0]
    settings["BIZHAWK_EXE"] = str(exe)
    _save_settings(settings)
    return exe


def auto_detect_proton(settings: Dict[str, Any]) -> Optional[Path]:
    """
    Attempt to locate a Proton binary under ~/.steam/steam/steamapps/common.
    """
    steam_root = Path(os.path.expanduser("~/.steam/steam"))
    common = steam_root / "steamapps" / "common"
    if not common.exists():
        return None

    candidates = []
    for path in common.rglob("proton"):
        if path.is_file():
            candidates.append(path)

    if not candidates:
        return None

    # Prefer Experimental if present
    experimental = [p for p in candidates if "Experimental/proton" in str(p)]
    if experimental:
        chosen = sorted(experimental)[-1]
    else:
        chosen = sorted(candidates)[-1]

    settings["PROTON_BIN"] = str(chosen)
    _save_settings(settings)
    return chosen


def select_proton_bin(initial: Optional[Path] = None) -> Optional[Path]:
    p = _select_file_dialog(
        title="Select Proton binary", initial=initial, dialog_key="proton_bin"
    )
    if p is None:
        return None
    if not p.is_file():
        error_dialog("Selected Proton binary does not exist.")
        return None
    return p


def select_bizhawk_exe(initial: Optional[Path] = None) -> Optional[Path]:
    p = _select_file_dialog(
        title="Select EmuHawk.exe",
        initial=initial,
        file_filter="*.exe",
        dialog_key="bizhawk_exe",
    )
    if p is None:
        return None
    if not p.is_file():
        error_dialog("Selected EmuHawk.exe does not exist.")
        return None
    return p


def _stage_runner(target: Path, source: Path) -> bool:
    """Copy the runner helper to ``target`` and mark it executable."""

    try:
        # Normalize newlines to avoid ``/usr/bin/env: 'python3\r': No such file``
        # errors when the runner file was produced with Windows line endings.
        data = source.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        target.write_bytes(data)
        target.chmod(target.stat().st_mode | 0o111)
        return True
    except Exception:
        return False


def build_runner(settings: Dict[str, Any], bizhawk_exe: Path, proton_bin: Path) -> Path:
    """
    Ensure the Python BizHawk runner helper is staged alongside BizHawk.

    The runner is copied into the BizHawk installation directory so that any
    launch shortcuts can invoke it directly with the same arguments
    Archipelago provides.
    """

    _ensure_dirs()
    try:
        runner_resource = resources.files(__package__).joinpath("run_bizhawk_proton.py")
    except (ModuleNotFoundError, AttributeError):
        runner_resource = None

    bizhawk_runner = bizhawk_exe.parent / "run_bizhawk_proton.py"

    if runner_resource is None:
        error_dialog("BizHawk runner helper (run_bizhawk_proton.py) is missing.")
        return bizhawk_runner

    staged_any = False
    with resources.as_file(runner_resource) as source_runner:
        if not source_runner.is_file():
            error_dialog("BizHawk runner helper (run_bizhawk_proton.py) is missing.")
            return bizhawk_runner

        staged_any = _stage_runner(bizhawk_runner, source_runner) or staged_any

    if not staged_any:
        error_dialog("Failed to stage BizHawk runner helper (run_bizhawk_proton.py).")

    runner = bizhawk_runner

    # Persist the runner path for other helpers to consume.
    settings["BIZHAWK_RUNNER"] = str(runner)
    _save_settings(settings)
    return runner



def ensure_bizhawk_desktop_shortcut(
    settings: Dict[str, Any], runner: Path, *, enabled: bool
) -> None:
    """Place a BizHawk (Proton) launcher on the Desktop when enabled."""
    if not runner.is_file() or not os.access(str(runner), os.X_OK):
        return

    desktop_dir = Path(os.path.expanduser("~/Desktop"))
    shortcut_path = desktop_dir / "BizHawk-Proton.sh"
    legacy_desktop_entry = desktop_dir / "BizHawk-Proton.desktop"

    if not enabled:
        settings["BIZHAWK_DESKTOP_SHORTCUT"] = "no"
        _save_settings(settings)
        return

    content = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"exec \"{runner}\" \"$@\"\n"
    )
    try:
        shortcut_path.parent.mkdir(parents=True, exist_ok=True)
        with shortcut_path.open("w", encoding="utf-8") as f:
            f.write(content)
        shortcut_path.chmod(0o755)
        # Clean up the legacy .desktop file if present to avoid confusion.
        if legacy_desktop_entry.exists():
            legacy_desktop_entry.unlink()
        settings["BIZHAWK_DESKTOP_SHORTCUT"] = "yes"
        _save_settings(settings)
    except Exception as exc:  # pragma: no cover - filesystem edge cases
        settings["BIZHAWK_DESKTOP_SHORTCUT"] = "no"
        _save_settings(settings)
        error_dialog(f"Failed to create BizHawk Desktop shortcut: {exc}")


def maybe_update_bizhawk(
    settings: Dict[str, Any],
    bizhawk_exe: Path,
    *,
    download_messages: Optional[list[str]] = None,
) -> Tuple[Path, bool]:
    """
    If BizHawk is installed in our managed directory, check for an update and
    optionally download it. Returns the (possibly updated) executable path and
    whether an update was installed.
    """
    try:
        _ = bizhawk_exe.relative_to(BIZHAWK_WIN_DIR)
    except ValueError:
        # User-managed install; don't auto-update.
        return bizhawk_exe, False

    try:
        url, latest_ver = _github_latest_bizhawk()
    except Exception:
        return bizhawk_exe, False

    current_ver = str(settings.get("BIZHAWK_VERSION", "") or "")
    skip_ver = str(settings.get("BIZHAWK_SKIP_VERSION", "") or "")
    if not current_ver or current_ver == latest_ver or skip_ver == latest_ver:
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
        settings["BIZHAWK_SKIP_VERSION"] = latest_ver
        _save_settings(settings)
        return bizhawk_exe, False

    # Update now
    try:
        new_exe = download_and_extract_bizhawk(url, latest_ver)
    except Exception as e:
        error_dialog(f"BizHawk update failed: {e}")
        return bizhawk_exe, False

    settings["BIZHAWK_EXE"] = str(new_exe)
    settings["BIZHAWK_VERSION"] = latest_ver
    settings["BIZHAWK_SKIP_VERSION"] = ""
    _save_settings(settings)

    # Rebuild runner with updated path
    proton_bin_str = str(settings.get("PROTON_BIN", "") or "")
    if proton_bin_str:
        proton_bin = Path(proton_bin_str)
        if proton_bin.is_file():
            build_runner(settings, new_exe, proton_bin)

    if download_messages is not None:
        download_messages.append(f"Updated BizHawk to {latest_ver}")
    else:
        info_dialog(f"BizHawk updated to {latest_ver}.")
    return new_exe, True


def ensure_bizhawk_and_proton(
    *,
    download_selected: bool = True,
    create_shortcut: bool = False,
    download_messages: Optional[list[str]] = None,
    settings: Optional[Dict[str, Any]] = None,
    stage_connectors: bool = True,
) -> Optional[Tuple[Path, Path, bool]]:
    """
    Ensure BizHawk (Windows) and Proton are configured and runnable.

    On success, returns the Path to the BizHawk runner script, the EmuHawk.exe
    path, and a flag indicating whether any downloads occurred.

    When ``stage_connectors`` is False, connector downloads are skipped even if
    they appear missing or outdated.

    On failure or user cancellation, returns None.
    """
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

    # Existing config?
    exe_str = str(settings.get("BIZHAWK_EXE", "") or "")
    runner_str = str(settings.get("BIZHAWK_RUNNER", "") or "")
    proton_str = str(settings.get("PROTON_BIN", "") or "")

    exe = Path(exe_str) if exe_str else None
    runner = Path(runner_str) if runner_str else None
    proton_bin = Path(proton_str) if proton_str else None

    if exe and exe.is_file() and proton_bin and proton_bin.is_file() and runner and runner.is_file():
        exe, updated = maybe_update_bizhawk(
            settings, exe, download_messages=download_messages
        )
        downloaded = downloaded or updated
        runner_str = str(settings.get("BIZHAWK_RUNNER", "") or "")
        exe_str = str(settings.get("BIZHAWK_EXE", "") or "")
        if runner_str and exe_str:
            runner = Path(runner_str)
            exe = Path(exe_str)
            if runner.is_file() and exe.is_file():
                if updated:
                    ensure_bizhawk_desktop_shortcut(
                        settings, runner, enabled=create_shortcut
                    )
                return runner, exe, downloaded

    # Need to (re)configure BizHawk
    exe = auto_detect_bizhawk_exe(settings)
    if not exe or not exe.is_file():
        cached_exe: Optional[Path] = None
        if download_selected and (exe is None or not exe.is_file()):
            try:
                url, ver = _github_latest_bizhawk()
            except Exception as e:
                error_dialog(f"Failed to query latest BizHawk release: {e}")
                return None
            try:
                exe = download_and_extract_bizhawk(url, ver)
            except Exception as e:
                error_dialog(f"BizHawk download failed or was cancelled: {e}")
                return None
            settings["BIZHAWK_EXE"] = str(exe)
            settings["BIZHAWK_VERSION"] = ver
            settings["BIZHAWK_SKIP_VERSION"] = ""
            _merge_and_save_settings()
            downloaded = True
            if download_messages is not None:
                download_messages.append(f"Downloaded BizHawk {ver}")
        elif not download_selected:
            choice = _qt_question_dialog(
                title="BizHawk (Proton) setup",
                text=(
                    "BizHawk (with Proton) was not selected for download.\n\n"
                    "Select an existing EmuHawk.exe to continue?"
                ),
                ok_label="Select EmuHawk.exe",
                cancel_label="Cancel",
            )
            if choice != "ok":
                return None

            exe = select_bizhawk_exe(Path(os.path.expanduser("~")))
            if not exe:
                return None
            settings["BIZHAWK_EXE"] = str(exe)
            settings["BIZHAWK_VERSION"] = ""
            settings["BIZHAWK_SKIP_VERSION"] = ""
            _merge_and_save_settings()

    # Ensure Proton
    proton_bin = auto_detect_proton(settings)
    if not proton_bin or not proton_bin.is_file():
        # Ask user to select manually
        chosen = select_proton_bin(Path(os.path.expanduser("~/.steam/steam/steamapps/common")))
        if not chosen:
            return None
        proton_bin = chosen
        settings["PROTON_BIN"] = str(proton_bin)
        _merge_and_save_settings()

    # Build runner
    runner = build_runner(settings, exe, proton_bin)

    # Check for updates (in case user had an older version)
    exe, updated = maybe_update_bizhawk(
        settings, exe, download_messages=download_messages
    )
    downloaded = downloaded or updated

    ap_version = str(settings.get("AP_VERSION", "") or "")
    connectors_updated = False
    if stage_connectors:
        try:
            connectors_updated = ensure_connectors(
                settings,
                exe,
                ap_version=ap_version if ap_version else None,
                download_messages=download_messages,
            )
        except Exception as exc:
            error_dialog(f"Failed to stage BizHawk connectors: {exc}")
            return None

        downloaded = downloaded or connectors_updated

    # Create a desktop launcher for the runner only when a download occurred.
    if downloaded:
        ensure_bizhawk_desktop_shortcut(settings, runner, enabled=create_shortcut)

    return runner, exe, downloaded


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] != "ensure":
        print("Usage: ap_bizhelper_bizhawk.py ensure", file=sys.stderr)
        return 1

    result = ensure_bizhawk_and_proton()
    if result is None:
        return 1

    runner, _, _ = result
    print(str(runner))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
