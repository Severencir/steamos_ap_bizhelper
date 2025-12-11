#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Paths mirror the bash script and the config helper.
CONFIG_DIR = Path(os.path.expanduser("~/.config/ap_bizhelper_test"))
SETTINGS_FILE = CONFIG_DIR / "settings.json"
DATA_DIR = Path(os.path.expanduser("~/.local/share/ap_bizhelper_test"))
AP_APPIMAGE_DEFAULT = DATA_DIR / "Archipelago.AppImage"
DESKTOP_DIR = Path(os.path.expanduser("~/Desktop"))

GITHUB_API_LATEST = "https://api.github.com/repos/ArchipelagoMW/Archipelago/releases/latest"

_QT_APP: Optional["QtWidgets.QApplication"] = None
_QT_MIN_POINT_SIZE = 12


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


def _has_qt_dialogs() -> bool:
    return importlib.util.find_spec("PySide6") is not None


def _ensure_qt_app() -> "QtWidgets.QApplication":
    global _QT_APP

    if _QT_APP is not None:
        return _QT_APP

    from PySide6 import QtGui, QtWidgets

    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(sys.argv[:1] or ["ap-bizhelper"])

    font: QtGui.QFont = app.font()
    if font.pointSize() > 0 and font.pointSize() < _QT_MIN_POINT_SIZE:
        font.setPointSize(_QT_MIN_POINT_SIZE)
    elif font.pixelSize() > 0:
        font.setPixelSize(max(font.pixelSize(), int(_QT_MIN_POINT_SIZE * 1.5)))
    app.setFont(font)

    _QT_APP = app
    return app


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


def _qt_question_dialog(
    *, title: str, text: str, ok_label: str, cancel_label: str, extra_label: Optional[str] = None
) -> str:
    from PySide6 import QtWidgets

    _ensure_qt_app()
    box = QtWidgets.QMessageBox()
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QtWidgets.QMessageBox.Question)
    ok_button = box.addButton(ok_label, QtWidgets.QMessageBox.AcceptRole)
    cancel_button = box.addButton(cancel_label, QtWidgets.QMessageBox.RejectRole)
    extra_button = None
    if extra_label:
        extra_button = box.addButton(extra_label, QtWidgets.QMessageBox.ActionRole)
    box.setDefaultButton(ok_button)
    box.exec()
    clicked = box.clickedButton()
    if clicked == ok_button:
        return "ok"
    if extra_button is not None and clicked == extra_button:
        return "extra"
    return "cancel"


def _qt_file_dialog(
    *, title: str, initial: Optional[Path] = None, file_filter: Optional[str] = None
) -> Optional[Path]:
    from PySide6 import QtWidgets

    _ensure_qt_app()
    start_dir = str(initial) if initial is not None else str(Path.home())
    filter_text = file_filter or "All Files (*)"
    selected, _ = QtWidgets.QFileDialog.getOpenFileName(
        None, title, start_dir, filter_text, options=QtWidgets.QFileDialog.Options()
    )
    return Path(selected) if selected else None


def _zenity_file_dialog(
    *, title: str, initial: Optional[Path] = None, file_filter: Optional[str] = None
) -> Optional[Path]:
    args = ["--file-selection", f"--title={title}"]
    if initial is not None:
        args.append(f"--filename={initial}")
    if file_filter:
        args.append(f"--file-filter={file_filter}")

    code, out = _run_zenity(args)
    if code != 0 or not out:
        return None
    candidate = Path(out)
    return candidate if candidate.is_file() else None


def _select_file_dialog(
    *, title: str, initial: Optional[Path] = None, file_filter: Optional[str] = None
) -> Optional[Path]:
    if _has_qt_dialogs():
        selection = _qt_file_dialog(title=title, initial=initial, file_filter=file_filter)
        if selection is not None:
            return selection
    if _has_zenity():
        selection = _zenity_file_dialog(title=title, initial=initial, file_filter=file_filter)
        if selection is not None:
            return selection
    return None


def info_dialog(message: str) -> None:
    if _has_qt_dialogs():
        from PySide6 import QtWidgets

        _ensure_qt_app()
        box = QtWidgets.QMessageBox()
        box.setIcon(QtWidgets.QMessageBox.Information)
        box.setWindowTitle("Information")
        box.setText(message)
        box.exec()
        return

    if _has_zenity():
        _run_zenity(["--info", f"--text={message}"])
    else:
        # Last resort: print to stderr
        sys.stderr.write(message + "\n")

def error_dialog(message: str) -> None:
    if _has_qt_dialogs():
        from PySide6 import QtWidgets

        _ensure_qt_app()
        box = QtWidgets.QMessageBox()
        box.setIcon(QtWidgets.QMessageBox.Critical)
        box.setWindowTitle("Error")
        box.setText(message)
        box.exec()
        return

    if _has_zenity():
        _run_zenity(["--error", f"--text={message}"])
    else:
        sys.stderr.write("ERROR: " + message + "\n")

def choose_install_action(title: str, text: str, select_label: str = "Select") -> str:
    """
    Show a dialog offering Download / Select / Cancel.

    Returns "Download", "Select", or "Cancel". ``select_label`` customizes the
    text shown for the "Select" button.
    """
    if _has_qt_dialogs():
        choice = _qt_question_dialog(
            title=title,
            text=text,
            ok_label="Download",
            cancel_label="Cancel",
            extra_label=select_label,
        )
        if choice == "extra":
            return "Select"
        if choice == "ok":
            return "Download"
        return "Cancel"

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
            f"--extra-button={select_label}",
        ]
    )
    if out == select_label or code == 5:
        # Extra button: select a local file.
        return "Select"
    if code == 0:
        # "Download" was chosen.
        return "Download"
    # User hit Cancel/close
    return "Cancel"


def select_appimage(initial: Optional[Path] = None) -> Optional[Path]:
    selection = _select_file_dialog(title="Select Archipelago AppImage", initial=initial)
    if selection is None:
        return None
    p = selection
    if not p.is_file():
        error_dialog("Selected file does not exist.")
        return None
    try:
        p.chmod(p.stat().st_mode | 0o111)
    except Exception:
        pass
    return p


def _prompt_select_existing_appimage(initial: Path) -> Path:
    """Prompt the user to select an existing AppImage without offering download."""

    if _has_qt_dialogs():
        choice = _qt_question_dialog(
            title="Archipelago setup",
            text="Archipelago was not selected for download.\n\nSelect an existing AppImage to continue?",
            ok_label="Select AppImage",
            cancel_label="Cancel",
        )
        if choice != "ok":
            raise RuntimeError("User cancelled Archipelago AppImage selection")
    elif not _has_zenity():
        raise RuntimeError("zenity is required to select an Archipelago AppImage.")
    else:
        code, _ = _run_zenity(
            [
                "--question",
                "--title=Archipelago setup",
                "--text=Archipelago was not selected for download.\n\nSelect an existing AppImage to continue?",
                "--ok-label=Select AppImage",
                "--cancel-label=Cancel",
            ]
        )
        if code != 0:
            raise RuntimeError("User cancelled Archipelago AppImage selection")

    chosen = select_appimage(initial)
    if not chosen:
        raise RuntimeError("User cancelled Archipelago AppImage selection")

    return chosen
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


def download_appimage(
    url: str, dest: Path, version: str, *, download_messages: Optional[list[str]] = None
) -> None:
    """Download the AppImage to ``dest`` with a zenity progress dialog if possible."""

    download_with_progress(
        url,
        dest,
        title="Archipelago download",
        text=f"Downloading Archipelago {version}...",
    )
    if download_messages is not None:
        download_messages.append(f"Downloaded Archipelago {version}")


def _desktop_shortcut_path(name: str) -> Path:
    return DESKTOP_DIR / f"{name}.desktop"


def _write_desktop_shortcut(path: Path, name: str, exec_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={name}\n"
        f"Exec={exec_path}\n"
        "Terminal=false\n"
    )
    with path.open("w", encoding="utf-8") as f:
        f.write(content)
    path.chmod(0o755)


def _create_desktop_shortcut(
    settings: Dict[str, Any],
    name: str,
    exec_path: Path,
    settings_key: str,
    *,
    enabled: bool,
) -> None:
    shortcut_path = _desktop_shortcut_path(name)

    if not enabled:
        settings[settings_key] = "no"
        _save_settings(settings)
        return

    try:
        _write_desktop_shortcut(shortcut_path, name, exec_path)
        settings[settings_key] = "yes"
        _save_settings(settings)
    except Exception as exc:  # pragma: no cover - filesystem edge cases
        settings[settings_key] = "no"
        _save_settings(settings)
        error_dialog(f"Failed to create Desktop shortcut: {exc}")


def maybe_update_appimage(
    settings: Dict[str, Any], appimage: Path, *, download_messages: Optional[list[str]] = None
) -> Tuple[Path, bool]:
    """
    If we manage this AppImage (default path), check GitHub for a newer version.

    Respects AP_SKIP_VERSION. If an update is installed, updates AP_VERSION and
    returns the (possibly new) appimage path along with a flag indicating whether
    a download occurred.
    """
    # Only auto-update if using the default managed AppImage
    if appimage != AP_APPIMAGE_DEFAULT:
        return appimage, False

    try:
        url, latest_ver = _github_latest_appimage()
    except Exception:
        return appimage, False

    current_ver = str(settings.get("AP_VERSION", "") or "")
    skip_ver = str(settings.get("AP_SKIP_VERSION", "") or "")

    if not current_ver:
        return appimage, False

    if current_ver == latest_ver or skip_ver == latest_ver:
        return appimage, False

    if _has_qt_dialogs():
        choice = _qt_question_dialog(
            title="Archipelago update",
            text="An Archipelago update is available. Update now?",
            ok_label="Update now",
            cancel_label="Later",
            extra_label="Skip this version",
        )
        if choice == "cancel":
            return appimage, False
        if choice == "extra":
            settings["AP_SKIP_VERSION"] = latest_ver
            _save_settings(settings)
            return appimage, False
    elif not _has_zenity():
        return appimage, False
    else:
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
            return appimage, False
        if choice == "Skip this version":
            settings["AP_SKIP_VERSION"] = latest_ver
            _save_settings(settings)
            return appimage, False

    # Update now
    try:
        download_appimage(url, AP_APPIMAGE_DEFAULT, latest_ver, download_messages=download_messages)
    except Exception as e:
        error_dialog(f"Archipelago update failed: {e}")
        return appimage, False

    settings["AP_APPIMAGE"] = str(AP_APPIMAGE_DEFAULT)
    settings["AP_VERSION"] = latest_ver
    settings["AP_SKIP_VERSION"] = ""
    _save_settings(settings)
    if download_messages is not None:
        download_messages.append(f"Updated Archipelago to {latest_ver}")
    else:
        info_dialog(f"Archipelago updated to {latest_ver}.")
    return AP_APPIMAGE_DEFAULT, True
def ensure_appimage(
    *,
    download_selected: bool = True,
    create_shortcut: bool = False,
    download_messages: Optional[list[str]] = None,
) -> Path:
    """
    Ensure the Archipelago AppImage is configured and up to date.

    On success, returns the Path to the AppImage and persists any changes
    into the JSON settings file. On failure, raises RuntimeError.
    """
    _ensure_dirs()
    settings = _load_settings()

    downloaded = False

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

    needs_setup = app_path is None or not app_path.is_file() or not os.access(str(app_path), os.X_OK)

    # 3. If still missing, either download automatically (when selected) or prompt only for selection
    if needs_setup:
        if download_selected:
            try:
                url, ver = _github_latest_appimage()
            except Exception as e:
                error_dialog(f"Failed to query latest Archipelago release: {e}")
                raise RuntimeError("Failed to query latest Archipelago release") from e
            try:
                download_appimage(
                    url, AP_APPIMAGE_DEFAULT, ver, download_messages=download_messages
                )
            except Exception as e:
                error_dialog(f"Archipelago download failed or was cancelled: {e}")
                raise RuntimeError("Archipelago download failed") from e
            app_path = AP_APPIMAGE_DEFAULT
            settings["AP_APPIMAGE"] = str(AP_APPIMAGE_DEFAULT)
            settings["AP_VERSION"] = ver
            settings["AP_SKIP_VERSION"] = ""
            _save_settings(settings)
            downloaded = True
        else:
            app_path = _prompt_select_existing_appimage(Path(os.path.expanduser("~")))
            settings["AP_APPIMAGE"] = str(app_path)
            # No version information when manually selected.
            _save_settings(settings)

    if app_path is None or not app_path.is_file() or not os.access(str(app_path), os.X_OK):
        error_dialog("Archipelago AppImage was not configured correctly.")
        raise RuntimeError("Archipelago AppImage not configured")

    # 4. Auto-update if applicable
    app_path, updated = maybe_update_appimage(
        settings, app_path, download_messages=download_messages
    )
    downloaded = downloaded or updated

    # 5. Create a desktop shortcut only when a download occurred
    if downloaded:
        _create_desktop_shortcut(
            settings,
            "Archipelago",
            app_path,
            "AP_DESKTOP_SHORTCUT",
            enabled=create_shortcut,
        )

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
