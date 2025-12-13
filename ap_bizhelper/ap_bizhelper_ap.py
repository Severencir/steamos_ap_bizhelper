#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .ap_bizhelper_config import load_settings as _load_shared_settings, save_settings as _save_shared_settings

# Paths mirror the bash script and the config helper.
CONFIG_DIR = Path(os.path.expanduser("~/.config/ap_bizhelper_test"))
SETTINGS_FILE = CONFIG_DIR / "settings.json"
DATA_DIR = Path(os.path.expanduser("~/.local/share/ap_bizhelper_test"))
AP_APPIMAGE_DEFAULT = DATA_DIR / "Archipelago.AppImage"
DESKTOP_DIR = Path(os.path.expanduser("~/Desktop"))
DOWNLOADS_DIR = Path(os.path.expanduser("~/Downloads"))

GITHUB_API_LATEST = "https://api.github.com/repos/ArchipelagoMW/Archipelago/releases/latest"

_QT_APP: Optional["QtWidgets.QApplication"] = None
_QT_FONT_SCALE = 1.5
_QT_MIN_POINT_SIZE = 12
_QT_FILE_NAME_FONT_SCALE = 1.5
_QT_FILE_DIALOG_WIDTH = 1280
_QT_FILE_DIALOG_HEIGHT = 800
_QT_FILE_DIALOG_MAXIMIZE = True
_QT_FILE_DIALOG_NAME_WIDTH = 0
_QT_FILE_DIALOG_TYPE_WIDTH = 0
_QT_FILE_DIALOG_SIZE_WIDTH = 0
_QT_FILE_DIALOG_DATE_WIDTH = 0
_QT_FILE_DIALOG_COLUMN_SCALE = 1.8
_QT_IMPORT_ERROR: Optional[BaseException] = None
_DEFAULT_SETTINGS = {
    "ENABLE_GAMEPAD_FILE_DIALOG": True,
    "QT_FONT_SCALE": _QT_FONT_SCALE,
    "QT_MIN_POINT_SIZE": _QT_MIN_POINT_SIZE,
    "QT_FILE_NAME_FONT_SCALE": _QT_FILE_NAME_FONT_SCALE,
    "QT_FILE_DIALOG_WIDTH": _QT_FILE_DIALOG_WIDTH,
    "QT_FILE_DIALOG_HEIGHT": _QT_FILE_DIALOG_HEIGHT,
    "QT_FILE_DIALOG_MAXIMIZE": _QT_FILE_DIALOG_MAXIMIZE,
    "QT_FILE_DIALOG_NAME_WIDTH": _QT_FILE_DIALOG_NAME_WIDTH,
    "QT_FILE_DIALOG_TYPE_WIDTH": _QT_FILE_DIALOG_TYPE_WIDTH,
    "QT_FILE_DIALOG_SIZE_WIDTH": _QT_FILE_DIALOG_SIZE_WIDTH,
    "QT_FILE_DIALOG_DATE_WIDTH": _QT_FILE_DIALOG_DATE_WIDTH,
}

try:
    from PySide6 import QtCore as _QtCoreBase
except Exception:  # pragma: no cover - optional at import time
    _QtCoreBase = None


class GamepadFileDialogController(_QtCoreBase.QObject if _QtCoreBase else object):
    """Map Qt gamepad input to QFileDialog navigation."""

    AXIS_THRESHOLD = 0.6

    def __init__(self, dialog: "QtWidgets.QFileDialog") -> None:
        from PySide6 import QtCore, QtGamepad, QtGui, QtWidgets

        if _QtCoreBase is None:
            self.gamepad = None
            self._bind_timer = None
            self._warn_gamepad_unavailable(
                "Qt could not be imported; gamepad navigation is disabled."
            )
            return

        super().__init__(dialog)
        self.dialog = dialog
        self.QtCore = QtCore
        self.QtGui = QtGui
        self.QtWidgets = QtWidgets
        self.gamepad: Optional[QtGamepad.QGamepad] = None
        self._bind_timer: Optional[QtCore.QTimer] = None
        self._warned_no_gamepad = False
        self._gamepad_manager = QtGamepad.QGamepadManager.instance()

        self._start_gamepad_discovery()

        self.sidebar_view: Optional[QtWidgets.QWidget] = self._find_sidebar()
        self.file_view: Optional[QtWidgets.QWidget] = self._find_file_view()
        self._axis_state: dict[str, bool] = {
            "up": False,
            "down": False,
            "left": False,
            "right": False,
        }
        try:
            dialog.installEventFilter(self)
        except Exception:
            return

        self._bind_timer = self.QtCore.QTimer(self)
        self._bind_timer.setInterval(250)
        self._bind_timer.timeout.connect(self._on_bind_timer_timeout)

        try:
            self._gamepad_manager.connectedGamepadsChanged.connect(
                self._on_connected_gamepads_changed
            )
        except Exception:
            return

        if not self._bind_first_gamepad(initial=True):
            self._restart_bind_timer()
            self._fallback_to_keyboard_navigation()

    def eventFilter(self, obj: "QtCore.QObject", event: "QtCore.QEvent") -> bool:  # type: ignore[override]
        if obj is self.dialog and event.type() == self.QtCore.QEvent.Show:
            self.sidebar_view = self._find_sidebar()
            self.file_view = self._find_file_view()
        return False

    def _connect_signals(self) -> None:
        if self.gamepad is None:
            return
        self.gamepad.buttonAChanged.connect(self._on_accept)
        self.gamepad.buttonBChanged.connect(self._on_cancel)
        self.gamepad.buttonL1Changed.connect(self._on_back)
        self.gamepad.buttonR1Changed.connect(self._on_forward)
        self.gamepad.buttonYChanged.connect(self._on_up)
        self.gamepad.buttonXChanged.connect(self._on_context_menu)
        self.gamepad.buttonLeftChanged.connect(self._on_left)
        self.gamepad.buttonRightChanged.connect(self._on_right)
        self.gamepad.buttonUpChanged.connect(self._on_up_press)
        self.gamepad.buttonDownChanged.connect(self._on_down)
        self.gamepad.axisLeftXChanged.connect(self._on_axis_x)
        self.gamepad.axisLeftYChanged.connect(self._on_axis_y)

    def _disconnect_signals(self) -> None:
        if self.gamepad is None:
            return
        try:
            self.gamepad.buttonAChanged.disconnect()
            self.gamepad.buttonBChanged.disconnect()
            self.gamepad.buttonL1Changed.disconnect()
            self.gamepad.buttonR1Changed.disconnect()
            self.gamepad.buttonYChanged.disconnect()
            self.gamepad.buttonXChanged.disconnect()
            self.gamepad.buttonLeftChanged.disconnect()
            self.gamepad.buttonRightChanged.disconnect()
            self.gamepad.buttonUpChanged.disconnect()
            self.gamepad.buttonDownChanged.disconnect()
            self.gamepad.axisLeftXChanged.disconnect()
            self.gamepad.axisLeftYChanged.disconnect()
        except Exception:
            pass

    def _restart_bind_timer(self) -> None:
        if self._bind_timer is not None:
            self._bind_timer.start()

    def _stop_bind_timer(self) -> None:
        if self._bind_timer is not None:
            self._bind_timer.stop()

    def _start_gamepad_discovery(self) -> None:
        for method_name in ("startListening", "startDiscovery"):
            method = getattr(self._gamepad_manager, method_name, None)
            if callable(method):
                try:
                    result = method()
                except Exception:
                    continue
                if result is False:
                    continue
                return

    def _stop_gamepad_discovery(self) -> None:
        for method_name in ("stopListening", "stopDiscovery"):
            method = getattr(self._gamepad_manager, method_name, None)
            if callable(method):
                try:
                    method()
                except Exception:
                    pass
                return

    def _on_bind_timer_timeout(self) -> None:
        if self._bind_first_gamepad():
            self._stop_bind_timer()

    def _bind_first_gamepad(self, *, initial: bool = False) -> bool:
        from PySide6 import QtGamepad

        device_id = self._first_connected_gamepad_id()
        if device_id is None:
            self._start_gamepad_discovery()
            if not self._warned_no_gamepad:
                self._warn_gamepad_unavailable(
                    "No connected gamepads detected; connect a controller to enable navigation."
                )
                self._warned_no_gamepad = True
            self._disconnect_signals()
            self.gamepad = None
            self._restart_bind_timer()
            return False

        try:
            new_gamepad = QtGamepad.QGamepad(device_id, parent=self)
        except Exception as exc:
            self._warn_gamepad_unavailable(
                "Qt Gamepad backend or plugins are missing; falling back to keyboard navigation."
                f" Details: {exc}"
            )
            self._disconnect_signals()
            self.gamepad = None
            self._restart_bind_timer()
            return False

        self._disconnect_signals()
        if self.gamepad is not None:
            try:
                self.gamepad.deleteLater()
            except Exception:
                pass
        self.gamepad = new_gamepad
        self._warned_no_gamepad = False
        self._connect_signals()
        self._stop_bind_timer()
        return True

    def __del__(self) -> None:  # pragma: no cover - best effort cleanup
        self._stop_gamepad_discovery()

    def _first_connected_gamepad_id(self) -> Optional[int]:
        try:
            connected = self._gamepad_manager.connectedGamepads()
        except Exception:
            return None
        return connected[0] if connected else None

    def _on_connected_gamepads_changed(self) -> None:
        if not self._bind_first_gamepad():
            self._restart_bind_timer()
            self._fallback_to_keyboard_navigation()

    def _fallback_to_keyboard_navigation(self) -> None:
        try:
            self.dialog.setFocus(self.QtCore.Qt.FocusReason.ActiveWindowFocusReason)
        except Exception:
            pass

    def _warn_gamepad_unavailable(self, message: str) -> None:
        try:
            sys.stderr.write(f"[ap-bizhelper] {message}\n")
        except Exception:
            pass
        try:
            self.QtWidgets.QMessageBox.warning(self.dialog, "Gamepad unavailable", message)
        except Exception:
            pass

    def _find_sidebar(self) -> Optional["QtWidgets.QWidget"]:
        return self.dialog.findChild(self.QtWidgets.QWidget, "sidebar")

    def _find_file_view(self) -> Optional["QtWidgets.QWidget"]:
        for name in ("listView", "treeView"):
            found = self.dialog.findChild(self.QtWidgets.QWidget, name)
            if found is not None:
                return found
        return None

    def _focus_widget(self, widget: Optional["QtWidgets.QWidget"]) -> None:
        if widget is None:
            return
        widget.setFocus(self.QtCore.Qt.FocusReason.OtherFocusReason)

    def _send_key(self, key: int, *, modifiers: "QtCore.Qt.KeyboardModifiers" = None) -> None:
        if modifiers is None:
            modifiers = self.QtCore.Qt.KeyboardModifier.NoModifier
        target = self.dialog.focusWidget() or self.dialog
        press = self.QtGui.QKeyEvent(self.QtCore.QEvent.KeyPress, key, modifiers)
        release = self.QtGui.QKeyEvent(self.QtCore.QEvent.KeyRelease, key, modifiers)
        self.QtWidgets.QApplication.postEvent(target, press)
        self.QtWidgets.QApplication.postEvent(target, release)

    def _handle_axis(self, value: float, positive: str, negative: str, pos_key: int, neg_key: int) -> None:
        if value > self.AXIS_THRESHOLD:
            if not self._axis_state[positive]:
                self._axis_state[positive] = True
                self._axis_state[negative] = False
                self._send_key(pos_key)
        elif value < -self.AXIS_THRESHOLD:
            if not self._axis_state[negative]:
                self._axis_state[negative] = True
                self._axis_state[positive] = False
                self._send_key(neg_key)
        else:
            self._axis_state[positive] = False
            self._axis_state[negative] = False

    def _on_accept(self, pressed: bool) -> None:
        if pressed:
            self._send_key(self.QtCore.Qt.Key_Return)

    def _on_cancel(self, pressed: bool) -> None:
        if pressed:
            self._send_key(self.QtCore.Qt.Key_Escape)

    def _on_back(self, pressed: bool) -> None:
        if pressed:
            self._send_key(self.QtCore.Qt.Key_Left, modifiers=self.QtCore.Qt.KeyboardModifier.AltModifier)

    def _on_forward(self, pressed: bool) -> None:
        if pressed:
            self._send_key(self.QtCore.Qt.Key_Right, modifiers=self.QtCore.Qt.KeyboardModifier.AltModifier)

    def _on_up(self, pressed: bool) -> None:
        if pressed:
            self._send_key(self.QtCore.Qt.Key_Up, modifiers=self.QtCore.Qt.KeyboardModifier.AltModifier)

    def _on_context_menu(self, pressed: bool) -> None:
        if pressed:
            self._send_key(self.QtCore.Qt.Key_Menu)

    def _on_left(self, pressed: bool) -> None:
        if pressed:
            if self.dialog.focusWidget() is not self.sidebar_view:
                self._focus_widget(self.sidebar_view)
            else:
                self._send_key(self.QtCore.Qt.Key_Left)

    def _on_right(self, pressed: bool) -> None:
        if pressed:
            if self.dialog.focusWidget() is not self.file_view:
                self._focus_widget(self.file_view)
            else:
                self._send_key(self.QtCore.Qt.Key_Right)

    def _on_up_press(self, pressed: bool) -> None:
        if pressed:
            self._send_key(self.QtCore.Qt.Key_Up)

    def _on_down(self, pressed: bool) -> None:
        if pressed:
            self._send_key(self.QtCore.Qt.Key_Down)

    def _on_axis_x(self, value: float) -> None:
        self._handle_axis(
            value,
            positive="right",
            negative="left",
            pos_key=self.QtCore.Qt.Key_Right,
            neg_key=self.QtCore.Qt.Key_Left,
        )

    def _on_axis_y(self, value: float) -> None:
        self._handle_axis(
            -value,
            positive="down",
            negative="up",
            pos_key=self.QtCore.Qt.Key_Down,
            neg_key=self.QtCore.Qt.Key_Up,
        )


def _ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_settings() -> Dict[str, Any]:
    return {**_DEFAULT_SETTINGS, **_load_shared_settings()}


def _save_settings(settings: Dict[str, Any]) -> None:
    _ensure_dirs()
    merged_settings = {**_DEFAULT_SETTINGS, **settings}
    _save_shared_settings(merged_settings)


def _has_qt_dialogs() -> bool:
    global _QT_IMPORT_ERROR

    try:
        from PySide6 import QtWidgets  # noqa: F401
    except Exception as exc:
        _QT_IMPORT_ERROR = exc
        return False

    return True


def _has_qt_gamepad() -> bool:
    try:
        from PySide6 import QtGamepad  # noqa: F401
    except Exception:
        return False

    return True


def _coerce_font_setting(
    settings: Dict[str, Any], key: str, default: float, *, minimum: Optional[float] = None
) -> float:
    value = settings.get(key, default)
    try:
        numeric_value = float(value)
    except Exception:
        return default
    if minimum is not None:
        numeric_value = max(numeric_value, minimum)
    return numeric_value


def _coerce_int_setting(
    settings: Dict[str, Any], key: str, default: int, *, minimum: Optional[int] = None
) -> int:
    value = settings.get(key, default)
    try:
        numeric_value = int(value)
    except Exception:
        return default
    if minimum is not None:
        numeric_value = max(numeric_value, minimum)
    return numeric_value


def _coerce_bool_setting(settings: Dict[str, Any], key: str, default: bool) -> bool:
    value = settings.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _detect_global_scale() -> float:
    try:
        from PySide6 import QtGui
    except Exception:
        return 1.0

    try:
        screen = QtGui.QGuiApplication.primaryScreen()
    except Exception:
        return 1.0

    if screen is None:
        return 1.0

    dpi_scale = 1.0
    try:
        logical_dpi = float(screen.logicalDotsPerInch())
        if logical_dpi > 0:
            dpi_scale = logical_dpi / 96.0
    except Exception:
        dpi_scale = 1.0

    try:
        pixel_ratio = float(screen.devicePixelRatio())
        if pixel_ratio > 0:
            dpi_scale = max(dpi_scale, pixel_ratio)
    except Exception:
        pass

    return max(dpi_scale, 0.1)


def _ensure_qt_app(settings: Optional[Dict[str, Any]] = None) -> "QtWidgets.QApplication":
    global _QT_APP

    from PySide6 import QtGui, QtWidgets

    def _scaled_font(
        font: "QtGui.QFont",
        scale: float,
        *,
        min_point_size: Optional[int] = None,
        min_pixel_size: Optional[int] = None,
        fallback_point_size: Optional[int] = None,
    ) -> "QtGui.QFont":
        scaled_font = QtGui.QFont(font)
        if font.pointSize() > 0:
            new_size = int(font.pointSize() * scale)
            if min_point_size is not None:
                new_size = max(new_size, min_point_size)
            scaled_font.setPointSize(new_size)
        elif font.pixelSize() > 0:
            new_size = int(font.pixelSize() * scale)
            if min_pixel_size is not None:
                new_size = max(new_size, min_pixel_size)
            scaled_font.setPixelSize(new_size)
        elif fallback_point_size is not None:
            scaled_font.setPointSize(fallback_point_size)
        return scaled_font

    app = _QT_APP or QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(sys.argv[:1] or ["ap-bizhelper"])

    settings_obj = {**_DEFAULT_SETTINGS, **(settings or _load_settings())}
    font_scale = _coerce_font_setting(settings_obj, "QT_FONT_SCALE", _QT_FONT_SCALE, minimum=0.1)
    global_scale = _detect_global_scale()
    normalized_font_scale = font_scale / global_scale
    min_point_size = _coerce_font_setting(
        settings_obj, "QT_MIN_POINT_SIZE", _QT_MIN_POINT_SIZE, minimum=1
    )
    font: QtGui.QFont = app.font()
    min_scaled_point_size = int(min_point_size * normalized_font_scale)
    scaled_font = _scaled_font(
        font,
        normalized_font_scale,
        min_point_size=min_scaled_point_size,
        min_pixel_size=min_scaled_point_size,
        fallback_point_size=min_scaled_point_size,
    )
    app.setFont(scaled_font)

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


def _zenity_error_dialog(message: str) -> None:
    """Attempt to show a zenity error dialog with detailed text."""

    if _has_zenity():
        _run_zenity(["--error", f"--text={message}"])
    else:
        sys.stderr.write("ERROR: " + message + "\n")


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


def _preferred_start_dir(initial: Optional[Path], settings: Dict[str, Any]) -> Path:
    last_dir_setting = str(settings.get("LAST_FILE_DIALOG_DIR", "") or "")
    candidates = [
        initial,
        Path(last_dir_setting) if last_dir_setting else None,
        DOWNLOADS_DIR if DOWNLOADS_DIR.exists() else None,
        Path.home(),
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        candidate_path = candidate.expanduser()
        if candidate_path.is_file():
            candidate_path = candidate_path.parent
        if candidate_path.exists():
            return candidate_path
    return Path.home()


def _sidebar_urls() -> list["QtCore.QUrl"]:
    from PySide6 import QtCore

    common_dirs = [
        Path.home(),
        DOWNLOADS_DIR,
        Path(os.path.expanduser("~/Documents")),
        Path(os.path.expanduser("~/Desktop")),
        Path(os.path.expanduser("~/Music")),
        Path(os.path.expanduser("~/Pictures")),
        Path(os.path.expanduser("~/Videos")),
    ]
    return [
        QtCore.QUrl.fromLocalFile(str(path)) for path in common_dirs if path.expanduser().exists()
    ]


def _configure_file_view_columns(
    dialog: "QtWidgets.QFileDialog", settings_obj: Dict[str, Any]
) -> None:
    from PySide6 import QtCore, QtWidgets

    tree_view = dialog.findChild(QtWidgets.QTreeView, "treeView")
    if tree_view is None:
        return

    model = tree_view.model()
    header = tree_view.header()
    if model is None or header is None:
        return

    column_count = model.columnCount()
    if column_count <= 0:
        return

    label_to_index: dict[str, int] = {}
    for idx in range(column_count):
        try:
            label = str(
                model.headerData(idx, QtCore.Qt.Horizontal, QtCore.Qt.DisplayRole) or ""
            ).strip()
        except Exception:
            label = ""
        if label:
            label_to_index[label.lower()] = idx

    desired_columns = [
        ("name", "QT_FILE_DIALOG_NAME_WIDTH"),
        ("size", "QT_FILE_DIALOG_SIZE_WIDTH"),
        ("type", "QT_FILE_DIALOG_TYPE_WIDTH"),
        ("date modified", "QT_FILE_DIALOG_DATE_WIDTH"),
    ]
    fallback_indices = {"name": 0, "size": 1, "type": 2, "date modified": 3}

    updated_settings = False
    for label, setting_key in desired_columns:
        index = label_to_index.get(label, fallback_indices.get(label))
        if index is None or index >= column_count:
            continue

        try:
            base_width = tree_view.sizeHintForColumn(index)
        except Exception:
            base_width = 0
        if base_width <= 0:
            try:
                base_width = header.sectionSizeHint(index)
            except Exception:
                base_width = 0
        if base_width <= 0:
            try:
                base_width = header.defaultSectionSize()
            except Exception:
                base_width = 0

        configured_width = _coerce_int_setting(
            settings_obj, setting_key, 0, minimum=0
        )
        target_width = configured_width or int(base_width * _QT_FILE_DIALOG_COLUMN_SCALE)
        if target_width > 0:
            try:
                header.setSectionResizeMode(index, QtWidgets.QHeaderView.Interactive)
            except Exception:
                pass
            header.resizeSection(index, target_width)
            try:
                header.setSectionResizeMode(index, QtWidgets.QHeaderView.Interactive)
            except Exception:
                pass
            if configured_width <= 0:
                settings_obj[setting_key] = target_width
                updated_settings = True

    if updated_settings:
        _save_settings(settings_obj)


def _qt_file_dialog(
    *,
    title: str,
    start_dir: Path,
    file_filter: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Optional[Path]:
    from PySide6 import QtCore, QtGui, QtWidgets

    settings_obj = {**_DEFAULT_SETTINGS, **(settings or {})}
    _ensure_qt_app(settings_obj)
    global_scale = _detect_global_scale()
    filter_text = file_filter or "All Files (*)"
    dialog = QtWidgets.QFileDialog()
    dialog.setWindowTitle(title)
    dialog.setAcceptMode(QtWidgets.QFileDialog.AcceptOpen)
    dialog.setFileMode(QtWidgets.QFileDialog.ExistingFile)
    dialog.setDirectory(str(start_dir))
    dialog.setNameFilter(filter_text)
    dialog.setViewMode(QtWidgets.QFileDialog.Detail)
    dialog.setOption(QtWidgets.QFileDialog.DontUseNativeDialog, True)
    dialog.setOption(QtWidgets.QFileDialog.ReadOnly, False)
    width = _coerce_int_setting(
        settings_obj, "QT_FILE_DIALOG_WIDTH", _QT_FILE_DIALOG_WIDTH, minimum=0
    )
    height = _coerce_int_setting(
        settings_obj, "QT_FILE_DIALOG_HEIGHT", _QT_FILE_DIALOG_HEIGHT, minimum=0
    )
    if width > 0 and height > 0:
        dialog.resize(width, height)
    if hasattr(QtGui.QGuiApplication, "setNavigationMode") and hasattr(
        QtCore.Qt, "NavigationModeKeypadDirectional"
    ):
        QtGui.QGuiApplication.setNavigationMode(
            QtCore.Qt.NavigationModeKeypadDirectional
        )

    def _scale_file_name_font(widget: "QtWidgets.QWidget") -> None:
        base_font = widget.font()
        scaled_font = QtGui.QFont(base_font)
        name_font_scale = _coerce_font_setting(
            settings_obj, "QT_FILE_NAME_FONT_SCALE", _QT_FILE_NAME_FONT_SCALE, minimum=0.1
        )
        effective_name_font_scale = name_font_scale / global_scale
        if base_font.pointSize() > 0:
            scaled_font.setPointSize(
                int(
                    base_font.pointSize()
                    * effective_name_font_scale
                )
            )
        elif base_font.pixelSize() > 0:
            scaled_font.setPixelSize(
                int(
                    base_font.pixelSize()
                    * effective_name_font_scale
                )
            )
        widget.setFont(scaled_font)

    for view_name in ("listView", "treeView"):
        file_view = dialog.findChild(QtWidgets.QWidget, view_name)
        if file_view is not None:
            _scale_file_name_font(file_view)

    sidebar_urls = _sidebar_urls()
    if sidebar_urls:
        dialog.setSidebarUrls(sidebar_urls)
    if _coerce_bool_setting(
        settings_obj, "QT_FILE_DIALOG_MAXIMIZE", _QT_FILE_DIALOG_MAXIMIZE
    ):
        dialog.setWindowState(dialog.windowState() | QtCore.Qt.WindowMaximized)
    _configure_file_view_columns(dialog, settings_obj)
    dialog.activateWindow()
    dialog.raise_()
    dialog.setFocus(QtCore.Qt.FocusReason.ActiveWindowFocusReason)
    steam_launch = bool(os.environ.get("SteamGameId"))
    enable_gamepad = bool(settings_obj.get("ENABLE_GAMEPAD_FILE_DIALOG", True)) or steam_launch
    if enable_gamepad and _has_qt_gamepad():
        try:
            GamepadFileDialogController(dialog)
        except Exception:
            pass
    if dialog.exec() == QtWidgets.QDialog.Accepted:
        selected_files = dialog.selectedFiles()
        if selected_files:
            return Path(selected_files[0])
    return None


def _select_file_dialog(
    *,
    title: str,
    initial: Optional[Path] = None,
    file_filter: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Optional[Path]:
    if not _has_qt_dialogs():
        details = ""
        if _QT_IMPORT_ERROR is not None:
            details = f"\nDetails: {_QT_IMPORT_ERROR}"

        _zenity_error_dialog(
            "PySide6 is required for file selection but is not installed or failed to load.\n"
            "Please install PySide6 (e.g. `pip install PySide6`) and try again."
            f"{details}"
        )
        return None

    settings_obj = settings if settings is not None else _load_settings()
    start_dir = _preferred_start_dir(initial, settings_obj)

    try:
        selection = _qt_file_dialog(
            title=title, start_dir=start_dir, file_filter=file_filter, settings=settings_obj
        )
    except Exception as exc:  # pragma: no cover - GUI/runtime issues
        _zenity_error_dialog(f"PySide6 file selection failed: {exc}")
        return None

    if selection:
        settings_obj["LAST_FILE_DIALOG_DIR"] = str(selection.parent)
        if settings is None:
            _save_settings(settings_obj)

    return selection


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


def select_appimage(
    initial: Optional[Path] = None, *, settings: Optional[Dict[str, Any]] = None
) -> Optional[Path]:
    selection = _select_file_dialog(
        title="Select Archipelago AppImage", initial=initial, settings=settings
    )
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


def _prompt_select_existing_appimage(initial: Path, *, settings: Dict[str, Any]) -> Path:
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

    chosen = select_appimage(initial, settings=settings)
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
            app_path = _prompt_select_existing_appimage(
                Path(os.path.expanduser("~")), settings=settings
            )
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
