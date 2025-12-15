#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .ap_bizhelper_config import (
    load_settings as _load_shared_settings,
    save_settings as _save_shared_settings,
)
from .logging_utils import get_app_logger

# Paths mirror the bash script and the config helper.
CONFIG_DIR = Path(os.path.expanduser("~/.config/ap_bizhelper"))
SETTINGS_FILE = CONFIG_DIR / "settings.json"
DATA_DIR = Path(os.path.expanduser("~/.local/share/ap_bizhelper"))
AP_APPIMAGE_DEFAULT = DATA_DIR / "Archipelago.AppImage"
DESKTOP_DIR = Path(os.path.expanduser("~/Desktop"))
DOWNLOADS_DIR = Path(os.path.expanduser("~/Downloads"))

APP_LOGGER = get_app_logger()

GITHUB_API_LATEST = "https://api.github.com/repos/ArchipelagoMW/Archipelago/releases/latest"

_QT_APP: Optional["QtWidgets.QApplication"] = None
_QT_BASE_FONT: Optional["QtGui.QFont"] = None
_QT_FONT_SCALE = 1.5
_QT_MIN_POINT_SIZE = 12
_QT_FILE_NAME_FONT_SCALE = 1.8
_QT_FILE_DIALOG_WIDTH = 1280
_QT_FILE_DIALOG_HEIGHT = 800
_QT_FILE_DIALOG_MAXIMIZE = True
_QT_FILE_DIALOG_NAME_WIDTH = 850
_QT_FILE_DIALOG_TYPE_WIDTH = 0
_QT_FILE_DIALOG_SIZE_WIDTH = 250
_QT_FILE_DIALOG_DATE_WIDTH = 300
_QT_FILE_DIALOG_SIDEBAR_WIDTH = 400
_QT_FILE_DIALOG_COLUMN_SCALE = 1.8
_QT_FILE_DIALOG_DEFAULT_SHRINK = 0.95
_QT_IMPORT_ERROR: Optional[BaseException] = None
_DEFAULT_SETTINGS = {
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
    "QT_FILE_DIALOG_SIDEBAR_WIDTH": _QT_FILE_DIALOG_SIDEBAR_WIDTH,
}


def _ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_settings() -> Dict[str, Any]:
    return {**_DEFAULT_SETTINGS, **_load_shared_settings()}


def _save_settings(settings: Dict[str, Any]) -> None:
    _ensure_dirs()
    merged_settings = {**_DEFAULT_SETTINGS, **settings}
    _save_shared_settings(merged_settings)


def _ensure_qt_available() -> None:
    global _QT_IMPORT_ERROR

    if _QT_IMPORT_ERROR is not None:
        APP_LOGGER.log(
            "PySide6 failed to load; runtime dialogs are unavailable.",
            level="ERROR",
            include_context=True,
            mirror_console=True,
            stream="stderr",
            location="qt-deps",
        )
        raise RuntimeError("PySide6 is required for ap-bizhelper") from _QT_IMPORT_ERROR

    try:
        from PySide6 import QtWidgets  # noqa: F401
    except Exception as exc:  # pragma: no cover - import guard
        _QT_IMPORT_ERROR = exc
        APP_LOGGER.log(
            f"PySide6 is required but could not be imported: {exc}",
            level="ERROR",
            include_context=True,
            mirror_console=True,
            stream="stderr",
            location="qt-deps",
        )
        raise RuntimeError("PySide6 is required for ap-bizhelper") from exc


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
    _ensure_qt_available()
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


def _ensure_qt_app(settings: Optional[Dict[str, Any]] = None) -> "QtWidgets.QApplication":
    global _QT_APP, _QT_BASE_FONT

    _ensure_qt_available()
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
        _QT_BASE_FONT = app.font()
    elif _QT_BASE_FONT is None:
        _QT_BASE_FONT = app.font()

    settings_obj = {**_DEFAULT_SETTINGS, **(settings or _load_settings())}
    font_scale = _coerce_font_setting(settings_obj, "QT_FONT_SCALE", _QT_FONT_SCALE, minimum=0.1)
    global_scale = _detect_global_scale()
    normalized_font_scale = font_scale / global_scale
    min_point_size = _coerce_font_setting(
        settings_obj, "QT_MIN_POINT_SIZE", _QT_MIN_POINT_SIZE, minimum=1
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


def _enable_dialog_gamepad(
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
    except Exception as exc:  # pragma: no cover - runtime guard
        APP_LOGGER.log(
            f"Failed to enable gamepad navigation: {exc}",
            level="WARNING",
            include_context=True,
            location="gamepad",
        )
    return None


def _qt_question_dialog(
    *, title: str, text: str, ok_label: str, cancel_label: str, extra_label: Optional[str] = None
) -> str:
    from PySide6 import QtCore, QtWidgets

    _ensure_qt_app()
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
    _enable_dialog_gamepad(
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


def _dialog_dir_map(settings: Dict[str, Any]) -> Dict[str, str]:
    stored = settings.get("LAST_FILE_DIALOG_DIRS", {})
    if isinstance(stored, dict):
        return stored
    return {}


def _preferred_start_dir(initial: Optional[Path], settings: Dict[str, Any], dialog_key: str) -> Path:
    last_dir_setting = str(settings.get("LAST_FILE_DIALOG_DIR", "") or "")
    per_dialog_dir = str(_dialog_dir_map(settings).get(dialog_key, "") or "")

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


def _remember_file_dialog_dir(settings: Dict[str, Any], selection: Path, dialog_key: str) -> None:
    parent = selection.parent if selection.is_file() else selection
    dialog_dirs = _dialog_dir_map(settings)
    dialog_dirs[dialog_key] = str(parent)
    settings["LAST_FILE_DIALOG_DIRS"] = dialog_dirs
    settings["LAST_FILE_DIALOG_DIR"] = str(parent)


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


def _widen_file_dialog_sidebar(
    dialog: "QtWidgets.QFileDialog", settings_obj: Dict[str, Any]
) -> None:
    from PySide6 import QtWidgets

    splitter = dialog.findChild(QtWidgets.QSplitter)
    if splitter is None:
        return
    try:
        sizes = splitter.sizes()
    except Exception:
        return
    if not sizes or len(sizes) < 2:
        return

    new_sizes = list(sizes)
    new_sizes[0] = max(
        1,
        _coerce_int_setting(
            settings_obj, "QT_FILE_DIALOG_SIDEBAR_WIDTH", _QT_FILE_DIALOG_SIDEBAR_WIDTH, minimum=1
        ),
    )
    try:
        splitter.setSizes(new_sizes)
    except Exception:
        return


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
        target_width = configured_width or int(
            base_width * _QT_FILE_DIALOG_COLUMN_SCALE * _QT_FILE_DIALOG_DEFAULT_SHRINK
        )
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


def _focus_file_view(dialog: "QtWidgets.QFileDialog") -> None:
    from PySide6 import QtCore, QtWidgets

    tree_view = dialog.findChild(QtWidgets.QTreeView, "treeView")
    if tree_view is None:
        return

    selection_model = tree_view.selectionModel()
    model = tree_view.model()
    if model is None or selection_model is None:
        return

    try:
        current_index = tree_view.currentIndex()
        if not current_index.isValid() and model.rowCount() > 0:
            first_index = model.index(0, 0)
            if first_index.isValid():
                tree_view.setCurrentIndex(first_index)
                selection_model.select(
                    first_index,
                    QtCore.QItemSelectionModel.Select
                    | QtCore.QItemSelectionModel.Rows,
                )
    except Exception:
        pass

    try:
        tree_view.setFocus(QtCore.Qt.FocusReason.ActiveWindowFocusReason)
    except Exception:
        pass


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
    _widen_file_dialog_sidebar(dialog, settings_obj)
    if _coerce_bool_setting(
        settings_obj, "QT_FILE_DIALOG_MAXIMIZE", _QT_FILE_DIALOG_MAXIMIZE
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
    gamepad_layer = _enable_dialog_gamepad(dialog)
    if dialog.exec() == QtWidgets.QDialog.Accepted:
        selected_files = dialog.selectedFiles()
        if selected_files:
            if gamepad_layer is not None:
                gamepad_layer.shutdown()
            return Path(selected_files[0])
    if gamepad_layer is not None:
        gamepad_layer.shutdown()
    return None


def _select_file_dialog(
    *,
    title: str,
    initial: Optional[Path] = None,
    file_filter: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
    dialog_key: str = "default",
) -> Optional[Path]:
    _ensure_qt_available()
    settings_obj = settings if settings is not None else _load_settings()
    start_dir = _preferred_start_dir(initial, settings_obj, dialog_key)

    try:
        selection = _qt_file_dialog(
            title=title, start_dir=start_dir, file_filter=file_filter, settings=settings_obj
        )
    except Exception as exc:  # pragma: no cover - GUI/runtime issues
        APP_LOGGER.log(
            f"PySide6 file selection failed: {exc}",
            level="ERROR",
            include_context=True,
            mirror_console=True,
            stream="stderr",
            location="qt-file-dialog",
        )
        raise

    if selection:
        _remember_file_dialog_dir(settings_obj, selection, dialog_key)
        if settings is None:
            _save_settings(settings_obj)

    return selection


def info_dialog(message: str) -> None:
    _ensure_qt_available()
    APP_LOGGER.log_dialog("Information", message, backend="qt", location="info-dialog")
    from PySide6 import QtWidgets

    _ensure_qt_app()
    box = QtWidgets.QMessageBox()
    box.setIcon(QtWidgets.QMessageBox.Information)
    box.setWindowTitle("Information")
    box.setText(message)
    ok_button = box.addButton(QtWidgets.QMessageBox.Ok)
    box.setDefaultButton(QtWidgets.QMessageBox.Ok)
    _enable_dialog_gamepad(
        box, affirmative=ok_button, negative=ok_button, default=ok_button
    )
    box.exec()


def error_dialog(message: str) -> None:
    _ensure_qt_available()
    APP_LOGGER.log_dialog(
        "Error", message, level="ERROR", backend="qt", location="error-dialog"
    )
    from PySide6 import QtWidgets

    _ensure_qt_app()
    box = QtWidgets.QMessageBox()
    box.setIcon(QtWidgets.QMessageBox.Critical)
    box.setWindowTitle("Error")
    box.setText(message)
    ok_button = box.addButton(QtWidgets.QMessageBox.Ok)
    box.setDefaultButton(QtWidgets.QMessageBox.Ok)
    _enable_dialog_gamepad(
        box, affirmative=ok_button, negative=ok_button, default=ok_button
    )
    box.exec()

def choose_install_action(title: str, text: str, select_label: str = "Select") -> str:
    """
    Show a dialog offering Download / Select / Cancel.

    Returns "Download", "Select", or "Cancel". ``select_label`` customizes the
    text shown for the "Select" button.
    """
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


def select_appimage(
    initial: Optional[Path] = None, *, settings: Optional[Dict[str, Any]] = None
) -> Optional[Path]:
    selection = _select_file_dialog(
        title="Select Archipelago AppImage",
        initial=initial,
        settings=settings,
        dialog_key="appimage",
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

    choice = _qt_question_dialog(
        title="Archipelago setup",
        text="Archipelago was not selected for download.\n\nSelect an existing AppImage to continue?",
        ok_label="Select AppImage",
        cancel_label="Cancel",
    )
    if choice != "ok":
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


def download_with_progress(
    url: str,
    dest: Path,
    *,
    title: str,
    text: str,
) -> None:
    """Download ``url`` to ``dest`` with a PySide6 progress dialog."""

    _ensure_dirs()
    _ensure_qt_available()
    from PySide6 import QtWidgets

    if dest.exists():
        try:
            dest.unlink()
        except Exception:
            pass

    _ensure_qt_app()
    dialog = QtWidgets.QProgressDialog()
    dialog.setWindowTitle(title)
    dialog.setLabelText(text)
    dialog.setRange(0, 100)
    dialog.setValue(0)
    dialog.setAutoClose(True)
    dialog.setAutoReset(True)
    dialog.setCancelButtonText("Cancel")
    dialog.show()

    cancel_button = dialog.findChild(QtWidgets.QPushButton)
    _enable_dialog_gamepad(
        dialog,
        affirmative=cancel_button,
        negative=cancel_button,
        default=cancel_button,
    )

    app = QtWidgets.QApplication.instance()
    if app is None:
        raise RuntimeError("QApplication unavailable for download progress dialog")

    req = urllib.request.Request(url, headers={"User-Agent": "ap-bizhelper/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp, dest.open("wb") as f:
            total_str = resp.headers.get("Content-Length") or "0"
            try:
                total = int(total_str)
            except ValueError:
                total = 0
            downloaded = 0
            chunk_size = 65536
            while True:
                if dialog.wasCanceled():
                    raise RuntimeError("Download cancelled by user")
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if total > 0:
                    percent = max(0, min(100, int(downloaded * 100 / total)))
                    dialog.setValue(percent)
                app.processEvents()
            dialog.setValue(100)
            app.processEvents()
    except Exception:
        if dest.exists():
            try:
                dest.unlink()
            except Exception:
                pass
        dialog.cancel()
        raise

    try:
        dest.chmod(dest.stat().st_mode | 0o111)
    except Exception:
        pass


def download_appimage(
    url: str, dest: Path, version: str, *, download_messages: Optional[list[str]] = None
) -> None:
    """Download the AppImage to ``dest`` with a Qt progress dialog."""

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
    settings: Optional[Dict[str, Any]] = None,
) -> Path:
    """
    Ensure the Archipelago AppImage is configured and up to date.

    On success, returns the Path to the AppImage and persists any changes
    into the JSON settings file. On failure, raises RuntimeError.
    """
    _ensure_dirs()
    provided_settings = settings
    settings = settings if settings is not None else _load_settings()

    def _merge_and_save_settings() -> None:
        nonlocal settings

        if provided_settings is not None and settings is not provided_settings:
            merged = {**provided_settings, **settings}
            provided_settings.clear()
            provided_settings.update(merged)
            settings = provided_settings

        _save_settings(settings)

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
            _merge_and_save_settings()
            downloaded = True
        else:
            app_path = _prompt_select_existing_appimage(
                Path(os.path.expanduser("~")), settings=settings
            )
            settings["AP_APPIMAGE"] = str(app_path)
            # No version information when manually selected.
            _merge_and_save_settings()

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
