#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

CONFIG_DIR = Path(os.path.expanduser("~/.config/ap_bizhelper_test"))
SETTINGS_FILE = CONFIG_DIR / "settings.json"
DATA_DIR = Path(os.path.expanduser("~/.local/share/ap_bizhelper_test"))

BIZHAWK_WIN_DIR = DATA_DIR / "bizhawk_win"
PROTON_PREFIX = DATA_DIR / "proton_prefix"
BIZHAWK_RUNNER = DATA_DIR / "run_bizhawk_proton.py"

GITHUB_API_LATEST = "https://api.github.com/repos/TASEmulators/BizHawk/releases/latest"


def _ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BIZHAWK_WIN_DIR.mkdir(parents=True, exist_ok=True)
    PROTON_PREFIX.mkdir(parents=True, exist_ok=True)


def _load_settings() -> Dict[str, Any]:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        with SETTINGS_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_settings(settings: Dict[str, Any]) -> None:
    _ensure_dirs()
    tmp = SETTINGS_FILE.with_suffix(SETTINGS_FILE.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(SETTINGS_FILE)


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


def info_dialog(message: str) -> None:
    if _has_zenity():
        _run_zenity(["--info", f"--text={message}"])
    else:
        sys.stderr.write(message + "\n")


def error_dialog(message: str) -> None:
    if _has_zenity():
        _run_zenity(["--error", f"--text={message}"])
    else:
        sys.stderr.write("ERROR: " + message + "\n")


def _github_latest_bizhawk() -> Tuple[str, str]:
    """
    Return (download_url, version_tag) for the latest BizHawk Windows x64 zip.
    """
    import urllib.request
    import json as _json

    req = urllib.request.Request(GITHUB_API_LATEST, headers={"User-Agent": "ap-bizhelper/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read().decode("utf-8")
    j = _json.loads(data)

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


def download_and_extract_bizhawk(url: str, version: str) -> Path:
    """
    Download the BizHawk Windows zip and extract it into BIZHAWK_WIN_DIR.

    Returns the detected EmuHawk.exe path.
    """
    import urllib.request
    import zipfile
    import tempfile

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

    # Download zip (no progress for now)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmpf:
        tmp_path = Path(tmpf.name)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ap-bizhelper/1.0"})
        with urllib.request.urlopen(req, timeout=300) as resp, tmp_path.open("wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)

        # Extract
        with zipfile.ZipFile(tmp_path, "r") as zf:
            zf.extractall(BIZHAWK_WIN_DIR)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass

    # Detect EmuHawk.exe
    exe = auto_detect_bizhawk_exe({})
    if exe is None:
        raise RuntimeError("Could not find EmuHawk.exe after extracting BizHawk.")
    return exe


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
    if not _has_zenity():
        return None
    args = ["--file-selection", "--title=Select Proton binary"]
    if initial is not None:
        args.append(f"--filename={initial}")
    code, out = _run_zenity(args)
    if code != 0 or not out:
        return None
    p = Path(out)
    if not p.is_file():
        error_dialog("Selected Proton binary does not exist.")
        return None
    return p


def build_runner(settings: Dict[str, Any], bizhawk_exe: Path, proton_bin: Path) -> Path:
    """
    Ensure the Python BizHawk runner path (run_bizhawk_proton.py) is recorded and executable.

    We no longer generate a bash wrapper here; the dedicated Python
    runner is responsible for configuring Proton and launching EmuHawk.
    """
    _ensure_dirs()
    runner = BIZHAWK_RUNNER

    # If the runner script exists, make sure it's executable.
    if runner.is_file():
        try:
            runner.chmod(runner.stat().st_mode | 0o111)
        except Exception:
            pass

    # Persist the runner path for other helpers to consume.
    settings["BIZHAWK_RUNNER"] = str(runner)
    _save_settings(settings)
    return runner



def ensure_bizhawk_desktop_shortcut(settings: Dict[str, Any], runner: Path) -> None:
    """
    Optionally create a desktop launcher for the BizHawk Proton runner.

    This mirrors the Archipelago AppImage shortcut logic, but uses its own
    settings key BIZHAWK_DESKTOP_SHORTCUT to remember the user's choice.
    """
    # Runner must exist and be executable.
    if not runner.is_file() or not os.access(str(runner), os.X_OK):
        return

    state = str(settings.get("BIZHAWK_DESKTOP_SHORTCUT", "") or "")
    if state:
        return  # already decided

    if not _has_zenity():
        settings["BIZHAWK_DESKTOP_SHORTCUT"] = "no"
        _save_settings(settings)
        return

    code, _ = _run_zenity(
        [
            "--question",
            "--title=BizHawk (Proton) shortcut",
            "--text=Create menu launcher/shortcut for BizHawk (Proton via ap-bizhelper)?",
            "--ok-label=Yes",
            "--cancel-label=No",
        ]
    )
    if code != 0:
        settings["BIZHAWK_DESKTOP_SHORTCUT"] = "no"
        _save_settings(settings)
        return

    applications_dir = Path(os.path.expanduser("~/.local/share/applications"))
    try:
        applications_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    shortcut = applications_dir / "BizHawk-Proton.desktop"
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=BizHawk (Proton)\n"
        f"Exec={runner}\n"
        "Terminal=false\n"
    )
    try:
        with shortcut.open("w", encoding="utf-8") as f:
            f.write(content)
        shortcut.chmod(0o755)
        settings["BIZHAWK_DESKTOP_SHORTCUT"] = "yes"
        _save_settings(settings)
    except Exception:
        settings["BIZHAWK_DESKTOP_SHORTCUT"] = "no"
        _save_settings(settings)


def maybe_update_bizhawk(settings: Dict[str, Any], bizhawk_exe: Path) -> None:
    """
    If BizHawk is installed in our managed directory, check for an update and
    optionally download it.
    """
    try:
        _ = bizhawk_exe.relative_to(BIZHAWK_WIN_DIR)
    except ValueError:
        # User-managed install; don't auto-update.
        return

    try:
        url, latest_ver = _github_latest_bizhawk()
    except Exception:
        return

    current_ver = str(settings.get("BIZHAWK_VERSION", "") or "")
    skip_ver = str(settings.get("BIZHAWK_SKIP_VERSION", "") or "")
    if current_ver == latest_ver or skip_ver == latest_ver:
        return

    if not _has_zenity():
        return

    code, choice = _run_zenity(
        [
            "--question",
            "--title=BizHawk update",
            "--text=A BizHawk update is available. Update now?",
            "--ok-label=Update now",
            "--cancel-label=Later",
            "--extra-button=Skip this version",
        ]
    )
    if code != 0:
        # "Later"
        return

    if choice == "Skip this version":
        settings["BIZHAWK_SKIP_VERSION"] = latest_ver
        _save_settings(settings)
        return

    # Update now
    try:
        new_exe = download_and_extract_bizhawk(url, latest_ver)
    except Exception as e:
        error_dialog(f"BizHawk update failed: {e}")
        return

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

    info_dialog(f"BizHawk updated to {latest_ver}.")


def ensure_bizhawk_and_proton() -> Optional[Path]:
    """
    Ensure BizHawk (Windows) and Proton are configured and runnable.

    On success, returns the Path to the BizHawk runner script (BIZHAWK_RUNNER)
    and persists settings (BIZHAWK_EXE, PROTON_BIN, versions, etc.).

    On failure or user cancellation, returns None.
    """
    _ensure_dirs()
    settings = _load_settings()

    # Existing config?
    exe_str = str(settings.get("BIZHAWK_EXE", "") or "")
    runner_str = str(settings.get("BIZHAWK_RUNNER", "") or "")
    proton_str = str(settings.get("PROTON_BIN", "") or "")

    exe = Path(exe_str) if exe_str else None
    runner = Path(runner_str) if runner_str else None
    proton_bin = Path(proton_str) if proton_str else None

    if exe and exe.is_file() and proton_bin and proton_bin.is_file() and runner and runner.is_file():
        maybe_update_bizhawk(settings, exe)
        # Settings may have changed; reload
        settings = _load_settings()
        runner_str = str(settings.get("BIZHAWK_RUNNER", "") or "")
        if runner_str:
            runner = Path(runner_str)
            if runner.is_file():
                return runner

    # Need to (re)configure BizHawk
    exe = auto_detect_bizhawk_exe(settings)
    if not exe or not exe.is_file():
        if not _has_zenity():
            error_dialog("BizHawk is not configured and zenity is not available for setup.")
            return None

        code, _ = _run_zenity(
            [
                "--question",
                "--title=BizHawk (Proton) setup",
                "--text=BizHawk (Windows) is not configured.\n\n"
                        "Download latest Windows build via GitHub, or cancel?",
                "--ok-label=Download",
                "--cancel-label=Cancel",
            ]
        )
        if code != 0:
            return None

        # Download latest
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
        settings = _load_settings()
        settings["BIZHAWK_EXE"] = str(exe)
        settings["BIZHAWK_VERSION"] = ver
        settings["BIZHAWK_SKIP_VERSION"] = ""
        _save_settings(settings)

    # Ensure Proton
    proton_bin = auto_detect_proton(settings)
    if not proton_bin or not proton_bin.is_file():
        # Ask user to select manually
        chosen = select_proton_bin(Path(os.path.expanduser("~/.steam/steam/steamapps/common")))
        if not chosen:
            return None
        proton_bin = chosen
        settings = _load_settings()
        settings["PROTON_BIN"] = str(proton_bin)
        _save_settings(settings)

    # Build runner
    runner = build_runner(settings, exe, proton_bin)

    # Offer to create a desktop launcher for the runner.
    ensure_bizhawk_desktop_shortcut(settings, runner)

    # Check for updates (in case user had an older version)
    maybe_update_bizhawk(settings, exe)

    return runner


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] != "ensure":
        print("Usage: ap_bizhelper_bizhawk.py ensure", file=sys.stderr)
        return 1

    runner = ensure_bizhawk_and_proton()
    if runner is None:
        return 1

    print(str(runner))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
