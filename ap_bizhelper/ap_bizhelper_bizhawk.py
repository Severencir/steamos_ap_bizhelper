#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import importlib.resources as resources
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

from .ap_bizhelper_ap import download_with_progress

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
    import zipfile
    import tempfile

    _ensure_dirs()

    preserved_config = None
    try:
        preserved_config = next(BIZHAWK_WIN_DIR.rglob("config.ini"))
        if not preserved_config.is_file():
            preserved_config = None
    except StopIteration:
        preserved_config = None
    if preserved_config is not None:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ini") as cfg_tmp:
            shutil.copy2(preserved_config, cfg_tmp.name)
            preserved_config = Path(cfg_tmp.name)

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
    _stage_bizhawk_config(exe, preserved_config)
    return exe


def _stage_bizhawk_config(exe: Path, preserved_config: Optional[Path]) -> None:
    """Copy a default BizHawk config alongside ``exe`` if one is absent."""

    target_cfg = exe.parent / "config.ini"
    if target_cfg.exists():
        if preserved_config is not None and preserved_config.exists():
            preserved_config.unlink()
        return

    source_cfg: Optional[Path] = None
    if preserved_config is not None and preserved_config.is_file():
        source_cfg = preserved_config
    else:
        candidate = Path(__file__).with_name("config.ini")
        if candidate.is_file():
            source_cfg = candidate

    if source_cfg is None:
        return

    try:
        shutil.copy2(source_cfg, target_cfg)
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


def select_bizhawk_exe(initial: Optional[Path] = None) -> Optional[Path]:
    if not _has_zenity():
        return None
    args = [
        "--file-selection",
        "--title=Select EmuHawk.exe",
        "--file-filter=*.exe",
    ]
    if initial is not None:
        args.append(f"--filename={initial}")
    code, out = _run_zenity(args)
    if code != 0 or not out:
        return None
    p = Path(out)
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

    The runner is copied both to the managed data directory (for archival) and
    into the BizHawk installation directory so that any launch shortcuts can
    invoke it directly with the same arguments Archipelago provides.
    """

    _ensure_dirs()
    try:
        runner_resource = resources.files(__package__).joinpath("run_bizhawk_proton.py")
    except (ModuleNotFoundError, AttributeError):
        runner_resource = None

    bizhawk_runner = bizhawk_exe.parent / "run_bizhawk_proton.py"
    fallback_runner = BIZHAWK_RUNNER

    if runner_resource is None:
        error_dialog("BizHawk runner helper (run_bizhawk_proton.py) is missing.")
        return bizhawk_runner

    staged_any = False
    with resources.as_file(runner_resource) as source_runner:
        if not source_runner.is_file():
            error_dialog("BizHawk runner helper (run_bizhawk_proton.py) is missing.")
            return bizhawk_runner

        staged_any = _stage_runner(fallback_runner, source_runner) or staged_any
        staged_any = _stage_runner(bizhawk_runner, source_runner) or staged_any

    if not staged_any:
        error_dialog("Failed to stage BizHawk runner helper (run_bizhawk_proton.py).")

    runner = bizhawk_runner if bizhawk_runner.is_file() else fallback_runner

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

    if not _has_zenity():
        return bizhawk_exe, False

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
        return bizhawk_exe, False

    if choice == "Skip this version":
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
) -> Optional[Tuple[Path, Path, bool]]:
    """
    Ensure BizHawk (Windows) and Proton are configured and runnable.

    On success, returns the Path to the BizHawk runner script (BIZHAWK_RUNNER),
    the EmuHawk.exe path, and a flag indicating whether any downloads occurred.
    
    On failure or user cancellation, returns None.
    """
    _ensure_dirs()
    settings = _load_settings()
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
        # Settings may have changed; reload
        settings = _load_settings()
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
        if not download_selected and not _has_zenity():
            error_dialog("BizHawk is not configured and zenity is not available for setup.")
            return None

        if download_selected:
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
            downloaded = True
            if download_messages is not None:
                download_messages.append(f"Downloaded BizHawk {ver}")
        else:
            code, _ = _run_zenity(
                [
                    "--question",
                    "--title=BizHawk (Proton) setup",
                    "--text=BizHawk (with Proton) was not selected for download.\n\nSelect an existing EmuHawk.exe to continue?",
                    "--ok-label=Select EmuHawk.exe",
                    "--cancel-label=Cancel",
                ]
            )
            if code != 0:
                return None

            exe = select_bizhawk_exe(Path(os.path.expanduser("~")))
            if not exe:
                return None
            settings = _load_settings()
            settings["BIZHAWK_EXE"] = str(exe)
            settings["BIZHAWK_VERSION"] = ""
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

    # Check for updates (in case user had an older version)
    exe, updated = maybe_update_bizhawk(
        settings, exe, download_messages=download_messages
    )
    downloaded = downloaded or updated

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
