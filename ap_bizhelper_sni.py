#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Tuple

import shutil
import tempfile

# Config/data roots (only used as a workspace; final lua ends up in BizHawk dir)
CONFIG_DIR = Path(os.path.expanduser("~/.config/ap_bizhelper_test"))
DATA_DIR = Path(os.path.expanduser("~/.local/share/ap_bizhelper_test"))

SNI_WIN_DIR = DATA_DIR / "sni_win"
SNI_WIN_ZIP = SNI_WIN_DIR / "sni-win.zip"
SNI_WIN_URL = "https://github.com/alttpo/sni/releases/download/v0.0.102a/sni-v0.0.102a-windows-amd64.zip"


def _ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SNI_WIN_DIR.mkdir(parents=True, exist_ok=True)


def _has_zenity() -> bool:
    return subprocess.call(["which", "zenity"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0


def _run_zenity(args: list[str]) -> Tuple[int, str]:
    if not _has_zenity():
        return 127, ""
    try:
        proc = subprocess.Popen(
            ["zenity", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out, _ = proc.communicate()
        return proc.returncode, out.strip()
    except FileNotFoundError:
        return 127, ""


def _error_dialog(message: str) -> None:
    if _has_zenity():
        _run_zenity(["--error", f"--text={message}"])
    else:
        sys.stderr.write("ERROR: " + message + "\n")


def _info(message: str) -> None:
    sys.stderr.write(message + "\n")


def _find_lua_dir(root: Path) -> Path | None:
    # Look for a directory literally named "lua" (case-insensitive)
    for current_root, dirs, files in os.walk(root):
        if os.path.basename(current_root).lower() == "lua":
            return Path(current_root)
    return None


def _ensure_connector_lowercase(lua_dir: Path) -> None:
    """Ensure there is lua/connector.lua (rename if necessary, case-insensitively)."""
    connector_candidate = None
    for p in lua_dir.rglob("*.lua"):
        if "connector" in p.name.lower():
            connector_candidate = p
            break

    if connector_candidate is None:
        _error_dialog("Windows SNI Lua directory does not contain a Connector.lua file.")
        raise RuntimeError("No connector.lua found in SNI Lua directory")

    if connector_candidate.name != "connector.lua":
        target = connector_candidate.with_name("connector.lua")
        # If target already exists, replace it
        if target.exists():
            target.unlink()
        connector_candidate.rename(target)


def download_sni_if_needed(bizhawk_dir: Path) -> bool:
    """
    Ensure SNI Lua is installed directly into the BizHawk directory.

    - If bizhawk_dir/lua/connector.lua already exists, do nothing.
    - Otherwise, download the Windows SNI zip from GitHub,
      extract it to a temp location, copy ONLY the 'lua' directory
      into bizhawk_dir/lua, normalize to lua/connector.lua, and
      delete the temporary extracted files.

    Returns True if lua/connector.lua is present under bizhawk_dir.
    """
    _ensure_dirs()

    bizhawk_dir = bizhawk_dir.expanduser().resolve()
    lua_target = bizhawk_dir / "lua" / "connector.lua"

    if lua_target.exists():
        _info(f"[ap-bizhelper] SNI Lua already present at {lua_target}")
        return True

    if not _has_zenity():
        _info("[ap-bizhelper] SNI Lua not present and zenity is unavailable; skipping SNI setup.")
        return False

    code, _ = _run_zenity(
        [
            "--question",
            "--title=SNI (Windows) setup",
            "--text=Windows SNI Lua is required for SNES bridge support in BizHawk.\n\n"
                    "Download the Windows SNI build from GitHub now and install its Lua scripts into\n"
                    f"{bizhawk_dir}/lua ?",
            "--ok-label=Download",
            "--cancel-label=Skip",
        ]
    )
    if code != 0:
        _info("[ap-bizhelper] User skipped Windows SNI download.")
        return False

    extracted_dir = SNI_WIN_DIR / "extracted"

    # Clean any previous extraction
    if extracted_dir.exists():
        shutil.rmtree(extracted_dir, ignore_errors=True)

    extracted_dir.mkdir(parents=True, exist_ok=True)

    tmp_zip: Path | None = None
    try:
        import urllib.request
        import zipfile

        # Download to a temp file (not necessarily SNI_WIN_ZIP, to avoid partial file issues).
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmpf:
            tmp_zip = Path(tmpf.name)

        try:
            req = urllib.request.Request(SNI_WIN_URL, headers={"User-Agent": "ap-bizhelper/1.0"})
            with urllib.request.urlopen(req, timeout=300) as resp, tmp_zip.open("wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)

            # Extract entire archive into extracted_dir
            with zipfile.ZipFile(tmp_zip, "r") as zf:
                zf.extractall(extracted_dir)
        finally:
            try:
                if tmp_zip is not None and tmp_zip.exists():
                    tmp_zip.unlink()
            except Exception:
                pass

        # Find the 'lua' directory inside the extracted tree
        lua_src = _find_lua_dir(extracted_dir)
        if lua_src is None:
            _error_dialog("Downloaded SNI package does not contain a 'lua' directory.")
            return False

        # Install into BizHawk dir: bizhawk_dir/lua
        bizhawk_dir.mkdir(parents=True, exist_ok=True)
        lua_dest = bizhawk_dir / "lua"
        if lua_dest.exists():
            shutil.rmtree(lua_dest, ignore_errors=True)

        shutil.copytree(lua_src, lua_dest)

        # Normalize connector name to lua/connector.lua
        _ensure_connector_lowercase(lua_dest)

    except Exception as e:
        _error_dialog(f"Windows SNI download or extraction failed: {e}")
        return False
    finally:
        # Clean up extracted_dir to avoid leaving a shadow copy around
        if extracted_dir.exists():
            shutil.rmtree(extracted_dir, ignore_errors=True)

    if lua_target.exists():
        _info(f"[ap-bizhelper] Installed Windows SNI Lua into {bizhawk_dir}/lua")
        return True

    _error_dialog("SNI Lua installation did not complete successfully.")
    return False


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] != "ensure":
        print("Usage: ap_bizhelper_sni.py ensure /path/to/BizHawk", file=sys.stderr)
        return 1

    bizhawk_dir = Path(argv[2])
    ok = download_sni_if_needed(bizhawk_dir)
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
