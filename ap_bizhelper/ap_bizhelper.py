#!/usr/bin/env python3

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, Optional, Set, Tuple

from .ap_bizhelper_ap import (
    AP_APPIMAGE_DEFAULT,
    _ensure_qt_app,
    _has_qt_dialogs,
    _has_zenity,
    _run_zenity,
    _select_file_dialog,
    _qt_question_dialog,
    _zenity_error_dialog,
    ensure_appimage,
    error_dialog,
    info_dialog,
)
from .ap_bizhelper_bizhawk import ensure_bizhawk_and_proton
from .ap_bizhelper_config import (
    get_ext_behavior,
    load_settings,
    save_settings,
    set_ext_behavior,
)
from .ap_bizhelper_worlds import ensure_apworld_for_patch


def _is_running_under_steam() -> bool:
    """Return ``True`` when launched by Steam (Steam overlay/controller env set)."""

    return bool(os.environ.get("SteamGameId"))


def _shortcut_vdf_paths() -> Iterable[Path]:
    """Yield plausible ``shortcuts.vdf`` locations for the current user."""

    home = Path.home()
    bases = [home / ".steam/steam", home / ".local/share/Steam"]
    for base in bases:
        userdata = base / "userdata"
        if not userdata.is_dir():
            continue
        for userdir in userdata.iterdir():
            config = userdir / "config/shortcuts.vdf"
            if config.is_file():
                yield config


def _read_cstring(data: bytes, idx: int) -> Tuple[str, int]:
    end = data.find(b"\x00", idx)
    if end == -1:
        raise ValueError("Unterminated string in shortcuts.vdf")
    return data[idx:end].decode("utf-8", errors="ignore"), end + 1


def _parse_binary_kv(data: bytes, idx: int = 0) -> Tuple[dict, int]:
    """Parse a binary VDF-style key/value object starting at ``idx``."""

    obj = {}
    while idx < len(data):
        value_type = data[idx]
        idx += 1

        if value_type == 0x08:
            return obj, idx

        key, idx = _read_cstring(data, idx)

        if value_type == 0x00:  # nested object
            value, idx = _parse_binary_kv(data, idx)
        elif value_type == 0x01:  # string
            value, idx = _read_cstring(data, idx)
        elif value_type == 0x02:  # int32
            if idx + 4 > len(data):
                raise ValueError("Unexpected end of int32 in shortcuts.vdf")
            value = int.from_bytes(data[idx : idx + 4], "little", signed=True)
            idx += 4
        elif value_type == 0x07:  # uint64
            if idx + 8 > len(data):
                raise ValueError("Unexpected end of int64 in shortcuts.vdf")
            value = int.from_bytes(data[idx : idx + 8], "little", signed=False)
            idx += 8
        else:
            raise ValueError(f"Unsupported VDF field type: {value_type}")

        obj[key] = value

    raise ValueError("Unexpected end of shortcuts.vdf")


def _load_shortcuts(path: Path) -> list[dict]:
    """Return a list of shortcut dicts from a binary ``shortcuts.vdf`` file."""

    try:
        data = path.read_bytes()
        root, _ = _parse_binary_kv(data)
    except Exception:
        return []

    shortcuts_obj = root.get("shortcuts")
    if not isinstance(shortcuts_obj, dict):
        return []

    shortcuts: list[dict] = []
    for entry in shortcuts_obj.values():
        if isinstance(entry, dict):
            shortcuts.append(entry)

    return shortcuts


def _normalize_exe_field(value: str | None) -> Optional[Path]:
    if not value:
        return None
    cleaned = value.strip().strip("\"")
    first = cleaned.split(" ", 1)[0]
    try:
        return Path(first).expanduser().resolve()
    except Exception:
        return None


def _find_shortcut_appid(target: Path) -> Optional[int]:
    target = target.resolve()
    for shortcuts_path in _shortcut_vdf_paths():
        for entry in _load_shortcuts(shortcuts_path):
            exe_value = entry.get("exe") or entry.get("Exe")
            exe_path = _normalize_exe_field(str(exe_value)) if exe_value is not None else None
            if exe_path is None:
                continue
            if exe_path == target or str(target) in str(exe_path):
                appid = entry.get("appid") or entry.get("AppId")
                if isinstance(appid, int):
                    return appid

            app_name = str(entry.get("appname") or entry.get("AppName") or "")
            if app_name.lower().startswith("ap-bizhelper") or "bizhelper" in app_name.lower():
                appid = entry.get("appid") or entry.get("AppId")
                if isinstance(appid, int):
                    return appid
    return None


def _maybe_relaunch_via_steam(argv: list[str]) -> None:
    """If not under Steam, try to relaunch through the matching shortcut."""

    if _is_running_under_steam():
        return

    steam_appid = os.environ.get("AP_BIZHELPER_STEAM_APPID")
    if steam_appid and steam_appid.isdigit():
        appid = int(steam_appid)
    else:
        appid = _find_shortcut_appid(Path(argv[0]))

    if appid is None:
        return

    steam_binary = shutil.which("steam") or shutil.which("/usr/bin/steam")
    if not steam_binary:
        return

    relaunch_log = Path.home() / ".local/share/ap-bizhelper/steam-relaunch.log"
    relaunch_log.parent.mkdir(parents=True, exist_ok=True)
    def _log_line(message: str) -> None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with relaunch_log.open("a", encoding="utf-8") as log_file:
            log_file.write(f"[{timestamp}] {message}\n")

    # "steam -applaunch <appid>" does not work for non-Steam shortcuts; use the
    # universal steam://rungameid URL so Steam can relaunch both workshop apps
    # and custom shortcuts the user has added manually.
    launch_cmd = [steam_binary, f"steam://rungameid/{appid}"]
    if len(argv) > 1:
        launch_cmd.append("--")
        launch_cmd.extend(argv[1:])

    try:
        print(
            f"[ap-bizhelper] Relaunching via Steam for overlay/controller support (appid {appid})."
        )
        run_timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log_size_before = relaunch_log.stat().st_size if relaunch_log.exists() else 0
        with relaunch_log.open("a", encoding="utf-8") as log_file:
            log_file.write(
                f"[{run_timestamp}] Running: {' '.join(launch_cmd)} (cwd={os.getcwd()})\n"
            )
            log_file.flush()
            size_after_header = log_file.tell()
            proc = subprocess.run(
                launch_cmd,
                stdout=log_file,
                stderr=log_file,
                env=os.environ,
                check=False,
            )

        log_size_after = relaunch_log.stat().st_size
        steam_output_bytes = log_size_after - size_after_header
        if proc.returncode == 0 and steam_output_bytes <= 0:
            msg = (
                "Steam relaunch command produced no output; assuming relaunch failed. "
                f"See {relaunch_log} for details."
            )
            _log_line(msg)
            print(f"[ap-bizhelper] {msg}", file=sys.stderr)
            _zenity_error_dialog(msg)
            return

        if proc.returncode == 0:
            _log_line("Steam relaunch command reported exit code 0; exiting current process.")
            print(
                f"[ap-bizhelper] Steam relaunch command completed (see {relaunch_log})."
                " Exiting so Steam can launch the managed shortcut."
            )
            sys.exit(0)
        else:
            msg = (
                "Steam relaunch command exited with code "
                f"{proc.returncode}. See {relaunch_log} for details."
            )
            _log_line(msg)
            print(f"[ap-bizhelper] {msg}", file=sys.stderr)
            _zenity_error_dialog(msg)
    except Exception as exc:
        # If Steam launch fails for any reason, continue normal flow but inform the user so
        # the "relaunching" message is not misleading.
        _log_line(f"Failed to relaunch via Steam: {exc}")
        print(f"[ap-bizhelper] Failed to relaunch via Steam: {exc}", file=sys.stderr)
        _zenity_error_dialog(
            "Steam relaunch failed. Please check the console output "
            f"or log at {relaunch_log} to troubleshoot."
        )


def _select_patch_file() -> Path:
    patch = _select_file_dialog(
        title="Select Archipelago patch file",
        initial=Path.home(),
    )
    if patch is None:
        raise RuntimeError("User cancelled patch selection.")

    if not patch.is_file():
        raise RuntimeError("Selected patch file does not exist.")

    return patch


def _needs_archipelago_download(settings: dict) -> bool:
    app_path_str = str(settings.get("AP_APPIMAGE", "") or "")
    app_path = Path(app_path_str) if app_path_str else None

    if app_path and app_path.is_file() and os.access(str(app_path), os.X_OK):
        return False

    if AP_APPIMAGE_DEFAULT.is_file() and os.access(str(AP_APPIMAGE_DEFAULT), os.X_OK):
        return False

    return True


def _needs_bizhawk_download(settings: dict) -> bool:
    exe_str = str(settings.get("BIZHAWK_EXE", "") or "")
    runner_str = str(settings.get("BIZHAWK_RUNNER", "") or "")
    proton_str = str(settings.get("PROTON_BIN", "") or "")

    exe = Path(exe_str) if exe_str else None
    runner = Path(runner_str) if runner_str else None
    proton_bin = Path(proton_str) if proton_str else None

    return not (
        exe and exe.is_file() and runner and runner.is_file() and proton_bin and proton_bin.is_file()
    )


def _prompt_setup_choices(
    *,
    allow_archipelago_skip: bool,
    show_archipelago: bool,
    show_bizhawk: bool,
) -> Tuple[bool, bool, bool]:
    if not any((show_archipelago, show_bizhawk)):
        return False, False, False

    if _has_qt_dialogs():
        from PySide6 import QtWidgets

        _ensure_qt_app()
        dialog = QtWidgets.QDialog()
        dialog.setWindowTitle("Download setup")
        layout = QtWidgets.QVBoxLayout(dialog)
        label = QtWidgets.QLabel("Select which components to download and configure.")
        layout.addWidget(label)

        arch_box = None
        if show_archipelago:
            arch_box = QtWidgets.QCheckBox("Archipelago")
            arch_box.setChecked(True)
            layout.addWidget(arch_box)

        bizhawk_box = None
        if show_bizhawk:
            bizhawk_box = QtWidgets.QCheckBox("BizHawk (with Proton)")
            bizhawk_box.setChecked(True)
            layout.addWidget(bizhawk_box)

        shortcut_box = None
        if show_archipelago or show_bizhawk:
            shortcut_box = QtWidgets.QCheckBox(
                "Create Desktop shortcuts (Archipelago & BizHawk)"
            )
            shortcut_box.setChecked(True)
            layout.addWidget(shortcut_box)

        buttons = QtWidgets.QDialogButtonBox()
        download_btn = QtWidgets.QPushButton("Download")
        cancel_btn = QtWidgets.QPushButton("Cancel")
        buttons.addButton(download_btn, QtWidgets.QDialogButtonBox.AcceptRole)
        buttons.addButton(cancel_btn, QtWidgets.QDialogButtonBox.RejectRole)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QtWidgets.QDialog.Accepted:
            raise RuntimeError("User cancelled setup selection.")

        arch = arch_box.isChecked() if arch_box is not None else False
        bizhawk = bizhawk_box.isChecked() if bizhawk_box is not None else False
        shortcuts = shortcut_box.isChecked() if shortcut_box is not None else False

        return arch, bizhawk, shortcuts

    if not _has_zenity():
        # Fall back to enabling available options when zenity is unavailable.
        shortcuts = show_archipelago or show_bizhawk
        return show_archipelago, show_bizhawk, shortcuts

    while True:
        args = [
            "--list",
            "--checklist",
            "--title=Download setup",
            "--text=Select which components to download and configure.",
            "--column=Install",
            "--column=Component",
        ]

        if show_archipelago:
            args.extend(["TRUE", "Archipelago"])
        if show_bizhawk:
            args.extend(["TRUE", "BizHawk (with Proton)"])
        if show_archipelago or show_bizhawk:
            args.extend(["TRUE", "Create Desktop shortcuts (Archipelago & BizHawk)"])

        args.extend(["--ok-label=Download", "--cancel-label=Cancel", "--height=300"])

        code, out = _run_zenity(args)

        if code != 0:
            raise RuntimeError("User cancelled setup selection.")

        selections = [s.strip() for s in out.split("|") if s.strip()]
        arch = "Archipelago" in selections
        bizhawk = "BizHawk (with Proton)" in selections
        shortcuts = "Create Desktop shortcuts (Archipelago & BizHawk)" in selections

        return arch, bizhawk, shortcuts


def _ensure_apworld_for_extension(ext: str) -> None:
    ext = ext.strip().lower()
    if not ext:
        return

    # Only care about "new" extensions (no behavior stored yet)
    behavior = get_ext_behavior(ext)
    if behavior:
        return

    if not (_has_qt_dialogs() or _has_zenity()):
        print(
            f"[ap-bizhelper] No dialog backend available; skipping APWorld prompt for .{ext}."
        )
        return

    worlds_dir = Path.home() / ".local/share/Archipelago/worlds"
    text = (
        f"This looks like a new Archipelago patch extension (.{ext}).\n\n"
        "If this game requires an external .apworld file and it isn't already installed, "
        f"you can select it now to copy into:\n{worlds_dir}\n\n"
        "Do you want to select a .apworld file for this extension now?"
    )

    apworld_path: Optional[Path]
    if _has_qt_dialogs():
        choice = _qt_question_dialog(
            title=f"APWorld for .{ext}",
            text=text,
            ok_label="Select .apworld",
            cancel_label="Skip",
        )
        if choice != "ok":
            print(f"[ap-bizhelper] User skipped APWorld selection for .{ext}.")
            return

        apworld_path = _select_file_dialog(
            title=f"Select .apworld file for .{ext}",
            initial=Path.home(),
            file_filter="*.apworld",
        )
    else:
        _zenity_error_dialog(
            "PySide6 is required to select .apworld files.\n"
            "Install PySide6 and rerun the setup to pick a .apworld file."
        )
        return

    if apworld_path is None:
        return

    if apworld_path.is_file():
        try:
            worlds_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(apworld_path, worlds_dir / apworld_path.name)
            info_dialog(f"Copied {apworld_path.name} to:\n{worlds_dir}")
        except Exception as exc:  # pragma: no cover - filesystem edge cases
            error_dialog(f"Failed to copy {apworld_path.name}: {exc}")
    else:
        error_dialog("Selected .apworld file does not exist.")
def _list_bizhawk_pids() -> Set[int]:
    try:
        proc = subprocess.run(
            ["pgrep", "-f", "EmuHawk.exe"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except FileNotFoundError:
        return set()

    pids = set()
    for line in proc.stdout.splitlines():
        try:
            pids.add(int(line.strip()))
        except ValueError:
            continue
    return pids


def _is_archipelago_running() -> bool:
    try:
        proc = subprocess.run(
            ["pgrep", "-f", "Archipelago"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except FileNotFoundError:
        return False

    return any(line.strip() for line in proc.stdout.splitlines())


def _is_appimage_mounted(appimage: Optional[Path]) -> bool:
    if appimage is None:
        return False

    try:
        with open("/proc/mounts", "r", encoding="utf-8") as mounts:
            for line in mounts:
                if str(appimage) in line:
                    return True
    except Exception:
        pass

    try:
        for candidate in Path("/tmp").glob(".mount_*"):
            if candidate.is_dir() and appimage.stem.lower() in candidate.name.lower():
                return True
    except Exception:
        pass

    return False


def _wait_for_archipelago_ready(appimage: Path, *, timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_archipelago_running() or _is_appimage_mounted(appimage):
            return True
        time.sleep(1)

    print(
        "[ap-bizhelper] Archipelago did not appear to start within the timeout; "
        "skipping BizHawk auto-launch."
    )
    return False


def _find_matching_rom(patch: Path) -> Optional[Path]:
    if patch.suffix.lower() == ".sfc" and patch.is_file():
        return patch

    base = patch.with_suffix("")

    if base != patch and base.is_file():
        return base

    pattern = f"{base.name}.*"
    candidates = []
    for cand in patch.parent.glob(pattern):
        if cand == patch or cand == base:
            continue
        if cand.is_file():
            candidates.append(cand)

    candidates.sort()
    return candidates[0] if candidates else None


def _wait_for_rom(patch: Path, *, timeout: int = 60) -> Optional[Path]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rom = _find_matching_rom(patch)
        if rom:
            print(f"[ap-bizhelper] ROM detected: {rom}")
            return rom
        time.sleep(1)
    print("[ap-bizhelper] Timed out waiting for ROM; not launching BizHawk.")
    return None


def _launch_bizhawk(runner: Path, rom: Path) -> None:
    print(f"[ap-bizhelper] Launching BizHawk runner: {runner} {rom}")
    try:
        subprocess.Popen([str(runner), str(rom)])
    except Exception as exc:  # pragma: no cover - safety net for runtime environments
        error_dialog(f"Failed to launch BizHawk runner: {exc}")


def _detect_new_bizhawk(baseline: Iterable[int], *, timeout: int = 10) -> bool:
    baseline_set = set(baseline)
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = _list_bizhawk_pids()
        if current.difference(baseline_set):
            return True
        time.sleep(1)
    return False


def _handle_bizhawk_for_patch(patch: Path, runner: Optional[Path], baseline_pids: Iterable[int]) -> None:
    if runner is None or not runner.is_file():
        print("[ap-bizhelper] BizHawk runner not configured or not executable; skipping auto-launch.")
        return

    rom = _wait_for_rom(patch)
    if rom is None:
        return

    ext = rom.suffix.lower().lstrip(".")

    if ext != "sfc":
        print(
            "[ap-bizhelper] Non-SFC ROM detected; skipping BizHawk auto-launch and "
            "deferring to Archipelago."
        )
        return

    behavior = get_ext_behavior(ext)
    print(f"[ap-bizhelper] Patch: {patch}")
    print(f"[ap-bizhelper] ROM: {rom}")
    print(f"[ap-bizhelper] Detected extension: .{ext}")
    print(f"[ap-bizhelper] Saved behavior for .{ext}: {behavior or '<none>'}")

    if behavior == "auto":
        print("[ap-bizhelper] Behavior 'auto': not launching BizHawk; assuming AP/user handles it.")
        return
    if behavior == "fallback":
        print("[ap-bizhelper] Behavior 'fallback': launching BizHawk via Proton.")
        _launch_bizhawk(runner, rom)
        return
    if behavior not in (None, ""):
        print(f"[ap-bizhelper] Unknown behavior '{behavior}' for .{ext}; doing nothing for safety.")
        return

    print("[ap-bizhelper] No behavior stored yet; waiting briefly to see if BizHawk appears on its own.")
    if _detect_new_bizhawk(baseline_pids):
        print(f"[ap-bizhelper] Detected new BizHawk instance; recording .{ext} as 'auto'.")
        set_ext_behavior(ext, "auto")
        return

    print(
        "[ap-bizhelper] No BizHawk detected after fallback timeout; "
        f"switching .{ext} to 'fallback' and launching runner."
    )
    set_ext_behavior(ext, "fallback")
    _launch_bizhawk(runner, rom)


def _run_prereqs(*, allow_archipelago_skip: bool = False) -> Tuple[Optional[Path], Optional[Path]]:
    settings = load_settings()
    need_arch = _needs_archipelago_download(settings)
    need_bizhawk = _needs_bizhawk_download(settings)

    if any((need_arch, need_bizhawk)):
        arch, bizhawk, shortcuts = _prompt_setup_choices(
            allow_archipelago_skip=allow_archipelago_skip,
            show_archipelago=need_arch,
            show_bizhawk=need_bizhawk,
        )
    else:
        arch = False
        bizhawk = False
        shortcuts = False

    download_messages: list[str] = []

    appimage: Optional[Path] = None
    runner: Optional[Path] = None

    if arch or not allow_archipelago_skip:
        appimage = ensure_appimage(
            download_selected=arch,
            create_shortcut=shortcuts,
            download_messages=download_messages,
        )

    bizhawk_result: Optional[Tuple[Path, Path, bool]] = None
    bizhawk_result = ensure_bizhawk_and_proton(
        download_selected=bizhawk,
        create_shortcut=shortcuts,
        download_messages=download_messages,
    )
    if bizhawk:
        if bizhawk_result is None:
            raise RuntimeError("BizHawk setup was cancelled or failed.")
        runner, _, _ = bizhawk_result
    elif bizhawk_result is not None:
        runner, _, _ = bizhawk_result

    if download_messages:
        message = "Completed downloads:\n- " + "\n- ".join(download_messages)
        info_dialog(message)

    return appimage, runner


def _run_full_flow() -> int:
    try:
        appimage, runner = _run_prereqs()
    except RuntimeError as exc:
        error_dialog(str(exc))
        return 1

    if appimage is None:
        error_dialog("Archipelago was not selected for download and is required to continue.")
        return 1

    baseline_pids = _list_bizhawk_pids()

    try:
        patch = _select_patch_file()
    except RuntimeError as exc:
        error_dialog(str(exc))
        return 1

    ensure_apworld_for_patch(patch)

    print(f"[ap-bizhelper] Launching Archipelago with patch: {patch}")
    try:
        subprocess.Popen([str(appimage), str(patch)])
    except Exception as exc:  # pragma: no cover - runtime launcher safety net
        error_dialog(f"Failed to launch Archipelago: {exc}")
        return 1

    if _wait_for_archipelago_ready(appimage):
        _handle_bizhawk_for_patch(patch, runner, baseline_pids)
    return 0


def main(argv: list[str]) -> int:
    _maybe_relaunch_via_steam(argv)

    if len(argv) >= 2 and argv[1] == "ensure":
        try:
            _run_prereqs(allow_archipelago_skip=True)
        except RuntimeError:
            return 1
        return 0

    if len(argv) >= 2:
        print("Usage: ap_bizhelper.py [ensure]", file=sys.stderr)
        return 1

    return _run_full_flow()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
