"""Helpers for BizHawk Lua wiring and admin-warning suppression."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from ap_bizhelper_ui import error_dialog, has_zenity, info_dialog


CONFIG_DIR = Path(os.path.expanduser("~/.config/ap_bizhelper_test"))
DATA_DIR = Path(os.path.expanduser("~/.local/share/ap_bizhelper_test"))
SNI_WIN_DIR = DATA_DIR / "sni_win"
SNI_WIN_ZIP = SNI_WIN_DIR / "sni-win.zip"
SNI_WIN_URL = "https://github.com/alttpo/sni/releases/download/v0.0.102a/sni-v0.0.102a-windows-amd64.zip"


def _ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SNI_WIN_DIR.mkdir(parents=True, exist_ok=True)


def ensure_bizhawk_skip_admin_warning(bizhawk_exe: Path) -> None:
    key = "SkipSuperuserPrivsCheck"
    exe_dir = bizhawk_exe.parent
    cfg = None
    for candidate in exe_dir.rglob("config.ini"):
        cfg = candidate
        break

    if cfg is None:
        info_dialog("[ap-bizhelper] No BizHawk config.ini found; skipping admin-warning tweak this run.")
        return

    content = cfg.read_text(encoding="utf-8", errors="ignore")
    if key in content:
        new_content = content
        new_content = new_content.replace(f'"{key}": false', f'"{key}": true')
        new_content = new_content.replace(f"{key}=false", f"{key}=true")
    else:
        new_content = content + f"\n{key}=true\n"

    if new_content != content:
        cfg.write_text(new_content, encoding="utf-8")
        info_dialog(f"[ap-bizhelper] Ensured {key}=true in BizHawk config: {cfg}")


def _resolve_connector_from_appimage() -> Path | None:
    search_roots = [Path("/tmp"), Path.home()]
    for root in search_roots:
        try:
            for candidate in root.rglob("Connector.lua"):
                if ".mount_Archip" in candidate.as_posix() and "SNI" in candidate.as_posix():
                    return candidate
        except Exception:
            continue
    return None


def ensure_sni_prepared() -> None:
    """Front-load consent/download for Windows SNI."""

    _ensure_dirs()
    extracted = SNI_WIN_DIR / "extracted"
    if extracted.exists():
        return

    if not has_zenity():
        info_dialog("[ap-bizhelper] SNI not prepared and zenity unavailable; skipping download.")
        return

    import urllib.request
    import zipfile
    import tempfile

    code = os.system(
        "zenity --question --title='SNI setup' "
        "--text='SNI (SNES bridge) is required to auto-inject the Lua connector into BizHawk for SNES worlds.\\n\\nDownload and set it up now?' "
        "--ok-label='Download' --cancel-label='Skip'"
    )
    if code != 0:
        info_dialog("[ap-bizhelper] User skipped SNI setup.")
        return

    extracted.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
        tmp_path = Path(tmp.name)
    try:
        req = urllib.request.Request(SNI_WIN_URL, headers={"User-Agent": "ap-bizhelper/1.0"})
        with urllib.request.urlopen(req, timeout=300) as resp, tmp_path.open("wb") as f:
            shutil.copyfileobj(resp, f)

        with zipfile.ZipFile(tmp_path, "r") as zf:
            zf.extractall(extracted)
        info_dialog("[ap-bizhelper] SNI downloaded and prepared.")
    except Exception as e:
        error_dialog(f"SNI download failed: {e}")
        shutil.rmtree(extracted, ignore_errors=True)
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass


def ensure_sfc_lua_path(bizhawk_exe: Path) -> None:
    """
    Install the hybrid SNI Lua (Connector.lua from AP AppImage, Windows SNI
    binaries from the downloaded zip) into the BizHawk directory.
    """

    _ensure_dirs()
    biz_dir = bizhawk_exe.parent
    existing = biz_dir / "SNI" / "lua" / "Connector.lua"
    if existing.is_file():
        return

    extracted = SNI_WIN_DIR / "extracted"
    if not extracted.exists():
        info_dialog("[ap-bizhelper] Windows SNI not prepared; skipping Lua injection.")
        return

    connector = _resolve_connector_from_appimage()
    if connector is None or not connector.is_file():
        info_dialog("[ap-bizhelper] Could not locate Connector.lua in Archipelago AppImage.")
        return

    (biz_dir / "SNI" / "lua").mkdir(parents=True, exist_ok=True)
    shutil.copy2(connector, existing)
    shutil.copytree(extracted, biz_dir / "SNI", dirs_exist_ok=True)
    info_dialog(f"[ap-bizhelper] Installed hybrid SNI into {biz_dir}/SNI")

