from __future__ import annotations

"""Shared dialog helpers built on PySide6.

This module centralizes dialog rendering so the main app, shims, and helpers can
reuse consistent widgets. Callers can opt into persistent directory tracking by
passing load/save callbacks for file dialogs.
"""

import importlib.util
import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .ap_bizhelper_config import load_settings as _load_shared_settings, save_settings as _save_shared_settings
from .logging_utils import AppLogger, get_app_logger

DOWNLOADS_DIR = Path(os.path.expanduser("~/Downloads"))
DIALOG_DEFAULTS = {
    "QT_FONT_SCALE": 1.5,
    "QT_MIN_POINT_SIZE": 12,
    "QT_FILE_NAME_FONT_SCALE": 1.8,
    "QT_FILE_DIALOG_WIDTH": 1280,
    "QT_FILE_DIALOG_HEIGHT": 800,
    "QT_FILE_DIALOG_MAXIMIZE": True,
    "QT_FILE_DIALOG_NAME_WIDTH": 850,
    "QT_FILE_DIALOG_TYPE_WIDTH": 300,
    "QT_FILE_DIALOG_SIZE_WIDTH": 300,
    "QT_FILE_DIALOG_DATE_WIDTH": 0,
    "QT_FILE_DIALOG_SIDEBAR_WIDTH": 400,
}

_QT_APP: Optional["QtWidgets.QApplication"] = None
_QT_BASE_FONT: Optional["QtGui.QFont"] = None
_QT_IMPORT_ERROR: Optional[BaseException] = None


def merge_dialog_settings(settings: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    return {**DIALOG_DEFAULTS, **(settings or {})}


def _coerce_font_setting(
    settings: Dict[str, object], key: str, default: float, *, minimum: Optional[float] = None
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
    settings: Dict[str, object], key: str, default: int, *, minimum: Optional[int] = None
) -> int:
    value = settings.get(key, default)
    try:
        numeric_value = int(value)
    except Exception:
        return default
    if minimum is not None:
        numeric_value = max(numeric_value, minimum)
    return numeric_value


def _coerce_bool_setting(settings: Dict[str, object], key: str, default: bool) -> bool:
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


def ensure_qt_available() -> None:
    global _QT_IMPORT_ERROR

    if _QT_IMPORT_ERROR is not None:
        raise RuntimeError("PySide6 is required for ap-bizhelper") from _QT_IMPORT_ERROR

    try:
        from PySide6 import QtWidgets  # noqa: F401
    except Exception as exc:  # pragma: no cover - import guard
        _QT_IMPORT_ERROR = exc
        raise RuntimeError("PySide6 is required for ap-bizhelper") from exc


def _detect_global_scale() -> float:
    ensure_qt_available()
    from PySide6 import QtGui

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


def ensure_qt_app(settings: Optional[Dict[str, object]] = None) -> "QtWidgets.QApplication":
    global _QT_APP, _QT_BASE_FONT

    ensure_qt_available()
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

    if _QT_APP is not None:
        return _QT_APP

    try:
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    except Exception as exc:  # pragma: no cover - runtime guard
        raise RuntimeError("PySide6 application could not be created") from exc

    if _QT_BASE_FONT is None:
        _QT_BASE_FONT = app.font()

    settings_obj = merge_dialog_settings(settings)
    font_scale = _coerce_font_setting(settings_obj, "QT_FONT_SCALE", float(DIALOG_DEFAULTS["QT_FONT_SCALE"]), minimum=0.1)
    global_scale = _detect_global_scale()
    normalized_font_scale = font_scale / global_scale
    min_point_size = _coerce_font_setting(
        settings_obj, "QT_MIN_POINT_SIZE", float(DIALOG_DEFAULTS["QT_MIN_POINT_SIZE"]), minimum=1
    )
    font: QtGui.QFont = QtGui.QFont(_QT_BASE_FONT) if _QT_BASE_FONT else app.font()
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


def enable_dialog_gamepad(
    dialog: "QtWidgets.QDialog | QtWidgets.QMessageBox",
    *,
    affirmative: Optional["QtWidgets.QAbstractButton"] = None,
    negative: Optional["QtWidgets.QAbstractButton"] = None,
    special: Optional["QtWidgets.QAbstractButton"] = None,
    default: Optional["QtWidgets.QAbstractButton"] = None,
) -> Optional["object"]:
    """Attach controller navigation to ``dialog`` if available."""

    try:
        from . import gamepad_input

        layer = gamepad_input.install_gamepad_navigation(
            dialog,
            actions={
                "affirmative": affirmative,
                "negative": negative,
                "special": special,
                "default": default,
            },
        )
        if layer is not None:
            dialog.finished.connect(layer.shutdown)  # type: ignore[attr-defined]
        return layer
    except Exception:  # pragma: no cover - optional dependency
        return None


def _sidebar_urls() -> list["QtCore.QUrl"]:
    ensure_qt_available()
    from PySide6 import QtCore

    paths = [
        Path(os.path.expanduser("~")),
        DOWNLOADS_DIR,
        Path("/"),
    ]
    urls = []
    for p in paths:
        if p.exists():
            try:
                urls.append(QtCore.QUrl.fromLocalFile(p.as_posix()))
            except Exception:
                continue
    return urls


def _widen_file_dialog_sidebar(dialog: "QtWidgets.QFileDialog", settings: Dict[str, object]) -> None:
    try:
        from PySide6 import QtWidgets

        sidebar = dialog.findChild(QtWidgets.QListView, "sidebar")
        if sidebar is None:
            return
        width = _coerce_int_setting(
            settings, "QT_FILE_DIALOG_SIDEBAR_WIDTH", int(DIALOG_DEFAULTS["QT_FILE_DIALOG_SIDEBAR_WIDTH"]), minimum=0
        )
        if width > 0:
            sidebar.setFixedWidth(width)
    except Exception:
        return


def _configure_file_view_columns(dialog: "QtWidgets.QFileDialog", settings: Dict[str, object]) -> None:
    try:
        from PySide6 import QtCore, QtWidgets

        tree_view = dialog.findChild(QtWidgets.QTreeView, "treeView")
        if tree_view is None:
            return
        header: Optional["QtWidgets.QHeaderView"] = tree_view.header()
        if header is None:
            return
        dialog.setLabelText(QtWidgets.QFileDialog.LookIn, "Look in:")
        name_width = _coerce_int_setting(
            settings, "QT_FILE_DIALOG_NAME_WIDTH", int(DIALOG_DEFAULTS["QT_FILE_DIALOG_NAME_WIDTH"]), minimum=0
        )
        if name_width > 0:
            header.resizeSection(0, name_width)
        type_width = _coerce_int_setting(
            settings, "QT_FILE_DIALOG_TYPE_WIDTH", int(DIALOG_DEFAULTS["QT_FILE_DIALOG_TYPE_WIDTH"]), minimum=0
        )
        if type_width > 0:
            header.resizeSection(1, type_width)
        size_width = _coerce_int_setting(
            settings, "QT_FILE_DIALOG_SIZE_WIDTH", int(DIALOG_DEFAULTS["QT_FILE_DIALOG_SIZE_WIDTH"]), minimum=0
        )
        if size_width > 0:
            header.resizeSection(2, size_width)
        date_width = _coerce_int_setting(
            settings, "QT_FILE_DIALOG_DATE_WIDTH", int(DIALOG_DEFAULTS["QT_FILE_DIALOG_DATE_WIDTH"]), minimum=0
        )
        if date_width > 0:
            header.resizeSection(3, date_width)
        header.setStretchLastSection(True)
        try:
            header.setSectionResizeMode(QtWidgets.QHeaderView.Fixed)
            header.setSectionResizeMode(0, QtWidgets.QHeaderView.Interactive)
        except Exception:
            pass
        try:
            tree_view.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        except Exception:
            pass
        try:
            tree_view.setFocus(QtCore.Qt.FocusReason.ActiveWindowFocusReason)
        except Exception:
            pass
    except Exception:
        return


def _focus_file_view(dialog: "QtWidgets.QFileDialog") -> None:
    try:
        from PySide6 import QtCore, QtWidgets

        tree_view = dialog.findChild(QtWidgets.QTreeView, "treeView")
        if tree_view is None:
            return
        model = tree_view.model()
        selection_model = tree_view.selectionModel()
        if not model or not selection_model:
            return
        current_index = tree_view.currentIndex()
        if not current_index.isValid() and model.rowCount() > 0:
            first_index = model.index(0, 0)
            if first_index.isValid():
                tree_view.setCurrentIndex(first_index)
                selection_model.select(
                    first_index,
                    QtCore.QItemSelectionModel.Select | QtCore.QItemSelectionModel.Rows,
                )
        tree_view.setFocus(QtCore.Qt.FocusReason.ActiveWindowFocusReason)
    except Exception:
        return


def question_dialog(
    *, title: str, text: str, ok_label: str, cancel_label: str, extra_label: Optional[str] = None
) -> str:
    from PySide6 import QtCore, QtWidgets

    ensure_qt_app()
    dialog = QtWidgets.QDialog()
    dialog.setWindowTitle(title)
    layout = QtWidgets.QVBoxLayout(dialog)

    label = QtWidgets.QLabel(text)
    label.setWordWrap(True)
    layout.addWidget(label)

    button_row = QtWidgets.QHBoxLayout()
    button_row.addStretch()
    ok_button = QtWidgets.QPushButton(ok_label)
    ok_button.setDefault(True)
    button_row.addWidget(ok_button)
    extra_button = None
    if extra_label:
        extra_button = QtWidgets.QPushButton(extra_label)
        button_row.addWidget(extra_button)
    cancel_button = QtWidgets.QPushButton(cancel_label)
    button_row.addWidget(cancel_button)
    button_row.addStretch()
    layout.addLayout(button_row)

    ok_button.clicked.connect(dialog.accept)
    cancel_button.clicked.connect(dialog.reject)
    extra_result = QtWidgets.QDialog.Accepted + 1
    if extra_button is not None:
        extra_button.clicked.connect(lambda: dialog.done(extra_result))

    dialog.setLayout(layout)
    dialog.setFocus(QtCore.Qt.FocusReason.ActiveWindowFocusReason)
    enable_dialog_gamepad(
        dialog,
        affirmative=ok_button,
        negative=cancel_button,
        special=extra_button,
        default=ok_button,
    )

    result = dialog.exec()
    if result == QtWidgets.QDialog.Accepted:
        return "ok"
    if result == extra_result:
        return "extra"
    return "cancel"


def info_dialog(message: str, *, title: str = "Information", logger: Optional[AppLogger] = None) -> None:
    ensure_qt_available()
    app_logger = logger or get_app_logger()
    app_logger.log_dialog(title, message, backend="qt", location="info-dialog")
    from PySide6 import QtWidgets

    ensure_qt_app()
    box = QtWidgets.QMessageBox()
    box.setIcon(QtWidgets.QMessageBox.Information)
    box.setWindowTitle(title)
    box.setText(message)
    ok_button = box.addButton(QtWidgets.QMessageBox.Ok)
    box.setDefaultButton(QtWidgets.QMessageBox.Ok)
    enable_dialog_gamepad(
        box, affirmative=ok_button, negative=ok_button, default=ok_button
    )
    box.exec()


def error_dialog(message: str, *, title: str = "Error", logger: Optional[AppLogger] = None) -> None:
    ensure_qt_available()
    app_logger = logger or get_app_logger()
    app_logger.log_dialog(title, message, level="ERROR", backend="qt", location="error-dialog")
    from PySide6 import QtWidgets

    ensure_qt_app()
    box = QtWidgets.QMessageBox()
    box.setIcon(QtWidgets.QMessageBox.Critical)
    box.setWindowTitle(title)
    box.setText(message)
    ok_button = box.addButton(QtWidgets.QMessageBox.Ok)
    box.setDefaultButton(QtWidgets.QMessageBox.Ok)
    enable_dialog_gamepad(
        box, affirmative=ok_button, negative=ok_button, default=ok_button
    )
    box.exec()


def preferred_start_dir(initial: Optional[Path], settings: Dict[str, object], dialog_key: str) -> Path:
    last_dir_setting = str(settings.get("LAST_FILE_DIALOG_DIR", "") or "")
    per_dialog_dir = str(settings.get("LAST_FILE_DIALOG_DIRS", {}).get(dialog_key, "") or "")

    candidates = [
        initial if initial and initial.expanduser() != Path.home() else None,
        Path(per_dialog_dir) if per_dialog_dir else None,
        Path(last_dir_setting) if last_dir_setting else None,
        DOWNLOADS_DIR if DOWNLOADS_DIR.exists() else None,
        initial if initial else None,
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


def remember_file_dialog_dir(settings: Dict[str, object], selection: Path, dialog_key: str) -> None:
    parent = selection.parent if selection.is_file() else selection
    dialog_dirs = settings.get("LAST_FILE_DIALOG_DIRS", {})
    dialog_dirs[dialog_key] = str(parent)
    settings["LAST_FILE_DIALOG_DIRS"] = dialog_dirs
    settings["LAST_FILE_DIALOG_DIR"] = str(parent)


def file_dialog(
    *,
    title: str,
    start_dir: Path,
    file_filter: Optional[str] = None,
    settings: Optional[Dict[str, object]] = None,
) -> Optional[Path]:
    from PySide6 import QtCore, QtGui, QtWidgets

    settings_obj = merge_dialog_settings(settings)
    ensure_qt_app(settings_obj)
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
        settings_obj, "QT_FILE_DIALOG_WIDTH", int(DIALOG_DEFAULTS["QT_FILE_DIALOG_WIDTH"]), minimum=0
    )
    height = _coerce_int_setting(
        settings_obj, "QT_FILE_DIALOG_HEIGHT", int(DIALOG_DEFAULTS["QT_FILE_DIALOG_HEIGHT"]), minimum=0
    )
    if width > 0 and height > 0:
        dialog.resize(width, height)
    if hasattr(QtGui.QGuiApplication, "setNavigationMode") and hasattr(QtCore.Qt, "NavigationModeKeypadDirectional"):
        QtGui.QGuiApplication.setNavigationMode(
            QtCore.Qt.NavigationModeKeypadDirectional
        )

    def _scale_file_name_font(widget: "QtWidgets.QWidget") -> None:
        base_font = widget.font()
        scaled_font = QtGui.QFont(base_font)
        name_font_scale = _coerce_font_setting(
            settings_obj, "QT_FILE_NAME_FONT_SCALE", float(DIALOG_DEFAULTS["QT_FILE_NAME_FONT_SCALE"]), minimum=0.1
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
    _widen_file_dialog_sidebar(dialog, settings_obj)
    if _coerce_bool_setting(
        settings_obj, "QT_FILE_DIALOG_MAXIMIZE", bool(DIALOG_DEFAULTS["QT_FILE_DIALOG_MAXIMIZE"])
    ):
        dialog.setWindowState(dialog.windowState() | QtCore.Qt.WindowMaximized)
    _configure_file_view_columns(dialog, settings_obj)
    _focus_file_view(dialog)
    try:
        QtCore.QTimer.singleShot(0, lambda: _focus_file_view(dialog))
    except Exception:
        pass
    dialog.activateWindow()
    dialog.raise_()
    dialog.setFocus(QtCore.Qt.FocusReason.ActiveWindowFocusReason)
    gamepad_layer = enable_dialog_gamepad(dialog)
    if dialog.exec() == QtWidgets.QDialog.Accepted:
        selected_files = dialog.selectedFiles()
        if selected_files:
            if gamepad_layer is not None:
                gamepad_layer.shutdown()
            return Path(selected_files[0])
    if gamepad_layer is not None:
        gamepad_layer.shutdown()
    return None


def select_file_dialog(
    *,
    title: str,
    dialog_key: str,
    initial: Optional[Path] = None,
    file_filter: Optional[str] = None,
    settings: Optional[Dict[str, object]] = None,
    save_settings: bool = True,
) -> Optional[Path]:
    settings_obj = merge_dialog_settings(settings or _load_shared_settings())
    start_dir = preferred_start_dir(initial, settings_obj, dialog_key)

    selection = file_dialog(
        title=title, start_dir=start_dir, file_filter=file_filter, settings=settings_obj
    )

    if selection:
        remember_file_dialog_dir(settings_obj, selection, dialog_key)
        if save_settings:
            _save_shared_settings(settings_obj)

    return selection


def checklist_dialog(
    title: str,
    text: Optional[str],
    items: Iterable[Tuple[bool, str]],
    *,
    ok_label: str = "OK",
    cancel_label: str = "Cancel",
    height: Optional[int] = None,
) -> Optional[List[str]]:
    from PySide6 import QtCore, QtWidgets

    ensure_qt_app()
    dialog = QtWidgets.QDialog()
    dialog.setWindowTitle(title or "Select items")
    if height:
        try:
            dialog.resize(dialog.width(), int(height))
        except ValueError:
            pass

    layout = QtWidgets.QVBoxLayout(dialog)
    if text:
        label = QtWidgets.QLabel(text)
        label.setWordWrap(True)
        layout.addWidget(label)

    scroll = QtWidgets.QScrollArea()
    scroll.setWidgetResizable(True)
    container = QtWidgets.QWidget()
    container_layout = QtWidgets.QVBoxLayout(container)

    checkboxes: List[QtWidgets.QCheckBox] = []
    for checked, label_text in items:
        cb = QtWidgets.QCheckBox(label_text)
        cb.setChecked(checked)
        container_layout.addWidget(cb)
        checkboxes.append(cb)
    container_layout.addStretch()
    scroll.setWidget(container)
    layout.addWidget(scroll)

    button_row = QtWidgets.QHBoxLayout()
    button_row.addStretch()
    ok_button = QtWidgets.QPushButton(ok_label)
    ok_button.setDefault(True)
    button_row.addWidget(ok_button)
    cancel_button = QtWidgets.QPushButton(cancel_label)
    button_row.addWidget(cancel_button)
    button_row.addStretch()
    layout.addLayout(button_row)

    def _prime_focus() -> None:
        ok_button.setFocus(QtCore.Qt.FocusReason.ActiveWindowFocusReason)
        ok_button.pressed.disconnect(_prime_focus)
        cancel_button.pressed.disconnect(_prime_focus)

    ok_button.pressed.connect(_prime_focus)
    cancel_button.pressed.connect(_prime_focus)
    ok_button.clicked.connect(dialog.accept)
    cancel_button.clicked.connect(dialog.reject)

    enable_dialog_gamepad(
        dialog, affirmative=ok_button, negative=cancel_button, default=ok_button
    )
    dialog.setLayout(layout)
    dialog.setFocusProxy(ok_button)
    QtCore.QTimer.singleShot(
        0, lambda: ok_button.setFocus(QtCore.Qt.FocusReason.ActiveWindowFocusReason)
    )

    result = dialog.exec()
    if result != QtWidgets.QDialog.Accepted:
        return None

    return [cb.text() for cb in checkboxes if cb.isChecked()]


def progress_dialog_from_stream(
    title: str, text: str, stream: Iterable[str], *, cancel_label: str = "Cancel"
) -> int:
    from PySide6 import QtWidgets

    ensure_qt_app()
    dialog = QtWidgets.QProgressDialog()
    dialog.setWindowTitle(title or "Progress")
    dialog.setLabelText(text)
    dialog.setCancelButtonText(cancel_label)
    dialog.setRange(0, 100)
    dialog.setValue(0)
    dialog.setAutoClose(True)
    dialog.setAutoReset(True)
    dialog.show()

    cancel_button = dialog.findChild(QtWidgets.QPushButton)
    enable_dialog_gamepad(
        dialog,
        affirmative=cancel_button,
        negative=cancel_button,
        default=cancel_button,
    )

    app = QtWidgets.QApplication.instance()
    if app is None:
        return 1

    def is_cancelled() -> bool:
        app.processEvents()
        return dialog.wasCanceled()

    for line in stream:
        if is_cancelled():
            dialog.cancel()
            return 1
        value = line.strip()
        if not value:
            continue
        try:
            percent = int(float(value))
        except ValueError:
            continue
        dialog.setValue(max(0, min(100, percent)))
        if is_cancelled():
            dialog.cancel()
            return 1
    dialog.setValue(100)
    app.processEvents()
    if dialog.wasCanceled():
        dialog.cancel()
        return 1
    return 0
