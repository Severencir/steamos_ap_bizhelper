#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

# Paths mirror the bash script and the config helper.
CONFIG_DIR = Path(os.path.expanduser("~/.config/ap_bizhelper_test"))
SETTINGS_FILE = CONFIG_DIR / "settings.json"
DATA_DIR = Path(os.path.expanduser("~/.local/share/ap_bizhelper_test"))
AP_APPIMAGE_DEFAULT = DATA_DIR / "Archipelago.AppImage"

GITHUB_API_LATEST = "https://api.github.com/repos/ArchipelagoMW/Archipelago/releases/latest"


def _ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_settings() -> Dict[str, Any]:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        with SETTINGS_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # On any error, treat as empty and let the caller repopulate.
        return {}


def _save_settings(settings: Dict[str, Any]) -> None:
    _ensure_dirs()
    tmp = SETTINGS_FILE.with_suffix(SETTINGS_FILE.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, sort_keys=True)
        f.write("\\n")
    tmp.replace(SETTINGS_FILE)


def _has_zenity() -> bool:
    return subprocess.call(["which", "zenity"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0


def _run_zenity(args: list[str], *, input_text: Optional[str] = None) -> Tuple[int, str]:
    """
    Run zenity with given args, return (exit_code, stdout_text).
    If zenity is not available, returns (127, "").
    """
    if not _has_zenity():
        return 127, ""
    try:
        proc = subprocess.Popen(
            ["zenity", *args],
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out, _ = proc.communicate(input_text)
        return proc.returncode, out.strip()
    except FileNotFoundError:
        return 127, ""


def info_dialog(message: str) -> None:
    if _has_zenity():
        _run_zenity(["--info", f"--text={message}"])
    else:
        # Last resort: print to stderr
        sys.stderr.write(message + "\\n")


def error_dialog(message: str) -> None:
    if _has_zenity():
        _run_zenity(["--error", f"--text={message}"])
    else:
        sys.stderr.write("ERROR: " + message + "\\n")


def choose_install_action(title: str, text: str) -> str:
    """
    Show a dialog offering Download / Select / Cancel.

    Returns "Download", "Select", or "Cancel".
    """
    if not _has_zenity():
        # Without zenity we can't offer a clickable choice safely.
        return "Cancel"

    code, out = _run_zenity(
        [
            "--question",
            f"--title={title}",
            f"--text={text}",
            "--ok-label=Download",
            "--cancel-label=Cancel",
            "--extra-button=Select",
        ]
    )
    if code == 0:
        # Either "Download" (no stdout) or "Select" (stdout = "Select").
        if out == "Select":
            return "Select"
        return "Download"
    # User hit Cancel/close
    return "Cancel"


def select_appimage(initial: Optional[Path] = None) -> Optional[Path]:
    if not _has_zenity():
        return None
    args = ["--file-selection", "--title=Select Archipelago AppImage"]
    if initial is not None:
        args.append(f"--filename={initial}")
    code, out = _run_zenity(args)
    if code != 0 or not out:
        return None
    p = Path(out)
    if not p.is_file():
        error_dialog("Selected file does not exist.")
        return None
    try:
        p.chmod(p.stat().st_mode | 0o111)
    except Exception:
        pass
    return p


def _github_latest_appimage() -> Tuple[str, str]:
    """
    Return (download_url, version_tag) for the latest Archipelago Linux AppImage.

    Raises RuntimeError on failure.
    """
    import urllib.request
    import json as _json

    req = urllib.request.Request(GITHUB_API_LATEST, headers={"User-Agent": "ap-bizhelper/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read().decode("utf-8")
    j = _json.loads(data)

    tag = j.get("tag_name") or ""
    assets = j.get("assets") or []
    pattern = re.compile(r"Archipelago_.*_linux-x86_64\.AppImage$")
    for asset in assets:
        name = asset.get("name") or ""
        if pattern.search(name):
            url = asset.get("browser_download_url")
            if url:
                return url, tag
    raise RuntimeError("Could not find Archipelago Linux AppImage asset in latest release.")


def download_with_progress(url: str, dest: Path, *, title: str, text: str) -> None:
    """Download ``url`` to ``dest`` with optional zenity progress UI."""

    _ensure_dirs()
    if dest.exists():
        try:
            dest.unlink()
        except Exception:
            pass

    # If zenity is available, show a progress dialog.
    if _has_zenity():
        proc = subprocess.Popen(
            [
                "zenity",
                "--progress",
                f"--title={title}",
                f"--text={text}",
                "--percentage=0",
                "--auto-close",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ap-bizhelper/1.0"})
            with urllib.request.urlopen(req, timeout=300) as resp, dest.open("wb") as f:
                total_str = resp.headers.get("Content-Length") or "0"
                try:
                    total = int(total_str)
                except ValueError:
                    total = 0
                downloaded = 0
                chunk_size = 65536
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if proc.stdin and total > 0:
                        percent = max(0, min(100, int(downloaded * 100 / total)))
                        try:
                            proc.stdin.write(f"{percent}\n")
                            proc.stdin.flush()
                        except BrokenPipeError:
                            raise RuntimeError("Download cancelled by user")
                if proc.stdin:
                    try:
                        proc.stdin.write("100\n")
                        proc.stdin.flush()
                    except BrokenPipeError:
                        pass
        except Exception:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            proc.wait(timeout=1)
            if dest.exists():
                try:
                    dest.unlink()
                except Exception:
                    pass
            raise
        finally:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            proc.wait(timeout=5)
    else:
        req = urllib.request.Request(url, headers={"User-Agent": "ap-bizhelper/1.0"})
        with urllib.request.urlopen(req, timeout=300) as resp, dest.open("wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)

    try:
        dest.chmod(dest.stat().st_mode | 0o111)
    except Exception:
        pass


def download_appimage(url: str, dest: Path, version: str) -> None:
    """Download the AppImage to ``dest`` with a zenity progress dialog if possible."""

    download_with_progress(
        url,
        dest,
        title="Archipelago download",
        text=f"Downloading Archipelago {version}...",
    )


def ensure_desktop_shortcut(settings: Dict[str, Any], appimage: Path) -> None:
    """
    Optionally create a desktop shortcut, respecting AP_DESKTOP_SHORTCUT.
    """
    if not appimage.is_file() or not os.access(str(appimage), os.X_OK):
        return

    state = str(settings.get("AP_DESKTOP_SHORTCUT", "") or "")
    if state:
        return  # already decided

    if not _has_zenity():
        settings["AP_DESKTOP_SHORTCUT"] = "no"
        _save_settings(settings)
        return

    code, _ = _run_zenity(
        [
            "--question",
            "--title=Archipelago shortcut",
            "--text=Create menu launcher/shortcut for Archipelago?",
            "--ok-label=Yes",
            "--cancel-label=No",
        ]
    )
    if code != 0:
        settings["AP_DESKTOP_SHORTCUT"] = "no"
        _save_settings(settings)
        return

    applications_dir = Path(os.path.expanduser("~/.local/share/applications"))
    try:
        applications_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    shortcut = applications_dir / "Archipelago.desktop"
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Archipelago\n"
        f"Exec={appimage}\n"
        "Terminal=false\n"
    )
    try:
        with shortcut.open("w", encoding="utf-8") as f:
            f.write(content)
        shortcut.chmod(0o755)
        settings["AP_DESKTOP_SHORTCUT"] = "yes"
        _save_settings(settings)
    except Exception:
        settings["AP_DESKTOP_SHORTCUT"] = "no"
        _save_settings(settings)


def maybe_update_appimage(settings: Dict[str, Any], appimage: Path) -> Path:
    """
    If we manage this AppImage (default path), check GitHub for a newer version.

    Respects AP_SKIP_VERSION. If an update is installed, updates AP_VERSION and
    returns the (possibly new) appimage path.
    """
    # Only auto-update if using the default managed AppImage
    if appimage != AP_APPIMAGE_DEFAULT:
        return appimage

    try:
        url, latest_ver = _github_latest_appimage()
    except Exception:
        return appimage

    current_ver = str(settings.get("AP_VERSION", "") or "")
    skip_ver = str(settings.get("AP_SKIP_VERSION", "") or "")

    if current_ver == latest_ver or skip_ver == latest_ver:
        return appimage

    if not _has_zenity():
        return appimage

    code, choice = _run_zenity(
        [
            "--question",
            "--title=Archipelago update",
            "--text=An Archipelago update is available. Update now?",
            "--ok-label=Update now",
            "--cancel-label=Later",
            "--extra-button=Skip this version",
        ]
    )
    if code != 0:
        # "Later"
        return appimage

    if choice == "Skip this version":
        settings["AP_SKIP_VERSION"] = latest_ver
        _save_settings(settings)
        return appimage

    # Update now
    try:
        download_appimage(url, AP_APPIMAGE_DEFAULT, latest_ver)
    except Exception as e:
        error_dialog(f"Archipelago update failed: {e}")
        return appimage

    settings["AP_APPIMAGE"] = str(AP_APPIMAGE_DEFAULT)
    settings["AP_VERSION"] = latest_ver
    settings["AP_SKIP_VERSION"] = ""
    _save_settings(settings)
    info_dialog(f"Archipelago updated to {latest_ver}.")
    return AP_APPIMAGE_DEFAULT


def ensure_appimage() -> Path:
    """
    Ensure the Archipelago AppImage is configured and up to date.

    On success, returns the Path to the AppImage and persists any changes
    into the JSON settings file. On failure, raises RuntimeError.
    """
    _ensure_dirs()
    settings = _load_settings()

    # 1. Try stored path
    app_path_str = str(settings.get("AP_APPIMAGE", "") or "")
    app_path = Path(app_path_str) if app_path_str else None

    if app_path and app_path.is_file():
        # Make sure it's executable
        try:
            app_path.chmod(app_path.stat().st_mode | 0o111)
        except Exception:
            pass
    else:
        # 2. Try the default managed AppImage
        if AP_APPIMAGE_DEFAULT.is_file():
            app_path = AP_APPIMAGE_DEFAULT
        else:
            app_path = None

    # 3. If still missing, prompt user for download vs select
    if app_path is None or not app_path.is_file() or not os.access(str(app_path), os.X_OK):
        action = choose_install_action(
            "Archipelago setup",
            "Archipelago AppImage is not configured.\\n\\n"
            "Download latest from GitHub, select an existing one, or cancel?",
        )
        if action == "Download":
            try:
                url, ver = _github_latest_appimage()
            except Exception as e:
                error_dialog(f"Failed to query latest Archipelago release: {e}")
                raise RuntimeError("Failed to query latest Archipelago release") from e
            try:
                download_appimage(url, AP_APPIMAGE_DEFAULT, ver)
            except Exception as e:
                error_dialog(f"Archipelago download failed or was cancelled: {e}")
                raise RuntimeError("Archipelago download failed") from e
            app_path = AP_APPIMAGE_DEFAULT
            settings["AP_APPIMAGE"] = str(AP_APPIMAGE_DEFAULT)
            settings["AP_VERSION"] = ver
            settings["AP_SKIP_VERSION"] = ""
            _save_settings(settings)
        elif action == "Select":
            chosen = select_appimage(Path(os.path.expanduser("~")))
            if not chosen:
                # User cancelled selection; treat as overall cancellation.
                raise RuntimeError("User cancelled Archipelago AppImage selection")
            app_path = chosen
            settings["AP_APPIMAGE"] = str(app_path)
            # No version information when manually selected.
            _save_settings(settings)
        else:
            # Cancel
            raise RuntimeError("User cancelled Archipelago setup")

    if app_path is None or not app_path.is_file() or not os.access(str(app_path), os.X_OK):
        error_dialog("Archipelago AppImage was not configured correctly.")
        raise RuntimeError("Archipelago AppImage not configured")

    # 4. Auto-update if applicable
    app_path = maybe_update_appimage(settings, app_path)

    # 5. Optionally create a desktop shortcut
    ensure_desktop_shortcut(settings, app_path)

    return app_path


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] != "ensure":
        print("Usage: ap_bizhelper_ap.py ensure", file=sys.stderr)
        return 1
    try:
        app_path = ensure_appimage()
    except RuntimeError:
        return 1
    print(str(app_path))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
