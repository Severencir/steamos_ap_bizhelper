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
from typing import Optional, Tuple

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
from .ap_bizhelper_bizhawk import (
    ensure_bizhawk_install,
    ensure_runtime_root,
    validate_runtime_root,
)
from .ap_bizhelper_config import (
    get_path_setting,
    get_default_path_setting,
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
from .constants import (
    AP_APPIMAGE_KEY,
    AP_WAIT_FOR_EXIT_KEY,
    AP_WAIT_FOR_EXIT_POLL_SECONDS_KEY,
    APPLICATIONS_DIR,
    ARCHIPELAGO_WORLDS_DIR,
    BIZHELPER_APPIMAGE_KEY,
    BIZHAWK_CLEAR_LD_PRELOAD_KEY,
    BIZHAWK_EXE_KEY,
    BIZHAWK_RUNNER_KEY,
    BIZHAWK_RUNTIME_DOWNLOAD_KEY,
    BIZHAWK_RUNTIME_ROOT_KEY,
    FILE_FILTER_APWORLD,
    LOG_PREFIX,
    MIME_PACKAGES_DIR,
    PENDING_RELAUNCH_ARGS_KEY,
    SAVE_MIGRATION_HELPER_PATH_KEY,
    STEAM_APPID_KEY,
    STEAM_ROOT_PATH_KEY,
    USE_CACHED_RELAUNCH_ARGS_KEY,
)
from .logging_utils import RUNNER_LOG_ENV, get_app_logger
from .ap_bizhelper_worlds import ensure_apworld_for_patch
from .ui_utils import (
    ensure_local_action_scripts,
    show_uninstall_dialog,
    show_utils_dialog,
    uninstall_all,
    uninstall_core,
)


APP_LOGGER = get_app_logger()
NO_STEAM_DEFAULT_COMMANDS = {"ensure", "uninstall-all", "uninstall-core"}


def _split_launcher_args(argv: list[str]) -> tuple[list[str], bool, bool]:
    user_args = [arg for arg in argv[1:] if not arg.startswith("--appimage")]
    explicit_no_steam = "--nosteam" in user_args
    explicit_steam = "--steam" in user_args
    if explicit_no_steam or explicit_steam:
        user_args = [arg for arg in user_args if arg not in {"--nosteam", "--steam"}]
    return user_args, explicit_no_steam, explicit_steam

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

        cached_appid = str(settings.get(STEAM_APPID_KEY) or "")
        if cached_appid == steam_game_id:
            return

        settings[STEAM_APPID_KEY] = steam_game_id
        save_settings(settings)
        APP_LOGGER.log(
            f"Detected Steam launch; cached app id {steam_game_id} for future relaunches.",
            include_context=True,
            mirror_console=True,
        )


def _capture_bizhelper_appimage(settings: dict) -> None:
    """Persist the current AppImage path into settings when available."""

    with APP_LOGGER.context("_capture_bizhelper_appimage"):
        appimage_value = os.environ.get("APPIMAGE")
        appimage_path: Optional[Path] = None

        if appimage_value:
            appimage_path = Path(appimage_value)
        else:
            argv_path = Path(sys.argv[0]).resolve()
            if argv_path.is_file():
                appimage_path = argv_path

        if not appimage_path:
            return

        cached_path = str(settings.get(BIZHELPER_APPIMAGE_KEY) or "")
        new_path = str(appimage_path)
        if cached_path == new_path:
            return

        settings[BIZHELPER_APPIMAGE_KEY] = new_path
        save_settings(settings)
        APP_LOGGER.log(
            f"Captured ap-bizhelper AppImage path: {new_path}",
            include_context=True,
        )


def _require_bizhelper_appimage(settings: dict, action: str) -> bool:
    appimage_value = str(settings.get(BIZHELPER_APPIMAGE_KEY) or "")
    if not appimage_value:
        error_dialog(
            "The ap-bizhelper AppImage path is missing from settings.\n\n"
            f"Unable to {action}."
        )
        return False

    appimage_path = Path(appimage_value)
    if not appimage_path.is_file():
        error_dialog(
            "The ap-bizhelper AppImage could not be found.\n\n"
            f"Path: {appimage_path}\n\nUnable to {action}."
        )
        return False

    return True


def _get_known_steam_appid(settings: dict) -> Optional[str]:
    """Return the active or cached Steam app id when available."""

    steam_game_id = _steam_game_id_from_env()
    if steam_game_id:
        return steam_game_id

    cached_appid = str(settings.get(STEAM_APPID_KEY) or "")
    if cached_appid.isdigit():
        return cached_appid

    return None


def _maybe_relaunch_via_steam(argv: list[str], settings: dict) -> None:
    """If not under Steam, try to relaunch through the matching shortcut."""

    with APP_LOGGER.context("_maybe_relaunch_via_steam"):
        if _is_running_under_steam():
            return

        steam_appid_env = os.environ.get("AP_BIZHELPER_STEAM_APPID")
        cached_appid = str(settings.get(STEAM_APPID_KEY) or "")

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
            _clear_relaunch_cache(settings, force_save=True)
            sys.exit(1)

        if str(settings.get(STEAM_APPID_KEY)) != str(appid):
            settings[STEAM_APPID_KEY] = str(appid)
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
                        settings[PENDING_RELAUNCH_ARGS_KEY] = argv[1:]
                        settings[USE_CACHED_RELAUNCH_ARGS_KEY] = True
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
    for cache_key in (PENDING_RELAUNCH_ARGS_KEY, USE_CACHED_RELAUNCH_ARGS_KEY):
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
    app_path_str = str(settings.get(AP_APPIMAGE_KEY, "") or "")
    app_path = Path(app_path_str) if app_path_str else None

    if app_path and app_path.is_file() and os.access(str(app_path), os.X_OK):
        return False

    if AP_APPIMAGE_DEFAULT.is_file() and os.access(str(AP_APPIMAGE_DEFAULT), os.X_OK):
        return False

    return True


def _needs_bizhawk_download(settings: dict) -> bool:
    exe_str = str(settings.get(BIZHAWK_EXE_KEY, "") or "")
    runner_str = str(settings.get(BIZHAWK_RUNNER_KEY, "") or "")

    exe = Path(exe_str) if exe_str else None
    runner = Path(runner_str) if runner_str else None

    return not (exe and exe.is_file() and runner and runner.is_file())


def _needs_runtime_setup(settings: dict) -> bool:
    runtime_root = get_path_setting(settings, BIZHAWK_RUNTIME_ROOT_KEY)
    try:
        validate_runtime_root(runtime_root)
        return False
    except Exception:
        return True


def _prompt_setup_choices(
    *,
    allow_archipelago_skip: bool,
    show_archipelago: bool,
    show_bizhawk: bool,
    show_runtime: bool,
) -> Tuple[bool, bool, bool, bool]:
    if not any((show_archipelago, show_bizhawk, show_runtime)):
        return False, False, False, False

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
        bizhawk_box = QtWidgets.QCheckBox("BizHawk (Linux)")
        bizhawk_box.setChecked(True)
        layout.addWidget(bizhawk_box)

    runtime_box = None
    if show_runtime:
        runtime_box = QtWidgets.QCheckBox("BizHawk deps (mono/libgdiplus/lua)")
        runtime_box.setChecked(True)
        layout.addWidget(runtime_box)

    shortcut_box = None
    if show_archipelago or show_bizhawk or show_runtime:
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
    runtime = runtime_box.isChecked() if runtime_box is not None else False
    shortcuts = shortcut_box.isChecked() if shortcut_box is not None else False

    return arch, bizhawk, runtime, shortcuts


def _ensure_apworld_for_extension(ext: str) -> None:
    ext = ext.strip().lower()
    if not ext:
        return

    # Only care about "new" extensions (no behavior stored yet)
    behavior = get_ext_behavior(ext)
    if behavior:
        return

    worlds_dir = ARCHIPELAGO_WORLDS_DIR
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
        print(f"{LOG_PREFIX} User skipped APWorld selection for .{ext}.")
        return

    apworld_path = _select_file_dialog(
        title=f"Select .apworld file for .{ext}",
        initial=Path.home(),
        file_filter=FILE_FILTER_APWORLD,
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

    applications_dir = APPLICATIONS_DIR
    mime_packages_dir = MIME_PACKAGES_DIR
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
        f"{LOG_PREFIX} Archipelago did not appear to start within the timeout; "
        "skipping BizHawk auto-launch."
    )
    return False


def _poll_seconds(value: object, *, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _wait_for_archipelago_exit(settings: dict, appimage: Optional[Path]) -> None:
    wait_enabled = bool(settings.get(AP_WAIT_FOR_EXIT_KEY, True))
    if not wait_enabled:
        return

    if not (_is_archipelago_running() or _is_appimage_mounted(appimage)):
        return

    poll_seconds = _poll_seconds(
        settings.get(AP_WAIT_FOR_EXIT_POLL_SECONDS_KEY),
        default=5,
    )
    APP_LOGGER.log(
        "Waiting for Archipelago to exit before closing ap-bizhelper.",
        include_context=True,
        mirror_console=True,
    )
    while _is_archipelago_running() or _is_appimage_mounted(appimage):
        time.sleep(poll_seconds)
    APP_LOGGER.log(
        "Archipelago closed; exiting ap-bizhelper.",
        include_context=True,
        mirror_console=True,
    )


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


def _wait_for_rom(patch: Path) -> Optional[Path]:
    rom = _find_matching_rom(patch)
    if rom:
        print(f"{LOG_PREFIX} ROM detected: {rom}")
        return rom
    print(f"{LOG_PREFIX} ROM not detected; not launching BizHawk.")
    return None


def _systemd_show_unit(unit: str, properties: list[str]) -> Optional[dict[str, str]]:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        APP_LOGGER.log(
            "systemctl is not available; cannot query BizHawk runner unit status.",
            include_context=True,
        )
        return None
    cmd = [systemctl, "--user", "show", unit, f"--property={','.join(properties)}"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        APP_LOGGER.log(
            f"Failed to query unit status for {unit}: rc={result.returncode} "
            f"stdout={result.stdout!r} stderr={result.stderr!r}",
            include_context=True,
        )
        return None
    data: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def _journalctl_tail(unit: str, *, lines: int = 50) -> Optional[str]:
    journalctl = shutil.which("journalctl")
    if not journalctl:
        APP_LOGGER.log(
            "journalctl is not available; cannot fetch BizHawk runner logs.",
            include_context=True,
        )
        return None
    cmd = [journalctl, "--user", "-u", unit, "-n", str(lines), "--no-pager"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        APP_LOGGER.log(
            f"Failed to read journalctl for {unit}: rc={result.returncode} "
            f"stdout={result.stdout!r} stderr={result.stderr!r}",
            include_context=True,
        )
        return None
    return result.stdout.strip()


def _check_bizhawk_unit(unit: str) -> Optional[str]:
    properties = ["ExecMainStatus", "Result", "ExecMainCode"]
    for _ in range(3):
        data = _systemd_show_unit(unit, properties)
        if data is None:
            return None
        result = data.get("Result", "")
        status = data.get("ExecMainStatus", "")
        if result or status:
            break
        time.sleep(0.2)
    else:
        return None

    result = data.get("Result", "")
    status = data.get("ExecMainStatus", "")
    if result == "success" and (not status or status == "0"):
        return None
    if status and status != "0":
        return f"ExecMainStatus={status} Result={result or '<unknown>'} ExecMainCode={data.get('ExecMainCode','')}"
    if result and result != "success":
        return f"Result={result} ExecMainStatus={status or '<unknown>'} ExecMainCode={data.get('ExecMainCode','')}"
    return None


def _launch_bizhawk(settings: dict, runner: Path, rom: Path) -> None:
    print(f"{LOG_PREFIX} Launching BizHawk runner: {runner} {rom}")
    try:
        env = APP_LOGGER.component_environ(
            env=os.environ.copy(),
            category="bizhawk-runner",
            subdir="runner",
            env_var=RUNNER_LOG_ENV,
        )
        clear_preload = bool(settings.get(BIZHAWK_CLEAR_LD_PRELOAD_KEY, True))
        if clear_preload and env.get("LD_PRELOAD"):
            APP_LOGGER.log(
                "Clearing LD_PRELOAD for BizHawk runner launch.",
                include_context=True,
            )
            env.pop("LD_PRELOAD", None)

        bizhawk_dir = runner.parent
        cmd = [str(runner), str(rom)]
        proc = subprocess.Popen(
            cmd,
            cwd=str(bizhawk_dir),
            env=env,
        )
        APP_LOGGER.log(
            f"BizHawk runner started (pid={proc.pid}). BizHawk will be launched by the runner in a detached systemd scope.",
            include_context=True,
        )

        # If the runner exits immediately, surface the error and include a small log tail if available.
        time.sleep(0.2)
        rc = proc.poll()
        if rc is not None and rc != 0:
            log_tail = ""
            log_path = env.get(RUNNER_LOG_ENV)
            if log_path:
                try:
                    path = Path(log_path)
                    if path.is_file():
                        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]
                        if lines:
                            log_tail = "\n\nRecent runner log:\n" + "\n".join(lines)
                except Exception:
                    log_tail = ""
            error_dialog(
                "BizHawk runner exited immediately "
                f"(rc={rc}).{log_tail}"
            )
    except FileNotFoundError as exc:
        error_dialog(f"Failed to launch BizHawk runner: {exc}")
    except Exception as exc:  # pragma: no cover - safety net for runtime environments
        error_dialog(f"Failed to launch BizHawk runner: {exc}")


def _run_save_migration_helper(
    *, system_dir: Optional[str] = None, settings: Optional[dict] = None
) -> bool:
    with APP_LOGGER.context("_run_save_migration_helper"):
        if settings is None:
            settings = load_settings()

        helper_path = get_path_setting(settings, SAVE_MIGRATION_HELPER_PATH_KEY)
        if not helper_path or not helper_path.is_file():
            APP_LOGGER.log(
                "Save migration helper path is missing or invalid; cannot continue.",
                include_context=True,
                mirror_console=True,
            )
            error_dialog("Save migration helper path is missing; cannot continue.")
            return False

        cmd = [str(helper_path)]
        if system_dir:
            cmd.append(system_dir)

        APP_LOGGER.log(
            f"Launching save migration helper: {' '.join(cmd)}",
            include_context=True,
            mirror_console=True,
        )
        if system_dir:
            APP_LOGGER.log(
                f"Save migration helper system_dir={system_dir}",
                include_context=True,
            )

        try:
            result = subprocess.run(cmd, check=False)
        except Exception as exc:
            APP_LOGGER.log(
                f"Save migration helper failed to start: {exc}",
                include_context=True,
                mirror_console=True,
            )
            error_dialog(f"Failed to run save migration helper: {exc}")
            return False

        APP_LOGGER.log(
            f"Save migration helper completed with returncode={result.returncode}",
            include_context=True,
            mirror_console=True,
        )
        if result.returncode != 0:
            error_dialog("Save migration helper reported an error; BizHawk will not be launched.")
            return False
        return True


def _ensure_steam_root(settings: dict) -> None:
    default_root = get_default_path_setting(STEAM_ROOT_PATH_KEY)
    current_root = get_path_setting(settings, STEAM_ROOT_PATH_KEY)
    if current_root != default_root:
        return
    if current_root.exists():
        return
    selection = _select_file_dialog(
        title="Select your Steam install folder",
        dialog_key=STEAM_ROOT_PATH_KEY,
        initial=default_root,
        settings=settings,
        select_directories=True,
    )
    if not selection:
        return
    settings[STEAM_ROOT_PATH_KEY] = str(selection)
    save_settings(settings)
    APP_LOGGER.log(
        f"Stored Steam root override: {selection}",
        include_context=True,
        mirror_console=True,
    )


def _handle_bizhawk_for_patch(settings: dict, patch: Path, runner: Optional[Path]) -> None:
    if runner is None or not runner.is_file():
        print(f"{LOG_PREFIX} BizHawk runner not configured or not executable; skipping auto-launch.")
        return

    rom = _wait_for_rom(patch)
    if rom is None:
        return

    ext = rom.suffix.lower().lstrip(".")

    if ext != "sfc":
        print(
            f"{LOG_PREFIX} Non-SFC ROM detected; skipping BizHawk auto-launch and "
            "deferring to Archipelago."
        )
        return

    behavior = get_ext_behavior(ext)
    print(f"{LOG_PREFIX} Patch: {patch}")
    print(f"{LOG_PREFIX} ROM: {rom}")
    print(f"{LOG_PREFIX} Detected extension: .{ext}")
    print(f"{LOG_PREFIX} Saved behavior for .{ext}: {behavior or '<none>'}")

    if behavior == "auto":
        print(f"{LOG_PREFIX} Behavior 'auto': not launching BizHawk; assuming AP/user handles it.")
        return
    if behavior == "fallback":
        print(f"{LOG_PREFIX} Behavior 'fallback': launching BizHawk.")
        if not _run_save_migration_helper(settings=settings):
            return
        _launch_bizhawk(settings, runner, rom)
        return
    if behavior not in (None, ""):
        print(f"{LOG_PREFIX} Unknown behavior '{behavior}' for .{ext}; doing nothing for safety.")
        return
    print(f"{LOG_PREFIX} No behavior stored yet; defaulting .{ext} to 'fallback'.")
    set_ext_behavior(ext, "fallback")
    if not _run_save_migration_helper(settings=settings):
        return
    _launch_bizhawk(settings, runner, rom)


def _run_prereqs(settings: dict, *, allow_archipelago_skip: bool = False) -> Tuple[Optional[Path], Optional[Path]]:
    with APP_LOGGER.context("_run_prereqs"):
        _ensure_steam_root(settings)
        need_arch = _needs_archipelago_download(settings)
        need_bizhawk = _needs_bizhawk_download(settings)
        need_runtime = _needs_runtime_setup(settings)

        if any((need_arch, need_bizhawk, need_runtime)):
            arch, bizhawk, runtime, shortcuts = _prompt_setup_choices(
                allow_archipelago_skip=allow_archipelago_skip,
                show_archipelago=need_arch,
                show_bizhawk=need_bizhawk,
                show_runtime=need_runtime,
            )
        else:
            arch = False
            bizhawk = False
            runtime = False
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

        download_runtime = bool(settings.get(BIZHAWK_RUNTIME_DOWNLOAD_KEY, True))
        if need_runtime:
            download_runtime = runtime
            settings[BIZHAWK_RUNTIME_DOWNLOAD_KEY] = download_runtime
            save_settings(settings)

        bizhawk_result = ensure_bizhawk_install(
            download_selected=bizhawk,
            create_shortcut=shortcuts,
            download_messages=download_messages,
            settings=settings,
        )
        bizhawk_downloaded = False
        if bizhawk:
            if bizhawk_result is None:
                raise RuntimeError("BizHawk setup was cancelled or failed.")
            runner, _, bizhawk_downloaded = bizhawk_result
        elif bizhawk_result is not None:
            runner, _, bizhawk_downloaded = bizhawk_result

        if bizhawk_downloaded and not _run_save_migration_helper(settings=settings):
            raise RuntimeError("Save migration helper failed after BizHawk setup.")

        runtime_root = ensure_runtime_root(
            settings,
            download_enabled=download_runtime,
            download_messages=download_messages,
            prompt_on_missing=True,
        )
        if runtime_root is None:
            raise RuntimeError("BizHawk runtime setup was cancelled or failed.")

        if download_messages:
            message = "Completed downloads:\n- " + "\n- ".join(download_messages)
            APP_LOGGER.log(message, include_context=True)
            info_dialog(message)

        return appimage, runner


def _run_full_flow(
    settings: dict,
    patch_arg: Optional[str] = None,
    *,
    allow_steam: bool = True,
) -> int:
    with APP_LOGGER.context("_run_full_flow"):
        try:
            appimage, runner = _run_prereqs(settings)
        except RuntimeError as exc:
            error_dialog(str(exc))
            return 1

        if allow_steam:
            _capture_steam_appid_if_present(settings)

        if appimage is None:
            error_dialog("Archipelago was not selected for download and is required to continue.")
            return 1

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
        launch_env["AP_BIZHELPER_PATCH"] = str(patch)
        if shim_env:
            launch_env.update(shim_env)
        try:
            subprocess.Popen([str(appimage), str(patch)], env=launch_env)
        except Exception as exc:  # pragma: no cover - runtime launcher safety net
            error_dialog(f"Failed to launch Archipelago: {exc}")
            return 1

        archipelago_ready = _wait_for_archipelago_ready(appimage)
        if archipelago_ready:
            _handle_bizhawk_for_patch(settings, patch, runner)

        _wait_for_archipelago_exit(settings, appimage)

        return 0


def main(argv: list[str]) -> int:
    with APP_LOGGER.context("main"):
        APP_LOGGER.log("Starting ap-bizhelper", include_context=True)
        try:
            _ensure_qt_available()
        except RuntimeError:
            return 1
        settings = load_settings()
        _capture_bizhelper_appimage(settings)
        ensure_local_action_scripts(settings)

        try:
            _ensure_qt_app(settings)
        except Exception as exc:
            APP_LOGGER.log(
                f"Failed to initialize Qt with settings: {exc}",
                level="ERROR",
                include_context=True,
                mirror_console=True,
                stream="stderr",
            )
            return 1

        settings_dirty = False

        cached_relaunch_args = settings.pop(PENDING_RELAUNCH_ARGS_KEY, [])
        if cached_relaunch_args:
            settings_dirty = True

        if USE_CACHED_RELAUNCH_ARGS_KEY in settings:
            settings_dirty = True
            settings.pop(USE_CACHED_RELAUNCH_ARGS_KEY, None)

        if settings_dirty:
            save_settings(settings)

        user_args, explicit_no_steam, explicit_steam = _split_launcher_args(argv)
        if explicit_no_steam and explicit_steam:
            APP_LOGGER.log(
                "Cannot combine --steam and --nosteam.",
                level="ERROR",
                include_context=True,
                mirror_console=True,
                stream="stderr",
            )
            return 1

        patch_arg: Optional[str] = user_args[0] if user_args else None
        no_steam = explicit_no_steam or (
            patch_arg in NO_STEAM_DEFAULT_COMMANDS and not explicit_steam
        )

        if not no_steam:
            _capture_steam_appid_if_present(settings)

        running_under_steam = _is_running_under_steam() if not no_steam else False

        if not no_steam:
            if running_under_steam:
                if user_args:
                    _clear_relaunch_cache(settings, force_save=True)
                else:
                    user_args = [str(arg) for arg in cached_relaunch_args if str(arg).strip()]
                    _clear_relaunch_cache(settings, force_save=True)
            else:
                settings[PENDING_RELAUNCH_ARGS_KEY] = user_args
                save_settings(settings)
                _maybe_relaunch_via_steam(argv, settings)

        usage_message = (
            "Usage: ap_bizhelper.py [--nosteam|--steam] "
            "[ensure|utils|uninstall|uninstall-all|uninstall-core]"
        )

        if patch_arg == "ensure":
            if len(user_args) > 1:
                APP_LOGGER.log(
                    usage_message,
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
            if not no_steam:
                _capture_steam_appid_if_present(settings)
            return 0
        if patch_arg == "utils":
            if len(user_args) > 1:
                APP_LOGGER.log(
                    usage_message,
                    level="ERROR",
                    include_context=True,
                    mirror_console=True,
                    stream="stderr",
                )
                return 1
            if not _require_bizhelper_appimage(settings, "open utilities"):
                return 1
            show_utils_dialog()
            return 0
        if patch_arg in {"uninstall", "uninstall-all", "uninstall-core"}:
            if len(user_args) > 1:
                APP_LOGGER.log(
                    usage_message,
                    level="ERROR",
                    include_context=True,
                    mirror_console=True,
                    stream="stderr",
                )
                return 1
            if not _require_bizhelper_appimage(settings, "run the uninstaller"):
                return 1
            if patch_arg == "uninstall-all":
                uninstall_all()
            elif patch_arg == "uninstall-core":
                uninstall_core()
            else:
                show_uninstall_dialog()
            return 0

        if len(user_args) > 1:
            APP_LOGGER.log(
                "Extra launcher arguments detected; treating the first argument as the patch and ignoring the rest.",
                level="WARNING",
                include_context=True,
                mirror_console=True,
                stream="stderr",
            )

        return _run_full_flow(settings, patch_arg, allow_steam=not no_steam)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
