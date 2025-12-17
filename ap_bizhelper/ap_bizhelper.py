#!/usr/bin/env python3

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from urllib.parse import quote, unquote, urlparse
from pathlib import Path
from typing import Optional, Set, Tuple

from .ap_bizhelper_ap import AP_APPIMAGE_DEFAULT, ensure_appimage
from .dialogs import (
    enable_dialog_gamepad as _enable_dialog_gamepad,
    ensure_qt_app as _ensure_qt_app,
    ensure_qt_available as _ensure_qt_available,
    question_dialog as _qt_question_dialog,
    select_file_dialog as _select_file_dialog,
    error_dialog,
    info_dialog,
)
from .ap_bizhelper_bizhawk import ensure_bizhawk_and_proton
from .ap_bizhelper_config import (
    get_all_associations,
    get_association_mode,
    get_ext_behavior,
    get_ext_association,
    load_settings,
    save_settings,
    set_association_mode,
    set_ext_behavior,
    set_ext_association,
)
from .dialog_shim import prepare_dialog_shim_env
from .logging_utils import RUNNER_LOG_ENV, get_app_logger
from .ap_bizhelper_worlds import ensure_apworld_for_patch


APP_LOGGER = get_app_logger()


def _steam_game_id_from_env() -> Optional[str]:
    """Return the Steam game id from common environment keys, if available."""

    for key in ("SteamGameId", "SteamGameID", "SteamAppId", "SteamAppID"):
        value = os.environ.get(key)
        if value and str(value).isdigit():
            return str(value)
    return None


def _is_running_under_steam() -> bool:
    """Return ``True`` when launched by Steam (Steam overlay/controller env set)."""

    return bool(_steam_game_id_from_env())


def _capture_steam_appid_if_present(settings: dict) -> None:
    """Persist ``SteamGameId`` into settings when available."""

    with APP_LOGGER.context("_capture_steam_appid_if_present"):
        steam_game_id = _steam_game_id_from_env()
        if not steam_game_id:
            return

        cached_appid = str(settings.get("STEAM_APPID") or "")
        if cached_appid == steam_game_id:
            return

        settings["STEAM_APPID"] = steam_game_id
        save_settings(settings)
        APP_LOGGER.log(
            f"Detected Steam launch; cached app id {steam_game_id} for future relaunches.",
            include_context=True,
            mirror_console=True,
        )


def _get_known_steam_appid(settings: dict) -> Optional[str]:
    """Return the active or cached Steam app id when available."""

    steam_game_id = _steam_game_id_from_env()
    if steam_game_id:
        return steam_game_id

    cached_appid = str(settings.get("STEAM_APPID") or "")
    if cached_appid.isdigit():
        return cached_appid

    return None


def _maybe_relaunch_via_steam(argv: list[str], settings: dict) -> None:
    """If not under Steam, try to relaunch through the matching shortcut."""

    with APP_LOGGER.context("_maybe_relaunch_via_steam"):
        if _is_running_under_steam():
            return

        steam_appid_env = os.environ.get("AP_BIZHELPER_STEAM_APPID")
        cached_appid = str(settings.get("STEAM_APPID") or "")

        appid: Optional[int]
        appid_source: str
        if steam_appid_env and steam_appid_env.isdigit():
            appid = int(steam_appid_env)
            appid_source = "AP_BIZHELPER_STEAM_APPID environment variable"
        elif cached_appid.isdigit():
            appid = int(cached_appid)
            appid_source = "cached Steam launch"
        else:
            message = (
                "Steam relaunch needs the recorded app id. "
                "Launch ap-bizhelper from your Steam library once so it can capture it, "
                "then start it outside Steam again."
            )
            APP_LOGGER.log(
                message,
                level="ERROR",
                location="steam-relaunch",
                include_context=True,
                mirror_console=True,
                stream="stderr",
            )
            error_dialog(message)
            sys.exit(1)

        if str(settings.get("STEAM_APPID")) != str(appid):
            settings["STEAM_APPID"] = str(appid)
            save_settings(settings)
            APP_LOGGER.log(
                f"Stored Steam app id {appid} ({appid_source}) for future relaunches.",
                location="steam-relaunch",
                include_context=True,
                mirror_console=True,
            )

        steam_binary = shutil.which("steam") or shutil.which("/usr/bin/steam")
        if not steam_binary:
            APP_LOGGER.log(
                "Steam binary missing; continuing without relaunch support.",
                level="WARNING",
                location="steam-relaunch",
                include_context=True,
            )
            return

        APP_LOGGER.log(
            f"Preparing Steam relaunch attempt (appid={appid}, source={appid_source}).",
            location="steam-relaunch",
            include_context=True,
        )

        steam_uri = f"steam://rungameid/{appid}"
        if len(argv) > 1:
            encoded_args = quote(" ".join(argv[1:]))
            steam_uri = f"{steam_uri}//{encoded_args}"

        xdg_open = shutil.which("xdg-open")
        launch_attempts: list[tuple[str, list[str]]] = []
        if xdg_open:
            launch_attempts.append(("xdg-open", [xdg_open, steam_uri]))
        launch_attempts.append(("steam", [steam_binary, steam_uri]))

        try:
            APP_LOGGER.log(
                f"Relaunching via Steam for overlay/controller support (appid {appid}).",
                location="steam-relaunch",
                include_context=True,
                mirror_console=True,
            )
            for launcher_name, launch_cmd in launch_attempts:
                APP_LOGGER.log(
                    f"Running via {launcher_name}: {' '.join(launch_cmd)} (cwd={os.getcwd()})",
                    location="steam-relaunch",
                    include_context=True,
                )
                proc = subprocess.run(
                    launch_cmd,
                    capture_output=True,
                    text=True,
                    env=os.environ,
                    check=False,
                )

                if proc.stdout:
                    APP_LOGGER.log_lines(
                        f"{launcher_name} stdout", proc.stdout.splitlines(), location="steam-relaunch"
                    )
                if proc.stderr:
                    APP_LOGGER.log_lines(
                        f"{launcher_name} stderr", proc.stderr.splitlines(), location="steam-relaunch"
                    )

                if proc.returncode == 0:
                    if len(argv) > 1:
                        settings["PENDING_RELAUNCH_ARGS"] = argv[1:]
                        settings["USE_CACHED_RELAUNCH_ARGS"] = True
                        save_settings(settings)
                    APP_LOGGER.log(
                        f"{launcher_name} command reported exit code 0; exiting current process.",
                        location="steam-relaunch",
                        include_context=True,
                        mirror_console=True,
                    )
                    sys.exit(0)

                APP_LOGGER.log(
                    f"{launcher_name} command exited with code {proc.returncode}; trying next fallback",
                    level="WARNING",
                    location="steam-relaunch",
                    include_context=True,
                )

            msg = "Steam relaunch command failed across all launchers."
            APP_LOGGER.log(
                msg,
                level="ERROR",
                location="steam-relaunch",
                include_context=True,
                mirror_console=True,
                stream="stderr",
            )
            error_dialog(msg)
        except Exception as exc:
            APP_LOGGER.log(
                f"Failed to relaunch via Steam: {exc}",
                level="ERROR",
                location="steam-relaunch",
                include_context=True,
                mirror_console=True,
                stream="stderr",
            )
            error_dialog(
                "Steam relaunch failed. Please check the console output and recent log entries to troubleshoot."
            )


def _clear_relaunch_cache(settings: dict, force_save: bool = False) -> None:
    """Remove any stored relaunch arguments from the settings file."""

    cache_removed = force_save
    for cache_key in ("PENDING_RELAUNCH_ARGS", "USE_CACHED_RELAUNCH_ARGS"):
        if cache_key in settings:
            settings.pop(cache_key, None)
            cache_removed = True

    if cache_removed:
        save_settings(settings)


def _select_patch_file() -> Path:
    patch = _select_file_dialog(
        title="Select Archipelago patch file",
        initial=Path.home(),
        dialog_key="patch",
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

    from PySide6 import QtCore, QtWidgets

    _ensure_qt_app()
    dialog = QtWidgets.QDialog()
    dialog.setWindowTitle("Download setup")
    dialog.setWindowFlag(QtCore.Qt.WindowCloseButtonHint, False)
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

    button_row = QtWidgets.QHBoxLayout()
    button_row.addStretch()
    download_btn = QtWidgets.QPushButton("Download")
    download_btn.setDefault(True)
    button_row.addWidget(download_btn)
    cancel_btn = QtWidgets.QPushButton("Cancel")
    button_row.addWidget(cancel_btn)
    button_row.addStretch()
    layout.addLayout(button_row)

    download_btn.clicked.connect(dialog.accept)
    cancel_btn.clicked.connect(dialog.reject)

    _enable_dialog_gamepad(
        dialog, affirmative=download_btn, negative=cancel_btn, default=download_btn
    )

    if dialog.exec() != QtWidgets.QDialog.Accepted:
        raise RuntimeError("User cancelled setup selection.")

    arch = arch_box.isChecked() if arch_box is not None else False
    bizhawk = bizhawk_box.isChecked() if bizhawk_box is not None else False
    shortcuts = shortcut_box.isChecked() if shortcut_box is not None else False

    return arch, bizhawk, shortcuts


def _ensure_apworld_for_extension(ext: str) -> None:
    ext = ext.strip().lower()
    if not ext:
        return

    # Only care about "new" extensions (no behavior stored yet)
    behavior = get_ext_behavior(ext)
    if behavior:
        return

    worlds_dir = Path.home() / ".local/share/Archipelago/worlds"
    text = (
        f"This looks like a new Archipelago patch extension (.{ext}).\n\n"
        "If this game requires an external .apworld file and it isn't already installed, "
        f"you can select it now to copy into:\n{worlds_dir}\n\n"
        "Do you want to select a .apworld file for this extension now?"
    )

    apworld_path: Optional[Path]
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
        dialog_key="apworld",
    )

    if apworld_path is None:
        return

    if apworld_path.is_file():
        try:
            worlds_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(apworld_path, worlds_dir / apworld_path.name)
            APP_LOGGER.log(
                f"Copied {apworld_path.name} to {worlds_dir}",
                include_context=True,
                location="apworld-copy",
            )
        except Exception as exc:  # pragma: no cover - filesystem edge cases
            error_dialog(f"Failed to copy {apworld_path.name}: {exc}")
    else:
        error_dialog("Selected .apworld file does not exist.")


def _association_exec_command() -> str:
    """Return the Exec command for desktop entries."""

    appimage_env = os.environ.get("APPIMAGE")
    if appimage_env:
        appimage_path = Path(appimage_env)
        if appimage_path.is_file():
            return f"{shlex.quote(str(appimage_path))} %u"

    try:
        candidate = Path(sys.argv[0]).resolve()
        if candidate.is_file():
            return f"{shlex.quote(str(candidate))} %u"
    except Exception:
        pass

    return f"{shlex.quote(sys.executable)} -m ap_bizhelper %u"


def _registered_association_exts() -> list[str]:
    if get_association_mode() == "disabled":
        return []

    return sorted(
        ext
        for ext, state in get_all_associations().items()
        if str(state).lower() == "registered"
    )


def _apply_association_files(associated_exts: list[str]) -> None:
    """Write or remove desktop/mime association files based on ``associated_exts``."""

    if sys.platform != "linux":
        return

    applications_dir = Path.home() / ".local/share/applications"
    mime_packages_dir = Path.home() / ".local/share/mime/packages"
    desktop_path = applications_dir / "ap-bizhelper.desktop"

    # Remove stale xml files first
    if mime_packages_dir.is_dir():
        for xml_file in mime_packages_dir.glob("ap-bizhelper-*.xml"):
            ext = xml_file.stem.replace("ap-bizhelper-", "", 1)
            if ext not in associated_exts:
                try:
                    xml_file.unlink()
                except Exception:
                    pass

    if not associated_exts:
        if desktop_path.exists():
            try:
                desktop_path.unlink()
            except Exception:
                pass

        update_mime = shutil.which("update-mime-database")
        if update_mime:
            try:
                subprocess.run(
                    [update_mime, str(mime_packages_dir.parent)],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass

        update_desktop = shutil.which("update-desktop-database")
        if update_desktop:
            try:
                subprocess.run(
                    [update_desktop, str(applications_dir)],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass

        return

    mime_types: list[str] = []

    mime_packages_dir.mkdir(parents=True, exist_ok=True)
    for ext in associated_exts:
        mime_type = f"application/x-ap-bizhelper-{ext}"
        mime_types.append(mime_type)
        xml_path = mime_packages_dir / f"ap-bizhelper-{ext}.xml"
        content = (
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<mime-info xmlns=\"http://www.freedesktop.org/standards/shared-mime-info\">\n"
            f"  <mime-type type=\"{mime_type}\">\n"
            f"    <comment>Archipelago patch ({ext})</comment>\n"
            f"    <glob pattern=\"*.{ext}\"/>\n"
            "  </mime-type>\n"
            "</mime-info>\n"
        )
        try:
            xml_path.write_text(content, encoding="utf-8")
        except Exception:
            pass

    applications_dir.mkdir(parents=True, exist_ok=True)
    exec_cmd = _association_exec_command()
    desktop_content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=ap-bizhelper\n"
        "Comment=Launch Archipelago patches with ap-bizhelper\n"
        f"Exec={exec_cmd}\n"
        "Terminal=false\n"
        "Categories=Game;Utility;\n"
        f"MimeType={';'.join(mime_types)};\n"
    )

    try:
        desktop_path.write_text(desktop_content, encoding="utf-8")
        desktop_path.chmod(0o755)
    except Exception:
        pass

    update_mime = shutil.which("update-mime-database")
    if update_mime:
        try:
            subprocess.run(
                [update_mime, str(mime_packages_dir.parent)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    update_desktop = shutil.which("update-desktop-database")
    if update_desktop:
        try:
            subprocess.run(
                [update_desktop, str(applications_dir)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    xdg_mime = shutil.which("xdg-mime")
    if xdg_mime and desktop_path.exists():
        for mime_type in mime_types:
            try:
                subprocess.run(
                    [xdg_mime, "default", desktop_path.name, mime_type],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                continue


def _parse_patch_arg(arg: str) -> Path:
    """Return a filesystem path from a CLI argument or ``file://`` URI."""

    candidate: Path
    parsed = urlparse(arg)
    if parsed.scheme == "file":
        path_part = unquote(parsed.path)
        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            path_part = f"//{parsed.netloc}{parsed.path}"
        candidate = Path(path_part)
    else:
        candidate = Path(arg)

    candidate = candidate.expanduser()
    try:
        candidate = candidate.resolve()
    except Exception:
        pass

    if not candidate.is_file():
        raise RuntimeError(f"Patch file does not exist: {candidate}")

    return candidate


def _handle_extension_association(ext: str) -> None:
    ext = ext.strip().lower()
    if not ext:
        return

    association = get_ext_association(ext)
    mode = get_association_mode()

    if mode == "disabled" or association == "declined":
        return

    registered_exts = _registered_association_exts()
    if association == "registered":
        if ext not in registered_exts:
            registered_exts.append(ext)
        _apply_association_files(registered_exts)
        return

    if mode == "enabled":
        set_ext_association(ext, "registered")
        if ext not in registered_exts:
            registered_exts.append(ext)
        _apply_association_files(registered_exts)
        return

    if mode != "prompt":
        return

    prompt_text = (
        f"ap-bizhelper can handle .{ext} files automatically.\n\n"
        "Do you want to register ap-bizhelper as the default handler? If you accept,\n"
        "future new patch extensions will be associated automatically."
    )

    choice: Optional[str] = None
    choice = _qt_question_dialog(
        title=f"Handle .{ext} with ap-bizhelper",
        text=prompt_text,
        ok_label="Register handler",
        cancel_label="Not now",
        extra_label="Disable prompts",
    )

    apply_associations = False
    if choice == "ok":
        set_association_mode("enabled")
        set_ext_association(ext, "registered")
        if ext not in registered_exts:
            registered_exts.append(ext)
        apply_associations = True

    if choice == "extra":
        set_association_mode("disabled")
        set_ext_association(ext, "declined")

    if choice == "cancel":
        set_ext_association(ext, "declined")

    if apply_associations:
        _apply_association_files(registered_exts)


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


def _wait_for_launched_apps_to_close(appimage: Path, baseline_bizhawk_pids: Set[int]) -> None:
    """Block until Archipelago/AppImage and launched BizHawk exit."""

    # Only wait when apps actually launched; the archipelago AppImage is always required
    # for the main flow so appimage is expected to exist.
    print("[ap-bizhelper] Waiting for Archipelago/BizHawk to close before ending Steam session...")
    while True:
        archipelago_running = _is_archipelago_running() or _is_appimage_mounted(appimage)
        bizhawk_running = any(
            pid not in baseline_bizhawk_pids for pid in _list_bizhawk_pids()
        )

        if not archipelago_running and not bizhawk_running:
            print("[ap-bizhelper] Archipelago and BizHawk have closed; continuing shutdown.")
            return

        time.sleep(2)


def _notify_steam_game_exit(appid: str) -> None:
    """Ask Steam to clear the running state for this app id."""

    steam_binary = shutil.which("steam") or shutil.which("/usr/bin/steam")
    if not steam_binary:
        return

    try:
        subprocess.Popen(
            [steam_binary, f"steam://appquit/{appid}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"[ap-bizhelper] Requested Steam to end session for app id {appid}.")
    except Exception:
        # Nothing else to do if Steam is unavailable or refusing the command
        pass


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
        env = APP_LOGGER.component_environ(
            env=os.environ.copy(),
            category="bizhawk-runner",
            subdir="runner",
            env_var=RUNNER_LOG_ENV,
        )
        subprocess.Popen([str(runner), str(rom)], env=env)
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


def _run_prereqs(settings: dict, *, allow_archipelago_skip: bool = False) -> Tuple[Optional[Path], Optional[Path]]:
    with APP_LOGGER.context("_run_prereqs"):
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
                settings=settings,
            )

        bizhawk_result: Optional[Tuple[Path, Path, bool]] = None
        bizhawk_result = ensure_bizhawk_and_proton(
            download_selected=bizhawk,
            create_shortcut=shortcuts,
            download_messages=download_messages,
            settings=settings,
        )
        if bizhawk:
            if bizhawk_result is None:
                raise RuntimeError("BizHawk setup was cancelled or failed.")
            runner, _, _ = bizhawk_result
        elif bizhawk_result is not None:
            runner, _, _ = bizhawk_result

        if download_messages:
            message = "Completed downloads:\n- " + "\n- ".join(download_messages)
            APP_LOGGER.log(message, include_context=True)
            info_dialog(message)

        return appimage, runner


def _run_full_flow(settings: dict, patch_arg: Optional[str] = None) -> int:
    with APP_LOGGER.context("_run_full_flow"):
        try:
            appimage, runner = _run_prereqs(settings)
        except RuntimeError as exc:
            error_dialog(str(exc))
            return 1

        _capture_steam_appid_if_present(settings)

        if appimage is None:
            error_dialog("Archipelago was not selected for download and is required to continue.")
            return 1

        baseline_pids = _list_bizhawk_pids()

        _apply_association_files(_registered_association_exts())

        try:
            patch = _parse_patch_arg(patch_arg) if patch_arg else _select_patch_file()
        except RuntimeError as exc:
            error_dialog(str(exc))
            return 1

        _handle_extension_association(patch.suffix.lstrip("."))

        ensure_apworld_for_patch(patch)

        APP_LOGGER.log(
            f"Launching Archipelago with patch: {patch}",
            include_context=True,
            mirror_console=True,
        )
        shim_env = prepare_dialog_shim_env(APP_LOGGER)
        launch_env = APP_LOGGER.session_environ(env=os.environ.copy())
        if shim_env:
            launch_env.update(shim_env)
        try:
            subprocess.Popen([str(appimage), str(patch)], env=launch_env)
        except Exception as exc:  # pragma: no cover - runtime launcher safety net
            error_dialog(f"Failed to launch Archipelago: {exc}")
            return 1

        if _wait_for_archipelago_ready(appimage):
            _handle_bizhawk_for_patch(patch, runner, baseline_pids)

        steam_appid = _get_known_steam_appid(settings)
        if _is_running_under_steam():
            _wait_for_launched_apps_to_close(appimage, baseline_pids)
        if steam_appid:
            _notify_steam_game_exit(steam_appid)

        return 0


def main(argv: list[str]) -> int:
    with APP_LOGGER.context("main"):
        APP_LOGGER.log("Starting ap-bizhelper", include_context=True)
        try:
            _ensure_qt_available()
        except RuntimeError:
            return 1
        settings = load_settings()

        settings_dirty = False

        cached_relaunch_args = settings.pop("PENDING_RELAUNCH_ARGS", [])
        if cached_relaunch_args:
            settings_dirty = True

        if "USE_CACHED_RELAUNCH_ARGS" in settings:
            settings_dirty = True
            settings.pop("USE_CACHED_RELAUNCH_ARGS", None)

        if settings_dirty:
            save_settings(settings)

        _capture_steam_appid_if_present(settings)

        user_args = [arg for arg in argv[1:] if not arg.startswith("--appimage")]
        running_under_steam = _is_running_under_steam()

        if running_under_steam:
            if user_args:
                _clear_relaunch_cache(settings, force_save=True)
            else:
                user_args = [str(arg) for arg in cached_relaunch_args if str(arg).strip()]
                _clear_relaunch_cache(settings, force_save=True)
        else:
            settings["PENDING_RELAUNCH_ARGS"] = user_args
            save_settings(settings)
            _maybe_relaunch_via_steam(argv, settings)

        patch_arg: Optional[str] = user_args[0] if user_args else None

        if patch_arg == "ensure":
            if len(user_args) > 1:
                APP_LOGGER.log(
                    "Usage: ap_bizhelper.py [ensure]",
                    level="ERROR",
                    include_context=True,
                    mirror_console=True,
                    stream="stderr",
                )
                return 1
            try:
                _run_prereqs(settings, allow_archipelago_skip=True)
            except RuntimeError:
                return 1
            _capture_steam_appid_if_present(settings)
            return 0

        if len(user_args) > 1:
            APP_LOGGER.log(
                "Extra launcher arguments detected; treating the first argument as the patch and ignoring the rest.",
                level="WARNING",
                include_context=True,
                mirror_console=True,
                stream="stderr",
            )

        return _run_full_flow(settings, patch_arg)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
