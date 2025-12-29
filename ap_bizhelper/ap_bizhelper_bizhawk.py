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

from .ap_bizhelper_ap import (
    DATA_DIR,
    LEGACY_DATA_DIR,
    _is_newer_version,
    _normalize_asset_digest,
    download_with_progress,
)
from .dialogs import (
    question_dialog as _qt_question_dialog,
    select_file_dialog as _select_file_dialog,
    error_dialog,
    info_dialog,
)
from .ap_bizhelper_config import (
    CONFIG_DIR,
    load_settings as _load_shared_settings,
    save_settings as _save_shared_settings,
)

SETTINGS_FILE = CONFIG_DIR / "settings.json"

BIZHAWK_WIN_DIR = DATA_DIR / "bizhawk_win"
PROTON_PREFIX = DATA_DIR / "proton_prefix"
PROTON_10_URL = "https://github.com/ValveSoftware/Proton/archive/refs/tags/proton-10.0-3.tar.gz"
PROTON_10_VERSION = "10.0-3"
PROTON_10_TAG = "proton-10.0-3"
PROTON_10_DIR = DATA_DIR / "proton_10"

GITHUB_API_LATEST = "https://api.github.com/repos/TASEmulators/BizHawk/releases/latest"
ARCHIPELAGO_RELEASE_API = "https://api.github.com/repos/ArchipelagoMW/Archipelago/releases"
SNI_DOWNLOAD_URL = (
    "https://github.com/alttpo/sni/releases/download/v0.0.102a/"
    "sni-v0.0.102a-windows-amd64.zip"
)
SNI_VERSION = "v0.0.102a"
MIGRATABLE_BIZHAWK_ITEMS = ("connectors", "sni", "Scripts", "Lua", "config.ini")


def _ensure_dirs() -> None:
    if LEGACY_DATA_DIR.exists() and not DATA_DIR.exists():
        try:
            shutil.move(str(LEGACY_DATA_DIR), str(DATA_DIR))
        except Exception:
            pass
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BIZHAWK_WIN_DIR.mkdir(parents=True, exist_ok=True)
    PROTON_PREFIX.mkdir(parents=True, exist_ok=True)
    PROTON_10_DIR.mkdir(parents=True, exist_ok=True)


def _load_settings() -> Dict[str, Any]:
    return _load_shared_settings()


def _save_settings(settings: Dict[str, Any]) -> None:
    _ensure_dirs()
    _save_shared_settings(settings)


def _github_latest_bizhawk() -> Tuple[str, str, str, str]:
    """
    Return (download_url, version_tag, digest, digest_algorithm) for the latest BizHawk Windows x64 zip.
    """
    import urllib.request

    req = urllib.request.Request(GITHUB_API_LATEST, headers={"User-Agent": "ap-bizhelper/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read().decode("utf-8")
    j = json.loads(data)

    tag = j.get("tag_name") or ""
    assets = j.get("assets") or []

    def _asset_digest(asset: dict[str, Any]) -> Tuple[str, str]:
        digest = asset.get("digest")
        name = asset.get("name") or "(unknown)"
        if not digest:
            raise RuntimeError(f"BizHawk asset missing digest: {name}")
        try:
            algo, normalized = _normalize_asset_digest(digest)
        except ValueError as exc:
            raise RuntimeError(f"Invalid digest for asset {name}: {exc}") from exc
        return algo, normalized

    # Prefer assets whose name clearly ends with 'win-x64.zip'
    for asset in assets:
        name = asset.get("name") or ""
        if name.endswith("win-x64.zip"):
            url = asset.get("browser_download_url")
            if url:
                algo, digest = _asset_digest(asset)
                return url, tag, digest, algo

    # Fallback: look for anything containing 'win-x64' and ending in .zip
    pattern = re.compile(r"win-x64.*\.zip$")
    for asset in assets:
        name = asset.get("name") or ""
        if pattern.search(name):
            url = asset.get("browser_download_url")
            if url:
                algo, digest = _asset_digest(asset)
                return url, tag, digest, algo

    raise RuntimeError("Could not find BizHawk win-x64 zip asset in latest release.")


def _archipelago_release(tag: Optional[str] = None) -> Tuple[str, str, str, str]:
    """Return (download_url, version_tag, digest, digest_algorithm) for an Archipelago source archive."""

    import urllib.request

    url = f"{ARCHIPELAGO_RELEASE_API}/latest" if not tag else f"{ARCHIPELAGO_RELEASE_API}/tags/{tag}"
    req = urllib.request.Request(url, headers={"User-Agent": "ap-bizhelper/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read().decode("utf-8")
    j = json.loads(data)

    tag_name = j.get("tag_name") or (tag or "")
    assets = j.get("assets") or []

    def _select_archive(asset: dict[str, Any]) -> Optional[Tuple[str, str, str]]:
        name = asset.get("name") or ""
        url = asset.get("browser_download_url")
        if not name or not url:
            return None
        if not (name.endswith(".tar.gz") or name.endswith(".zip")):
            return None
        digest = asset.get("digest")
        if not digest:
            raise RuntimeError(f"Archipelago release asset missing digest: {name}")
        try:
            algo, normalized = _normalize_asset_digest(digest)
        except ValueError as exc:
            raise RuntimeError(f"Invalid digest for asset {name}: {exc}") from exc
        return url, normalized, algo

    for asset in assets:
        archive = _select_archive(asset)
        if archive:
            url, digest, algo = archive
            return url, tag_name, digest, algo

    raise RuntimeError("Could not locate Archipelago source archive download URL.")


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


def download_and_extract_bizhawk(
    url: str, version: str, *, expected_digest: str, digest_algorithm: str
) -> Path:
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
            expected_hash=expected_digest,
            hash_name=digest_algorithm,
            require_hash=True,
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


def _extract_archipelago_connectors(archive: Path, staging_dir: Path) -> None:
    staging_dir.mkdir(parents=True, exist_ok=True)

    def _extract_tar(tf: tarfile.TarFile) -> bool:
        extracted = False
        for member in tf.getmembers():
            parts = Path(member.name).parts
            try:
                data_idx = parts.index("data")
                if parts[data_idx + 1] != "lua":
                    continue
            except (ValueError, IndexError):
                continue
            if member.islnk() or member.issym():
                raise RuntimeError("Archives containing symbolic links are not supported")

            rel_parts = parts[data_idx + 2 :]
            if rel_parts:
                target_path = _validated_member_path(str(Path(*rel_parts)), staging_dir)
            else:
                target_path = staging_dir

            if member.isdir():
                target_path.mkdir(parents=True, exist_ok=True)
                extracted = True
                continue

            fileobj = tf.extractfile(member)
            if fileobj is None:
                raise RuntimeError(f"Could not read archive member: {member.name}")

            target_path.parent.mkdir(parents=True, exist_ok=True)
            with fileobj, target_path.open("wb") as out:
                shutil.copyfileobj(fileobj, out)
            extracted = True
        return extracted

    def _extract_zip(zf: zipfile.ZipFile) -> bool:
        extracted = False
        for info in zf.infolist():
            parts = Path(info.filename).parts
            try:
                data_idx = parts.index("data")
                if parts[data_idx + 1] != "lua":
                    continue
            except (ValueError, IndexError):
                continue
            if _zipinfo_is_symlink(info):
                raise RuntimeError("Archives containing symbolic links are not supported")

            rel_parts = parts[data_idx + 2 :]
            if rel_parts:
                target_path = _validated_member_path(str(Path(*rel_parts)), staging_dir)
            else:
                target_path = staging_dir

            if info.is_dir():
                target_path.mkdir(parents=True, exist_ok=True)
                extracted = True
                continue

            target_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target_path.open("wb") as out:
                shutil.copyfileobj(src, out)
            extracted = True
        return extracted

    if tarfile.is_tarfile(archive):
        with tarfile.open(archive, "r:*") as tf:
            has_connectors = _extract_tar(tf)
    elif zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive, "r") as zf:
            has_connectors = _extract_zip(zf)
    else:
        raise RuntimeError("Unsupported archive format for connectors.")

    if not has_connectors:
        raise RuntimeError("Archipelago source archive did not contain data/lua directory")


def _apply_archipelago_connector_archive(archive: Path, bizhawk_dir: Path) -> None:
    connectors_dest = bizhawk_dir / "connectors"
    with tempfile.TemporaryDirectory(dir=bizhawk_dir) as td:
        staging_root = Path(td) / "connectors"
        _extract_archipelago_connectors(archive, staging_root)

        if connectors_dest.exists():
            shutil.rmtree(connectors_dest)
        staging_root.rename(connectors_dest)


def _stage_archipelago_connectors(
    bizhawk_dir: Path,
    *,
    ap_version: Optional[str],
    download_messages: Optional[list[str]],
    settings: Optional[Dict[str, Any]] = None,
) -> str:
    """Download Archipelago source and copy data/lua into bizhawk_dir/connectors."""

    url, tag, digest, digest_algo = _archipelago_release(ap_version or None)
    if settings is not None and not ap_version and tag:
        latest_seen = str(settings.get("BIZHAWK_AP_CONNECTOR_LATEST_SEEN_VERSION", "") or "")
        if tag != latest_seen:
            settings["BIZHAWK_AP_CONNECTOR_LATEST_SEEN_VERSION"] = tag
            _save_settings(settings)
    suffix = ".tar.gz" if "tar" in url else ".zip"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmpf:
        tmp_path = Path(tmpf.name)

    try:
        download_with_progress(
            url,
            tmp_path,
            title="Archipelago connectors",
            text=f"Downloading Archipelago connectors ({tag})...",
            expected_hash=digest,
            hash_name=digest_algo,
            require_hash=True,
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
        _apply_sni_connector_archive(tmp_path, bizhawk_dir)
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


def _apply_sni_connector_archive(archive: Path, bizhawk_dir: Path) -> None:
    with tempfile.TemporaryDirectory() as td:
        extracted_root = _extract_archive(archive, Path(td))
        lua_dir = next((p for p in extracted_root.rglob("lua") if p.is_dir()), None)
        if lua_dir is None:
            raise RuntimeError("SNI archive did not contain a lua directory")
        _copy_tree(lua_dir, bizhawk_dir / "sni")


def connectors_need_download(
    settings: Dict[str, Any], bizhawk_exe: Optional[Path], *, ap_version: Optional[str]
) -> bool:
    if bizhawk_exe is None or not bizhawk_exe.is_file():
        return False

    desired_ap_version = ap_version or ""
    current_ap_version = str(settings.get("BIZHAWK_AP_CONNECTOR_VERSION", "") or "")
    connectors_dir = bizhawk_exe.parent / "connectors"
    if current_ap_version != "manual":
        if desired_ap_version != current_ap_version or not _has_archipelago_connector(connectors_dir):
            return True
    elif not _has_archipelago_connector(connectors_dir):
        return True

    current_sni_version = str(settings.get("BIZHAWK_SNI_VERSION", "") or "")
    sni_dir = bizhawk_exe.parent / "sni"
    if current_sni_version != "manual":
        if current_sni_version != SNI_VERSION or not _has_sni_connector(sni_dir):
            return True
    elif not _has_sni_connector(sni_dir):
        return True

    return False


def ensure_connectors(
    settings: Dict[str, Any],
    bizhawk_exe: Path,
    *,
    ap_version: Optional[str],
    download_messages: Optional[list[str]],
    ap_archive_path: Optional[Path] = None,
    sni_archive_path: Optional[Path] = None,
    allow_download: bool = True,
    allow_manual_selection: bool = False,
) -> bool:
    """Ensure connector directories are present for BizHawk installs."""

    bizhawk_dir = bizhawk_exe.parent
    updated = False

    def _infer_version_from_archive_name(archive: Path) -> str:
        match = re.search(r"(v?\d+\.\d+\.\d+[a-z0-9.\-]*)", archive.name, re.IGNORECASE)
        if match:
            return match.group(1)
        return "manual"

    def _select_archipelago_archive() -> Optional[Path]:
        selection = _select_file_dialog(
            title="Select Archipelago source archive",
            initial=Path.home(),
            file_filter="*.zip *.tar.gz",
            dialog_key="archipelago_connectors",
        )
        if selection is None:
            return None
        if not selection.is_file():
            error_dialog("Selected Archipelago archive does not exist.")
            return None
        return selection

    def _select_sni_archive() -> Optional[Path]:
        selection = _select_file_dialog(
            title="Select SNI connectors zip",
            initial=Path.home(),
            file_filter="*.zip",
            dialog_key="sni_connectors",
        )
        if selection is None:
            return None
        if not selection.is_file():
            error_dialog("Selected SNI archive does not exist.")
            return None
        return selection

    def _stage_archipelago_from_archive(archive: Path) -> str:
        _apply_archipelago_connector_archive(archive, bizhawk_dir)
        version = _infer_version_from_archive_name(archive)
        if download_messages is not None:
            download_messages.append(
                f"Staged BizHawk connectors from {archive.name} ({version})"
            )
        return version

    def _stage_sni_from_archive(archive: Path) -> str:
        _apply_sni_connector_archive(archive, bizhawk_dir)
        version = _infer_version_from_archive_name(archive)
        if download_messages is not None:
            download_messages.append(f"Staged BizHawk SNI connectors from {archive.name}")
        return version

    desired_ap_version = ap_version or ""
    current_ap_version = str(settings.get("BIZHAWK_AP_CONNECTOR_VERSION", "") or "")
    connectors_dir = bizhawk_dir / "connectors"
    if desired_ap_version != current_ap_version or not _has_archipelago_connector(connectors_dir):
        chosen_ap_archive = ap_archive_path
        if chosen_ap_archive is None and not allow_download and allow_manual_selection:
            chosen_ap_archive = _select_archipelago_archive()
            if chosen_ap_archive is None:
                raise RuntimeError("Archipelago connectors selection was cancelled.")

        if chosen_ap_archive is not None:
            tag = _stage_archipelago_from_archive(chosen_ap_archive)
            settings["BIZHAWK_AP_CONNECTOR_VERSION"] = tag
            if tag and tag != "manual":
                settings["BIZHAWK_AP_CONNECTOR_LATEST_SEEN_VERSION"] = tag
            updated = True
        elif allow_download:
            try:
                tag = _stage_archipelago_connectors(
                    bizhawk_dir,
                    ap_version=ap_version,
                    download_messages=download_messages,
                    settings=settings,
                )
            except Exception:
                if allow_manual_selection:
                    chosen_ap_archive = _select_archipelago_archive()
                    if chosen_ap_archive is None:
                        raise RuntimeError(
                            "Archipelago connectors download failed and selection was cancelled."
                        )
                    tag = _stage_archipelago_from_archive(chosen_ap_archive)
                else:
                    raise
            settings["BIZHAWK_AP_CONNECTOR_VERSION"] = tag
            if tag:
                settings["BIZHAWK_AP_CONNECTOR_LATEST_SEEN_VERSION"] = tag
            updated = True

    current_sni_version = str(settings.get("BIZHAWK_SNI_VERSION", "") or "")
    sni_dir = bizhawk_dir / "sni"
    if current_sni_version != SNI_VERSION or not _has_sni_connector(sni_dir):
        chosen_sni_archive = sni_archive_path
        if chosen_sni_archive is None and not allow_download and allow_manual_selection:
            chosen_sni_archive = _select_sni_archive()
            if chosen_sni_archive is None:
                raise RuntimeError("SNI connectors selection was cancelled.")

        if chosen_sni_archive is not None:
            sni_version = _stage_sni_from_archive(chosen_sni_archive)
            settings["BIZHAWK_SNI_VERSION"] = sni_version
            updated = True
        elif allow_download:
            try:
                _stage_sni_connectors(bizhawk_dir, download_messages)
                sni_version = SNI_VERSION
            except Exception:
                if allow_manual_selection:
                    chosen_sni_archive = _select_sni_archive()
                    if chosen_sni_archive is None:
                        raise RuntimeError(
                            "SNI connectors download failed and selection was cancelled."
                        )
                    sni_version = _stage_sni_from_archive(chosen_sni_archive)
                else:
                    raise
            settings["BIZHAWK_SNI_VERSION"] = sni_version
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


def _bizhawk_dir_is_safe(bizhawk_dir: Path) -> bool:
    try:
        resolved_dir = bizhawk_dir.resolve()
        resolved_root = BIZHAWK_WIN_DIR.resolve()
    except Exception:
        return False

    if not resolved_dir.is_dir():
        return False
    return resolved_dir.is_relative_to(resolved_root)


def _snapshot_bizhawk_install(bizhawk_dir: Optional[Path]) -> Optional[Path]:
    if bizhawk_dir is None or not _bizhawk_dir_is_safe(bizhawk_dir):
        return None

    staging_dir = Path(tempfile.mkdtemp(prefix="bizhawk_migrate_"))
    staged_any = False

    for item in MIGRATABLE_BIZHAWK_ITEMS:
        src = bizhawk_dir / item
        if not src.exists():
            continue
        dest = staging_dir / item
        if src.is_dir():
            shutil.copytree(src, dest)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        staged_any = True

    if not staged_any:
        shutil.rmtree(staging_dir, ignore_errors=True)
        return None

    return staging_dir


def _restore_bizhawk_install(snapshot_dir: Optional[Path], bizhawk_dir: Path) -> None:
    if snapshot_dir is None:
        return

    try:
        for item in MIGRATABLE_BIZHAWK_ITEMS:
            src = snapshot_dir / item
            if not src.exists():
                continue
            dest = bizhawk_dir / item
            if dest.exists():
                if dest.is_dir():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()
            if src.is_dir():
                shutil.copytree(src, dest)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
    finally:
        shutil.rmtree(snapshot_dir, ignore_errors=True)


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


def _default_steam_common_dir() -> Path:
    steam_root = Path(os.path.expanduser("~/.steam/steam"))
    return steam_root / "steamapps" / "common"


def _find_proton_binary(root: Path) -> Optional[Path]:
    direct = root / "proton"
    if direct.is_file():
        return direct

    candidates = sorted(path for path in root.rglob("proton") if path.is_file())
    if not candidates:
        return None
    return candidates[0]


def detect_pinned_proton_in_steam() -> Optional[Path]:
    common = _default_steam_common_dir()
    if not common.exists():
        return None

    candidates = [
        common / "Proton 10.0-3" / "proton",
        common / "Proton 10.0" / "proton",
        common / "Proton 10" / "proton",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def detect_local_pinned_proton() -> Optional[Path]:
    if not PROTON_10_DIR.exists():
        return None
    return _find_proton_binary(PROTON_10_DIR)


def proton_available(settings: Dict[str, Any]) -> bool:
    if detect_pinned_proton_in_steam():
        return True

    proton_str = str(settings.get("PROTON_BIN", "") or "")
    if proton_str:
        proton_bin = Path(proton_str)
        if proton_bin.is_file():
            return True

    if detect_local_pinned_proton():
        return True

    return False


def _extract_proton_archive(archive: Path) -> Path:
    if PROTON_10_DIR.exists():
        shutil.rmtree(PROTON_10_DIR)

    _extract_archive(archive, PROTON_10_DIR)
    proton_bin = _find_proton_binary(PROTON_10_DIR)
    if not proton_bin:
        raise RuntimeError("Could not locate Proton binary after extracting Proton 10.")
    try:
        proton_bin.chmod(proton_bin.stat().st_mode | 0o111)
    except Exception:
        pass
    return proton_bin


def download_and_extract_proton_10(*, download_messages: Optional[list[str]] = None) -> Path:
    _ensure_dirs()
    tmp_path = PROTON_10_DIR / f"proton-{PROTON_10_VERSION}.tar.gz"
    try:
        download_with_progress(
            PROTON_10_URL,
            tmp_path,
            title="Proton 10 download",
            text=f"Downloading Proton {PROTON_10_VERSION}...",
        )
        proton_bin = _extract_proton_archive(tmp_path)
        if download_messages is not None:
            download_messages.append(f"Downloaded Proton {PROTON_10_VERSION}")
        return proton_bin
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass


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


def manual_select_bizhawk(settings: Optional[Dict[str, Any]] = None) -> Optional[Path]:
    """Prompt for an existing BizHawk exe and persist the selection."""

    provided_settings = settings
    settings = settings if settings is not None else _load_settings()

    exe = select_bizhawk_exe(Path(os.path.expanduser("~")))
    if not exe:
        return None

    settings["BIZHAWK_EXE"] = str(exe)
    settings["BIZHAWK_VERSION"] = ""
    settings["BIZHAWK_SKIP_VERSION"] = ""
    _save_settings(settings)

    proton_str = str(settings.get("PROTON_BIN", "") or "")
    if proton_str:
        proton_bin = Path(proton_str)
        if proton_bin.is_file():
            build_runner(settings, exe, proton_bin)

    if provided_settings is not None and settings is not provided_settings:
        merged = {**provided_settings, **settings}
        provided_settings.clear()
        provided_settings.update(merged)

    return exe


def force_update_bizhawk(settings: Optional[Dict[str, Any]] = None) -> bool:
    """Force a download of the latest BizHawk build into the managed location."""

    provided_settings = settings
    settings = settings if settings is not None else _load_settings()

    try:
        url, latest_ver, latest_digest, latest_algo = _github_latest_bizhawk()
    except Exception as exc:
        error_dialog(f"Failed to query latest BizHawk release: {exc}")
        return False

    snapshot_dir = None
    existing_exe_str = str(settings.get("BIZHAWK_EXE", "") or "")
    existing_exe = Path(existing_exe_str) if existing_exe_str else None
    if existing_exe and existing_exe.is_file():
        snapshot_dir = _snapshot_bizhawk_install(existing_exe.parent)

    try:
        new_exe = download_and_extract_bizhawk(
            url, latest_ver, expected_digest=latest_digest, digest_algorithm=latest_algo
        )
    except Exception as exc:
        if snapshot_dir and existing_exe:
            _restore_bizhawk_install(snapshot_dir, existing_exe.parent)
        error_dialog(f"BizHawk update failed: {exc}")
        return False
    if snapshot_dir:
        _restore_bizhawk_install(snapshot_dir, new_exe.parent)

    settings["BIZHAWK_EXE"] = str(new_exe)
    settings["BIZHAWK_VERSION"] = latest_ver
    settings["BIZHAWK_SKIP_VERSION"] = ""
    settings["BIZHAWK_LATEST_SEEN_VERSION"] = latest_ver
    _save_settings(settings)

    proton_str = str(settings.get("PROTON_BIN", "") or "")
    if proton_str:
        proton_bin = Path(proton_str)
        if proton_bin.is_file():
            build_runner(settings, new_exe, proton_bin)

    if provided_settings is not None and settings is not provided_settings:
        merged = {**provided_settings, **settings}
        provided_settings.clear()
        provided_settings.update(merged)

    info_dialog(f"BizHawk updated to {latest_ver}.")
    return True


def _load_bizhawk_exe_from_settings(settings: Dict[str, Any]) -> Optional[Path]:
    exe_str = str(settings.get("BIZHAWK_EXE", "") or "")
    exe = Path(exe_str) if exe_str else None
    if not exe or not exe.is_file():
        error_dialog("BizHawk is not configured; cannot update connectors.")
        return None
    return exe


def manual_select_connectors(settings: Optional[Dict[str, Any]] = None) -> bool:
    settings = settings if settings is not None else _load_settings()
    exe = _load_bizhawk_exe_from_settings(settings)
    if exe is None:
        return False
    ap_version = str(settings.get("AP_VERSION", "") or "") or None
    try:
        return ensure_connectors(
            settings,
            exe,
            ap_version=ap_version,
            download_messages=None,
            allow_download=False,
            allow_manual_selection=True,
        )
    except Exception as exc:
        error_dialog(str(exc))
        return False


def force_update_connectors(settings: Optional[Dict[str, Any]] = None) -> bool:
    settings = settings if settings is not None else _load_settings()
    exe = _load_bizhawk_exe_from_settings(settings)
    if exe is None:
        return False
    ap_version = str(settings.get("AP_VERSION", "") or "") or None
    try:
        return ensure_connectors(
            settings,
            exe,
            ap_version=ap_version,
            download_messages=None,
            allow_download=True,
            allow_manual_selection=False,
        )
    except Exception as exc:
        error_dialog(str(exc))
        return False


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
        url, latest_ver, latest_digest, latest_algo = _github_latest_bizhawk()
    except Exception:
        return bizhawk_exe, False

    current_ver = str(settings.get("BIZHAWK_VERSION", "") or "")
    skip_ver = str(settings.get("BIZHAWK_SKIP_VERSION", "") or "")
    latest_seen = str(settings.get("BIZHAWK_LATEST_SEEN_VERSION", "") or "")
    should_prompt = _is_newer_version(latest_ver, latest_seen)
    if latest_ver and latest_ver != latest_seen:
        settings["BIZHAWK_LATEST_SEEN_VERSION"] = latest_ver
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
        settings["BIZHAWK_SKIP_VERSION"] = latest_ver
        settings["BIZHAWK_LATEST_SEEN_VERSION"] = latest_ver
        _save_settings(settings)
        return bizhawk_exe, False

    # Update now
    snapshot_dir = _snapshot_bizhawk_install(bizhawk_exe.parent)
    try:
        new_exe = download_and_extract_bizhawk(
            url, latest_ver, expected_digest=latest_digest, digest_algorithm=latest_algo
        )
    except Exception as e:
        _restore_bizhawk_install(snapshot_dir, bizhawk_exe.parent)
        error_dialog(f"BizHawk update failed: {e}")
        return bizhawk_exe, False
    _restore_bizhawk_install(snapshot_dir, new_exe.parent)

    settings["BIZHAWK_EXE"] = str(new_exe)
    settings["BIZHAWK_VERSION"] = latest_ver
    settings["BIZHAWK_SKIP_VERSION"] = ""
    settings["BIZHAWK_LATEST_SEEN_VERSION"] = latest_ver
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
    download_proton: bool = False,
    create_shortcut: bool = False,
    download_messages: Optional[list[str]] = None,
    settings: Optional[Dict[str, Any]] = None,
    stage_connectors: bool = True,
    ap_connector_archive: Optional[Path] = None,
    sni_connector_archive: Optional[Path] = None,
    allow_manual_connector_selection: bool = False,
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
    pinned_proton = detect_pinned_proton_in_steam()
    if pinned_proton and pinned_proton.is_file():
        proton_bin = pinned_proton
        if str(settings.get("PROTON_BIN", "") or "") != str(pinned_proton):
            settings["PROTON_BIN"] = str(pinned_proton)
            _merge_and_save_settings()

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
                url, ver, digest, digest_algo = _github_latest_bizhawk()
            except Exception as e:
                error_dialog(f"Failed to query latest BizHawk release: {e}")
                return None
            settings["BIZHAWK_LATEST_SEEN_VERSION"] = ver
            try:
                exe = download_and_extract_bizhawk(
                    url, ver, expected_digest=digest, digest_algorithm=digest_algo
                )
            except Exception as e:
                error_dialog(f"BizHawk download failed or was cancelled: {e}")
                return None
            settings["BIZHAWK_EXE"] = str(exe)
            settings["BIZHAWK_VERSION"] = ver
            settings["BIZHAWK_SKIP_VERSION"] = ""
            settings["BIZHAWK_LATEST_SEEN_VERSION"] = ver
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
    proton_bin = detect_pinned_proton_in_steam()
    if not proton_bin or not proton_bin.is_file():
        proton_bin = detect_local_pinned_proton()

    if not proton_bin or not proton_bin.is_file():
        if proton_str:
            proton_candidate = Path(proton_str)
            if proton_candidate.is_file():
                proton_bin = proton_candidate

    if not proton_bin or not proton_bin.is_file():
        if download_proton:
            try:
                proton_bin = download_and_extract_proton_10(
                    download_messages=download_messages
                )
            except Exception as e:
                error_dialog(f"Proton download failed or was cancelled: {e}")
                return None
            downloaded = True
        else:
            chosen = select_proton_bin(_default_steam_common_dir())
            if not chosen:
                error_dialog("Proton selection was cancelled.")
                return None
            proton_bin = chosen

    if proton_bin and proton_bin.is_file():
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
    if stage_connectors or allow_manual_connector_selection or ap_connector_archive or sni_connector_archive:
        try:
            connectors_updated = ensure_connectors(
                settings,
                exe,
                ap_version=ap_version if ap_version else None,
                download_messages=download_messages,
                ap_archive_path=ap_connector_archive,
                sni_archive_path=sni_connector_archive,
                allow_download=stage_connectors,
                allow_manual_selection=allow_manual_connector_selection,
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
