#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import os
import shlex
import shutil
import signal
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
    open_kill_switch_dialog,
    question_dialog as _qt_question_dialog,
    select_file_dialog as _select_file_dialog,
    dialogs_active as _dialogs_active,
    error_dialog,
    info_dialog,
    transient_info_dialog,
)
from .ap_bizhelper_bizhawk import (
    connectors_need_download,
    auto_detect_bizhawk_exe,
    ensure_bizhawk_and_proton,
    proton_available,
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
    AP_VERSION_KEY,
    APPLICATIONS_DIR,
    ARCHIPELAGO_WORLDS_DIR,
    BIZHELPER_APPIMAGE_KEY,
    BIZHAWK_EXE_KEY,
    BIZHAWK_RUNNER_KEY,
    FILE_FILTER_APWORLD,
    LOG_PREFIX,
    MIME_PACKAGES_DIR,
    PENDING_RELAUNCH_ARGS_KEY,
    PROTON_BIN_KEY,
    STEAM_APPID_KEY,
    STEAM_ROOT_PATH_KEY,
    USE_CACHED_RELAUNCH_ARGS_KEY,
    ENABLE_GAMEPAD_KILL_SWITCH_KEY,
)
from .logging_utils import RUNNER_LOG_ENV, get_app_logger
from .ap_bizhelper_worlds import ensure_apworld_for_patch
from .ui_utils import ensure_local_action_scripts, show_uninstall_dialog, show_utils_dialog


APP_LOGGER = get_app_logger()
_SHUTDOWN_SIGNAL_ACTIVE = False
_SIGNAL_HANDLERS_INSTALLED = False
_SHUTDOWN_DEBUG_ENV = "AP_BIZHELPER_DEBUG_SHUTDOWN"
_KILL_SWITCH_ACTIVE = False
_KILL_SWITCH_DIALOG = None
_QT_EVENT_PUMP_INTERVAL = 0.1


def _qt_dialogs_active() -> bool:
    return _dialogs_active() or _KILL_SWITCH_DIALOG is not None


def _pump_qt_events() -> None:
    if not _qt_dialogs_active():
        return
    if importlib.util.find_spec("PySide6") is None:
        return
    from PySide6 import QtWidgets  # type: ignore
    app = QtWidgets.QApplication.instance()
    if app is None:
        return
    try:
        app.processEvents()
    except Exception:
        pass


def _sleep_with_event_pump(duration: float, *, interval: float = _QT_EVENT_PUMP_INTERVAL) -> None:
    if duration <= 0:
        return
    if not _qt_dialogs_active() or interval <= 0:
        time.sleep(duration)
        return
    deadline = time.monotonic() + duration
    while True:
        _pump_qt_events()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(interval, remaining))


def _shutdown_debug_enabled() -> bool:
    value = str(os.environ.get(_SHUTDOWN_DEBUG_ENV, "")).strip().lower()
    return value not in ("", "0", "false", "no", "off")


def _shutdown_debug_log(
    message: str,
    *,
    location: str,
    data: Optional[dict[str, object]] = None,
) -> None:
    if not _shutdown_debug_enabled():
        return
    if data:
        extras = " ".join(f"{key}={data[key]!r}" for key in sorted(data))
        message = f"{message} | {extras}"
    try:
        APP_LOGGER.log(
            message,
            include_context=True,
            location=location,
        )
    except Exception:
        pass


def _tracked_bizhawk_pids(baseline_bizhawk_pids: Set[int]) -> Set[int]:
    return {pid for pid in _list_bizhawk_pids() if pid not in baseline_bizhawk_pids}


def _wait_for_bizhawk_exit(baseline_bizhawk_pids: Set[int], *, timeout: float = 4.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        _pump_qt_events()
        if not _tracked_bizhawk_pids(baseline_bizhawk_pids):
            return True
        _sleep_with_event_pump(0.25)
    return not _tracked_bizhawk_pids(baseline_bizhawk_pids)


def _coerce_timeout_value(value: object, default: float) -> float:
    if value is None:
        return default
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return default
    if timeout < 0:
        return default
    return timeout


def _get_shutdown_timeout(
    settings: dict,
    *,
    setting_key: str,
    env_key: str,
    default: float,
) -> float:
    env_value = os.environ.get(env_key)
    if env_value is not None:
        return _coerce_timeout_value(env_value, default)
    return _coerce_timeout_value(settings.get(setting_key), default)


def _coerce_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    string_value = str(value).strip().lower()
    if string_value in ("1", "true", "yes", "on", "y"):
        return True
    if string_value in ("0", "false", "no", "off", "n"):
        return False
    return default


def _get_shutdown_flag(
    settings: dict,
    *,
    setting_key: str,
    env_key: str,
    default: bool,
) -> bool:
    env_value = os.environ.get(env_key)
    if env_value is not None:
        return _coerce_bool(env_value, default)
    return _coerce_bool(settings.get(setting_key), default)


def _terminate_bizhawk_processes(
    baseline_bizhawk_pids: Set[int],
    *,
    term_timeout: float = 10.0,
    kill_timeout: float = 4.0,
    allow_sigkill: bool = True,
) -> bool:
    tracked_pids = _tracked_bizhawk_pids(baseline_bizhawk_pids)
    if not tracked_pids:
        return False

    print(f"{LOG_PREFIX} Sending SIGTERM to BizHawk (pids: {sorted(tracked_pids)}).")
    for pid in tracked_pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError:
            continue

    _wait_for_bizhawk_exit(baseline_bizhawk_pids, timeout=term_timeout)

    remaining = _tracked_bizhawk_pids(baseline_bizhawk_pids)
    if not remaining:
        return False

    if not allow_sigkill or kill_timeout <= 0:
        print(f"{LOG_PREFIX} BizHawk still running; skipping SIGKILL.")
        return True

    print(f"{LOG_PREFIX} BizHawk still running; sending SIGKILL to {sorted(remaining)}.")
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
        except PermissionError:
            continue

    _wait_for_bizhawk_exit(baseline_bizhawk_pids, timeout=kill_timeout)
    return bool(_tracked_bizhawk_pids(baseline_bizhawk_pids))


def _install_shutdown_signal_handlers(settings: dict, baseline_bizhawk_pids: Set[int]) -> None:
    global _SIGNAL_HANDLERS_INSTALLED
    if _SIGNAL_HANDLERS_INSTALLED:
        return

    def _handle_shutdown_signal(signum: int, _frame: object) -> None:
        global _SHUTDOWN_SIGNAL_ACTIVE
        if _SHUTDOWN_SIGNAL_ACTIVE:
            return
        _SHUTDOWN_SIGNAL_ACTIVE = True

        try:
            print(f"{LOG_PREFIX} Received signal {signum}; attempting BizHawk shutdown.")
            term_timeout = _get_shutdown_timeout(
                settings,
                setting_key="BIZHAWK_TERM_TIMEOUT",
                env_key="AP_BIZHELPER_BIZHAWK_TERM_TIMEOUT",
                default=10.0,
            )
            kill_timeout = _get_shutdown_timeout(
                settings,
                setting_key="BIZHAWK_KILL_TIMEOUT",
                env_key="AP_BIZHELPER_BIZHAWK_KILL_TIMEOUT",
                default=4.0,
            )
            allow_sigkill = _get_shutdown_flag(
                settings,
                setting_key="BIZHAWK_ALLOW_SIGKILL",
                env_key="AP_BIZHELPER_BIZHAWK_ALLOW_SIGKILL",
                default=True,
            )
            wait_timeout = _get_shutdown_timeout(
                settings,
                setting_key="BIZHAWK_SHUTDOWN_WAIT_TIMEOUT",
                env_key="AP_BIZHELPER_BIZHAWK_SHUTDOWN_WAIT_TIMEOUT",
                default=6.0,
            )

            still_running = _terminate_bizhawk_processes(
                baseline_bizhawk_pids,
                term_timeout=term_timeout,
                kill_timeout=kill_timeout,
                allow_sigkill=allow_sigkill,
            )
            if still_running and wait_timeout > 0:
                print(f"{LOG_PREFIX} Waiting for BizHawk to exit before syncing SaveRAM.")
                _wait_for_bizhawk_exit(baseline_bizhawk_pids, timeout=wait_timeout)

            if not _tracked_bizhawk_pids(baseline_bizhawk_pids):
                sync_bizhawk_saveram(settings)
            else:
                print(f"{LOG_PREFIX} BizHawk still running; skipping SaveRAM sync.")
        finally:
            raise SystemExit(0)

    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)


def _kill_switch_dialog_enabled(settings: dict) -> bool:
    return _coerce_bool(settings.get(ENABLE_GAMEPAD_KILL_SWITCH_KEY), True)


def _run_graceful_shutdown(settings: dict, baseline_bizhawk_pids: Set[int]) -> None:
    term_timeout = _get_shutdown_timeout(
        settings,
        setting_key="BIZHAWK_TERM_TIMEOUT",
        env_key="AP_BIZHELPER_BIZHAWK_TERM_TIMEOUT",
        default=10.0,
    )
    kill_timeout = _get_shutdown_timeout(
        settings,
        setting_key="BIZHAWK_KILL_TIMEOUT",
        env_key="AP_BIZHELPER_BIZHAWK_KILL_TIMEOUT",
        default=4.0,
    )
    allow_sigkill = _get_shutdown_flag(
        settings,
        setting_key="BIZHAWK_ALLOW_SIGKILL",
        env_key="AP_BIZHELPER_BIZHAWK_ALLOW_SIGKILL",
        default=True,
    )
    wait_timeout = _get_shutdown_timeout(
        settings,
        setting_key="BIZHAWK_SHUTDOWN_WAIT_TIMEOUT",
        env_key="AP_BIZHELPER_BIZHAWK_SHUTDOWN_WAIT_TIMEOUT",
        default=6.0,
    )

    still_running = _terminate_bizhawk_processes(
        baseline_bizhawk_pids,
        term_timeout=term_timeout,
        kill_timeout=kill_timeout,
        allow_sigkill=allow_sigkill,
    )
    if still_running and wait_timeout > 0:
        print(f"{LOG_PREFIX} Waiting for BizHawk to exit before syncing SaveRAM.")
        _wait_for_bizhawk_exit(baseline_bizhawk_pids, timeout=wait_timeout)

    if not _tracked_bizhawk_pids(baseline_bizhawk_pids):
        sync_bizhawk_saveram(settings)
    else:
        print(f"{LOG_PREFIX} BizHawk still running; skipping SaveRAM sync.")


def _install_kill_switch_dialog(settings: dict, baseline_bizhawk_pids: Set[int]) -> None:
    global _KILL_SWITCH_ACTIVE, _KILL_SWITCH_DIALOG
    if not _kill_switch_dialog_enabled(settings):
        return

    try:
        from PySide6 import QtCore, QtWidgets  # type: ignore
    except Exception:
        return

    app = QtWidgets.QApplication.instance()
    if app is None:
        return

    def _on_kill_switch_close() -> None:
        global _KILL_SWITCH_ACTIVE
        if _KILL_SWITCH_ACTIVE:
            return
        _KILL_SWITCH_ACTIVE = True
        _shutdown_debug_log(
            "Kill switch dialog closed",
            location="kill-switch-dialog",
        )
        try:
            transient_info_dialog("Kill switch closed. Shutting down.")
        except Exception:
            pass
        _run_graceful_shutdown(settings, baseline_bizhawk_pids)
        try:
            QtCore.QCoreApplication.quit()
        except Exception:
            pass
        raise SystemExit(0)

    dialog = open_kill_switch_dialog(on_close=_on_kill_switch_close, settings=settings)
    if dialog is not None:
        _KILL_SWITCH_DIALOG = dialog
    _SIGNAL_HANDLERS_INSTALLED = True


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
    proton_str = str(settings.get(PROTON_BIN_KEY, "") or "")

    exe = Path(exe_str) if exe_str else None
    runner = Path(runner_str) if runner_str else None
    proton_bin = Path(proton_str) if proton_str else None

    return not (
        exe and exe.is_file() and runner and runner.is_file() and proton_bin and proton_bin.is_file()
    )


def _needs_proton_download(settings: dict) -> bool:
    return not proton_available(settings)


def _needs_connector_download(settings: dict, *, ap_version: str) -> bool:
    exe_str = str(settings.get(BIZHAWK_EXE_KEY, "") or "")
    exe = Path(exe_str) if exe_str else None
    return connectors_need_download(settings, exe, ap_version=ap_version)


def _prompt_setup_choices(
    *,
    allow_archipelago_skip: bool,
    show_archipelago: bool,
    show_bizhawk: bool,
    show_connectors: bool,
    show_proton: bool,
) -> Tuple[bool, bool, bool, bool, bool]:
    if not any((show_archipelago, show_bizhawk, show_connectors, show_proton)):
        return False, False, False, False, False

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

    connectors_box = None
    if show_connectors:
        connectors_box = QtWidgets.QCheckBox("BizHawk connectors (download)")
        connectors_box.setChecked(True)
        layout.addWidget(connectors_box)

    proton_box = None
    if show_proton:
        proton_box = QtWidgets.QCheckBox("Proton 10 (local copy)")
        proton_box.setChecked(True)
        layout.addWidget(proton_box)

    shortcut_box = None
    if show_archipelago or show_bizhawk or show_connectors:
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
    connectors = connectors_box.isChecked() if connectors_box is not None else False
    proton = proton_box.isChecked() if proton_box is not None else False
    shortcuts = shortcut_box.isChecked() if shortcut_box is not None else False

    return arch, bizhawk, connectors, shortcuts, proton


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
        _pump_qt_events()
        if _is_archipelago_running() or _is_appimage_mounted(appimage):
            return True
        _sleep_with_event_pump(1)

    print(
        f"{LOG_PREFIX} Archipelago did not appear to start within the timeout; "
        "skipping BizHawk auto-launch."
    )
    return False


def _wait_for_launched_apps_to_close(
    appimage: Path,
    baseline_bizhawk_pids: Set[int],
    *,
    timeout: float = 0.0,
) -> None:
    """Block until Archipelago/AppImage and launched BizHawk exit."""

    # Only wait when apps actually launched; the archipelago AppImage is always required
    # for the main flow so appimage is expected to exist.
    print(f"{LOG_PREFIX} Waiting for Archipelago/BizHawk to close before ending Steam session...")
    timeout_start = time.monotonic()
    paused_total = 0.0
    pause_started: Optional[float] = None
    while True:
        _pump_qt_events()
        archipelago_running = _is_archipelago_running()
        bizhawk_pids = _list_bizhawk_pids()
        tracked_bizhawk = {pid for pid in bizhawk_pids if pid not in baseline_bizhawk_pids}
        bizhawk_running = bool(tracked_bizhawk)

        _shutdown_debug_log(
            "Shutdown wait status",
            location="shutdown-wait",
            data={
                "archipelago_running": archipelago_running,
                "appimage": str(appimage),
                "bizhawk_pids": sorted(bizhawk_pids),
                "tracked_bizhawk_pids": sorted(tracked_bizhawk),
            },
        )

        if not archipelago_running and not bizhawk_running:
            print(f"{LOG_PREFIX} Archipelago and BizHawk have closed; continuing shutdown.")
            return

        if timeout > 0:
            now = time.monotonic()
            if _qt_dialogs_active():
                if pause_started is None:
                    pause_started = now
            elif pause_started is not None:
                paused_total += now - pause_started
                pause_started = None
            paused_duration = paused_total + (now - pause_started if pause_started is not None else 0.0)
            elapsed = now - timeout_start - paused_duration
            remaining = timeout - elapsed
            if remaining <= 0:
                print(f"{LOG_PREFIX} Shutdown wait timeout reached; ending Steam session anyway.")
                return

        _sleep_with_event_pump(2)


def _resolve_bizhawk_root(settings: dict) -> Optional[Path]:
    exe_str = str(settings.get(BIZHAWK_EXE_KEY, "") or "")
    if exe_str:
        exe_path = Path(exe_str)
        if exe_path.is_file():
            return exe_path.parent

    runner_str = str(settings.get(BIZHAWK_RUNNER_KEY, "") or "")
    if runner_str:
        runner_path = Path(runner_str)
        if runner_path.is_file():
            return runner_path.parent

    exe_path = auto_detect_bizhawk_exe(settings)
    if exe_path and exe_path.is_file():
        return exe_path.parent

    return None


def sync_bizhawk_saveram(settings: dict) -> None:
    with APP_LOGGER.context("sync_bizhawk_saveram"):
        bizhawk_pids = _list_bizhawk_pids()
        if bizhawk_pids:
            _shutdown_debug_log(
                "Skipping SaveRAM sync; BizHawk still running.",
                location="saveram-sync",
                data={"bizhawk_pids": sorted(bizhawk_pids)},
            )
            print(f"{LOG_PREFIX} BizHawk still running; skipping SaveRAM sync.")
            return

        bizhawk_root = _resolve_bizhawk_root(settings)
        if bizhawk_root is None or not bizhawk_root.is_dir():
            _shutdown_debug_log(
                "BizHawk root directory not found; skipping SaveRAM sync.",
                location="saveram-sync",
                data={"bizhawk_root": None if bizhawk_root is None else str(bizhawk_root)},
            )
            print(f"{LOG_PREFIX} BizHawk root directory not found; skipping SaveRAM sync.")
            return

        central_root = get_path_setting(settings, "BIZHAWK_SAVERAM_DIR")
        save_dirs = [path for path in bizhawk_root.rglob("SaveRAM") if path.is_dir()]
        if not save_dirs:
            _shutdown_debug_log(
                "No SaveRAM directories found; skipping SaveRAM sync.",
                location="saveram-sync",
                data={"bizhawk_root": str(bizhawk_root)},
            )
            print(f"{LOG_PREFIX} No SaveRAM directories found under {bizhawk_root}.")
            return

        _shutdown_debug_log(
            "Starting SaveRAM sync",
            location="saveram-sync",
            data={
                "bizhawk_root": str(bizhawk_root),
                "save_dirs": [str(path) for path in save_dirs],
                "central_root": str(central_root),
            },
        )

        for save_ram in save_dirs:
            if save_ram.is_symlink():
                if save_ram.exists():
                    continue
                print(f"{LOG_PREFIX} Removing broken SaveRAM symlink at {save_ram}.")
                try:
                    save_ram.unlink()
                except OSError:
                    continue

            try:
                instance_rel = save_ram.parent.relative_to(bizhawk_root)
            except ValueError:
                instance_rel = Path(save_ram.parent.name)

            if instance_rel.parts:
                console_label = instance_rel.parts[-1]
            else:
                console_label = "default"

            console_label = console_label.strip() or "default"
            if console_label == ".":
                console_label = "default"
            console_label = console_label.replace("_", "-")

            dest_root = central_root / console_label
            if dest_root.is_symlink():
                continue
            dest_root.mkdir(parents=True, exist_ok=True)

            try:
                if save_ram.exists():
                    source_mode = save_ram.stat().st_mode
                    dest_root.chmod(source_mode)
            except (OSError, PermissionError):
                pass

            if save_ram.exists():
                for item in save_ram.iterdir():
                    dest_item = dest_root / item.name
                    if item.is_dir():
                        shutil.copytree(
                            item,
                            dest_item,
                            copy_function=shutil.copy2,
                            dirs_exist_ok=True,
                        )
                    else:
                        dest_item.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(item, dest_item)

            if save_ram.exists():
                shutil.rmtree(save_ram)
            os.symlink(dest_root, save_ram)
            print(f"{LOG_PREFIX} Synced SaveRAM to {dest_root} and linked {save_ram}.")
            _shutdown_debug_log(
                "SaveRAM sync completed",
                location="saveram-sync",
                data={
                    "save_ram": str(save_ram),
                    "dest_root": str(dest_root),
                },
            )


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
        print(f"{LOG_PREFIX} Requested Steam to end session for app id {appid}.")
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
        _pump_qt_events()
        rom = _find_matching_rom(patch)
        if rom:
            print(f"{LOG_PREFIX} ROM detected: {rom}")
            return rom
        _sleep_with_event_pump(1)
    print(f"{LOG_PREFIX} Timed out waiting for ROM; not launching BizHawk.")
    return None


def _launch_bizhawk(runner: Path, rom: Path) -> None:
    print(f"{LOG_PREFIX} Launching BizHawk runner: {runner} {rom}")
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
        _pump_qt_events()
        current = _list_bizhawk_pids()
        if current.difference(baseline_set):
            return True
        _sleep_with_event_pump(1)
    return False


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


def _handle_bizhawk_for_patch(patch: Path, runner: Optional[Path], baseline_pids: Iterable[int]) -> None:
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
        print(f"{LOG_PREFIX} Behavior 'fallback': launching BizHawk via Proton.")
        _launch_bizhawk(runner, rom)
        return
    if behavior not in (None, ""):
        print(f"{LOG_PREFIX} Unknown behavior '{behavior}' for .{ext}; doing nothing for safety.")
        return

    print(f"{LOG_PREFIX} No behavior stored yet; waiting briefly to see if BizHawk appears on its own.")
    if _detect_new_bizhawk(baseline_pids):
        print(f"{LOG_PREFIX} Detected new BizHawk instance; recording .{ext} as 'auto'.")
        set_ext_behavior(ext, "auto")
        return

    print(
        f"{LOG_PREFIX} No BizHawk detected after fallback timeout; "
        f"switching .{ext} to 'fallback' and launching runner."
    )
    set_ext_behavior(ext, "fallback")
    _launch_bizhawk(runner, rom)


def _run_prereqs(settings: dict, *, allow_archipelago_skip: bool = False) -> Tuple[Optional[Path], Optional[Path]]:
    with APP_LOGGER.context("_run_prereqs"):
        _ensure_steam_root(settings)
        need_arch = _needs_archipelago_download(settings)
        need_bizhawk = _needs_bizhawk_download(settings)
        need_proton = _needs_proton_download(settings)
        ap_version = str(settings.get(AP_VERSION_KEY, "") or "")
        need_connectors = need_bizhawk or _needs_connector_download(settings, ap_version=ap_version)

        if any((need_arch, need_bizhawk, need_connectors, need_proton)):
            arch, bizhawk, connectors, shortcuts, proton = _prompt_setup_choices(
                allow_archipelago_skip=allow_archipelago_skip,
                show_archipelago=need_arch,
                show_bizhawk=need_bizhawk,
                show_connectors=need_connectors,
                show_proton=need_proton,
            )
        else:
            arch = False
            bizhawk = False
            connectors = False
            shortcuts = False
            proton = False

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
            download_proton=proton,
            create_shortcut=shortcuts,
            download_messages=download_messages,
            settings=settings,
            stage_connectors=connectors,
            allow_manual_connector_selection=need_connectors,
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


def _run_full_flow(
    settings: dict,
    patch_arg: Optional[str] = None,
    *,
    allow_steam: bool = True,
    baseline_bizhawk_pids: Optional[Set[int]] = None,
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

        baseline_pids = baseline_bizhawk_pids if baseline_bizhawk_pids is not None else _list_bizhawk_pids()
        _install_shutdown_signal_handlers(settings, baseline_pids)

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
            _handle_bizhawk_for_patch(patch, runner, baseline_pids)

        if allow_steam:
            steam_appid = _get_known_steam_appid(settings)
            if _is_running_under_steam():
                shutdown_timeout = _get_shutdown_timeout(
                    settings,
                    setting_key="STEAM_SHUTDOWN_TIMEOUT",
                    env_key="AP_BIZHELPER_STEAM_SHUTDOWN_TIMEOUT",
                    default=0.0,
                )
                if not archipelago_ready:
                    shutdown_timeout = 0.0
                _wait_for_launched_apps_to_close(
                    appimage,
                    baseline_pids,
                    timeout=shutdown_timeout,
                )
                sync_bizhawk_saveram(settings)
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

        baseline_pids = _list_bizhawk_pids()
        _install_kill_switch_dialog(settings, baseline_pids)

        settings_dirty = False

        cached_relaunch_args = settings.pop(PENDING_RELAUNCH_ARGS_KEY, [])
        if cached_relaunch_args:
            settings_dirty = True

        if USE_CACHED_RELAUNCH_ARGS_KEY in settings:
            settings_dirty = True
            settings.pop(USE_CACHED_RELAUNCH_ARGS_KEY, None)

        if settings_dirty:
            save_settings(settings)

        user_args = [arg for arg in argv[1:] if not arg.startswith("--appimage")]
        no_steam = "--nosteam" in user_args
        if no_steam:
            user_args = [arg for arg in user_args if arg != "--nosteam"]

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

        patch_arg: Optional[str] = user_args[0] if user_args else None

        if patch_arg == "ensure":
            if len(user_args) > 1:
                APP_LOGGER.log(
                    "Usage: ap_bizhelper.py [--nosteam] [ensure|utils|uninstall]",
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
                    "Usage: ap_bizhelper.py [--nosteam] [ensure|utils|uninstall]",
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
        if patch_arg == "uninstall":
            if len(user_args) > 1:
                APP_LOGGER.log(
                    "Usage: ap_bizhelper.py [--nosteam] [ensure|utils|uninstall]",
                    level="ERROR",
                    include_context=True,
                    mirror_console=True,
                    stream="stderr",
                )
                return 1
            if not _require_bizhelper_appimage(settings, "run the uninstaller"):
                return 1
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

        return _run_full_flow(
            settings,
            patch_arg,
            allow_steam=not no_steam,
            baseline_bizhawk_pids=baseline_pids,
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
