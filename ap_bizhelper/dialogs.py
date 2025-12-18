from __future__ import annotations

"""Shared dialog helpers built on PySide6.

This module centralizes dialog rendering so the main app, shims, and helpers can
reuse consistent widgets. Callers can opt into persistent directory tracking by
passing load/save callbacks for file dialogs.
"""

import importlib.util
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .ap_bizhelper_config import load_settings as _load_shared_settings, save_settings as _save_shared_settings
from .logging_utils import AppLogger, get_app_logger

DOWNLOADS_DIR = Path(os.path.expanduser("~/Downloads"))
DIALOG_DEFAULTS = {
    "QT_FONT_SCALE": 1.5,
    "QT_MIN_POINT_SIZE": 12,
    "QT_FILE_NAME_FONT_SCALE": 1.8,
    "QT_FILE_ICON_SIZE": 48,
    "QT_FILE_DIALOG_WIDTH": 1280,
    "QT_FILE_DIALOG_HEIGHT": 800,
    "QT_FILE_DIALOG_MAXIMIZE": True,
    "QT_FILE_DIALOG_NAME_WIDTH": 850,
    "QT_FILE_DIALOG_TYPE_WIDTH": 200,
    "QT_FILE_DIALOG_SIZE_WIDTH": 200,
    "QT_FILE_DIALOG_DATE_WIDTH": 0,
    "QT_FILE_DIALOG_SIDEBAR_WIDTH": 200,
    "QT_FILE_DIALOG_SIDEBAR_ICON_SIZE": 32,
}

_QT_APP: Optional["QtWidgets.QApplication"] = None
_QT_BASE_FONT: Optional["QtGui.QFont"] = None
_QT_IMPORT_ERROR: Optional[BaseException] = None


DialogButtonRole = str


@dataclass
class DialogButtonSpec:
    label: str
    role: DialogButtonRole = "neutral"
    is_default: bool = False


@dataclass
class DialogResult:
    label: Optional[str]
    role: Optional[DialogButtonRole]
    checklist: List[str]
    progress_cancelled: bool = False


def merge_dialog_settings(settings: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    return {**DIALOG_DEFAULTS, **(settings or {})}


def _load_dialog_settings(settings: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    """Return dialog settings merged with defaults and persist new defaults.

    When ``settings`` is ``None``, the on-disk settings are loaded and any
    missing dialog defaults are written back immediately so future callers pick
    up the new baseline values without needing to save a selection first.
    """

    if settings is not None:
        return merge_dialog_settings(settings)

    stored_settings = _load_shared_settings()
    merged_settings = merge_dialog_settings(stored_settings)

    needs_save = False
    for key, value in DIALOG_DEFAULTS.items():
        if key not in stored_settings:
            needs_save = True
            break
        # Preserve existing user values; only mark save when defaults are missing.
    if needs_save:
        _save_shared_settings(merged_settings)

    return merged_settings


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


def _icon_for_level(icon: Optional[str], QtWidgets) -> Optional["QtGui.QPixmap"]:
    if not icon:
        return None
    normalized = icon.lower()
    icon_map = {
        "info": QtWidgets.QMessageBox.Information,
        "information": QtWidgets.QMessageBox.Information,
        "error": QtWidgets.QMessageBox.Critical,
        "critical": QtWidgets.QMessageBox.Critical,
        "warning": QtWidgets.QMessageBox.Warning,
        "question": QtWidgets.QMessageBox.Question,
    }
    if normalized not in icon_map:
        return None
    message_icon = QtWidgets.QMessageBox().iconPixmap()
    try:
        msg_box = QtWidgets.QMessageBox(icon_map[normalized], "", "")
        message_icon = msg_box.iconPixmap()
    except Exception:
        pass
    return message_icon


def modular_dialog(
    *,
    title: str,
    text: Optional[str] = None,
    buttons: Sequence[DialogButtonSpec],
    checklist: Optional[Iterable[Tuple[bool, str]]] = None,
    progress_stream: Optional[Iterable[str]] = None,
    icon: Optional[str] = None,
    height: Optional[int] = None,
) -> DialogResult:
    from PySide6 import QtCore, QtGui, QtWidgets

    if not buttons:
        raise ValueError("At least one button must be provided to modular_dialog")

    ensure_qt_app()
    dialog = QtWidgets.QDialog()
    dialog.setWindowTitle(title)
    if height is not None:
        try:
            dialog.resize(dialog.width(), int(height))
        except Exception:
            pass
    layout = QtWidgets.QVBoxLayout(dialog)

    header_layout = QtWidgets.QHBoxLayout()
    icon_pixmap = _icon_for_level(icon, QtWidgets)
    if icon_pixmap is not None:
        icon_label = QtWidgets.QLabel()
        icon_label.setPixmap(icon_pixmap)
        header_layout.addWidget(icon_label, alignment=QtCore.Qt.AlignmentFlag.AlignTop)

    if text:
        text_label = QtWidgets.QLabel(text)
        text_label.setWordWrap(True)
        header_layout.addWidget(text_label)

    if header_layout.count():
        layout.addLayout(header_layout)

    checklist_boxes: list[QtWidgets.QCheckBox] = []
    if checklist is not None:
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        container = QtWidgets.QWidget()
        container_layout = QtWidgets.QVBoxLayout(container)
        scroll.setFocusPolicy(QtCore.Qt.NoFocus)
        container.setFocusPolicy(QtCore.Qt.NoFocus)

        for checked, label_text in checklist:
            cb = QtWidgets.QCheckBox(label_text)
            cb.setChecked(checked)
            container_layout.addWidget(cb)
            checklist_boxes.append(cb)
        container_layout.addStretch()
        scroll.setWidget(container)
        layout.addWidget(scroll)

    progress_bar: Optional[QtWidgets.QProgressBar] = None
    if progress_stream is not None:
        progress_bar = QtWidgets.QProgressBar()
        progress_bar.setRange(0, 100)
        progress_bar.setValue(0)
        layout.addWidget(progress_bar)

    button_row = QtWidgets.QHBoxLayout()
    button_row.addStretch()
    qt_buttons: list[Tuple[QtWidgets.QPushButton, DialogButtonSpec]] = []
    default_button: Optional[QtWidgets.QPushButton] = None
    for idx, button_spec in enumerate(buttons):
        btn = QtWidgets.QPushButton(button_spec.label)
        if button_spec.is_default or (default_button is None and idx == 0):
            btn.setDefault(True)
            default_button = btn
        button_row.addWidget(btn)
        qt_buttons.append((btn, button_spec))
    button_row.addStretch()
    layout.addLayout(button_row)

    if qt_buttons:
        for (first_button, _), (second_button, _) in zip(qt_buttons, qt_buttons[1:]):
            QtWidgets.QWidget.setTabOrder(first_button, second_button)
        if checklist_boxes:
            QtWidgets.QWidget.setTabOrder(qt_buttons[-1][0], checklist_boxes[0])
            for prev, current in zip(checklist_boxes, checklist_boxes[1:]):
                QtWidgets.QWidget.setTabOrder(prev, current)
            QtWidgets.QWidget.setTabOrder(checklist_boxes[-1], qt_buttons[0][0])

    result = DialogResult(label=None, role=None, checklist=[], progress_cancelled=False)

    def _record_selection(button_spec: DialogButtonSpec) -> None:
        result.label = button_spec.label
        result.role = button_spec.role

    for button, spec in qt_buttons:
        button.clicked.connect(lambda _=False, s=spec: (_record_selection(s), dialog.accept()))

    def _handle_reject() -> None:
        if result.role is None:
            negative_spec = next((spec for _, spec in qt_buttons if spec.role == "negative"), None)
            if negative_spec:
                _record_selection(negative_spec)
            result.progress_cancelled = True

    dialog.rejected.connect(_handle_reject)

    positive_spec = next((spec for _, spec in qt_buttons if spec.role == "positive"), None)
    negative_spec = next((spec for _, spec in qt_buttons if spec.role == "negative"), None)
    special_spec = next((spec for _, spec in qt_buttons if spec.role == "special"), None)
    positive_button = next((btn for btn, spec in qt_buttons if spec.role == "positive"), None)
    negative_button = next((btn for btn, spec in qt_buttons if spec.role == "negative"), None)
    special_button = next((btn for btn, spec in qt_buttons if spec.role == "special"), None)
    enable_dialog_gamepad(
        dialog,
        affirmative=positive_button,
        negative=negative_button,
        special=special_button,
        default=default_button,
    )

    if default_button is not None:
        try:
            QtCore.QTimer.singleShot(
                0,
                lambda: default_button.setFocus(QtCore.Qt.FocusReason.ActiveWindowFocusReason),
            )
        except Exception:
            pass

    def _capture_checklist() -> None:
        result.checklist = [cb.text() for cb in checklist_boxes if cb.isChecked()]

    if progress_stream is None:
        dialog.activateWindow()
        dialog.raise_()
        exec_result = dialog.exec()
        _capture_checklist()
        if result.role is None:
            default_spec = next((spec for _, spec in qt_buttons if spec.is_default), None)
            if default_spec is None:
                default_spec = qt_buttons[0][1]
            if exec_result == QtWidgets.QDialog.Accepted:
                _record_selection(default_spec)
            elif exec_result == QtWidgets.QDialog.Rejected:
                fallback_negative = negative_spec or default_spec
                _record_selection(fallback_negative)
                result.progress_cancelled = True
        return result

    app = QtWidgets.QApplication.instance()
    if app is None:
        return result

    dialog.setWindowModality(QtCore.Qt.ApplicationModal)
    dialog.show()

    for line in progress_stream:
        app.processEvents()
        if result.role == "negative" or result.progress_cancelled:
            result.progress_cancelled = True
            break
        if not dialog.isVisible():
            result.progress_cancelled = True
            break
        if progress_bar is not None:
            value = line.strip()
            if not value:
                continue
            try:
                percent = int(float(value))
            except ValueError:
                continue
            progress_bar.setValue(max(0, min(100, percent)))

    if progress_bar is not None and not result.progress_cancelled:
        progress_bar.setValue(100)
    app.processEvents()

    if result.role is None and not result.progress_cancelled:
        if positive_spec is not None:
            _record_selection(positive_spec)
        elif special_spec is not None:
            _record_selection(special_spec)
        else:
            # A progress dialog without an affirmative button should still complete
            # successfully when the stream finishes.
            result.role = "positive"
    _capture_checklist()
    dialog.accept()
    return result


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
        from PySide6 import QtCore, QtWidgets

        sidebar = dialog.findChild(QtWidgets.QListView, "sidebar")
        if sidebar is None:
            return
        width = _coerce_int_setting(
            settings, "QT_FILE_DIALOG_SIDEBAR_WIDTH", int(DIALOG_DEFAULTS["QT_FILE_DIALOG_SIDEBAR_WIDTH"]), minimum=0
        )
        if width <= 0:
            return

        icon_size = _coerce_int_setting(
            settings,
            "QT_FILE_DIALOG_SIDEBAR_ICON_SIZE",
            int(DIALOG_DEFAULTS["QT_FILE_DIALOG_SIDEBAR_ICON_SIZE"]),
            minimum=0,
        )
        if icon_size > 0:
            try:
                sidebar.setIconSize(QtCore.QSize(icon_size, icon_size))
            except Exception:
                pass

        splitter: Optional[QtWidgets.QSplitter] = None
        parent = sidebar.parent()
        while parent is not None:
            if isinstance(parent, QtWidgets.QSplitter):
                splitter = parent
                break
            parent = parent.parent()
        if splitter is None:
            splitter = dialog.findChild(QtWidgets.QSplitter)

        if splitter is None:
            sidebar.setFixedWidth(width)
            sidebar.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Expanding)
            return

        sidebar_index = splitter.indexOf(sidebar)
        if sidebar_index < 0:
            sidebar_index = 0

        total_width = splitter.size().width()
        try:
            sizes = splitter.sizes()
        except Exception:
            sizes = []

        total_width = max(total_width, sum(sizes))
        if total_width <= 0:
            total_width = width * max(2, splitter.count())

        remaining = max(total_width - width, width)
        other_count = max(splitter.count() - 1, 1)
        per_other = max(int(remaining / other_count), 1)

        new_sizes = [per_other for _ in range(splitter.count())]
        if sidebar_index < len(new_sizes):
            new_sizes[sidebar_index] = width

        try:
            splitter.setSizes(new_sizes)
        except Exception:
            pass

        try:
            sidebar.setFixedWidth(width)
            sidebar.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Expanding)
        except Exception:
            pass

        for idx in range(splitter.count()):
            try:
                splitter.setStretchFactor(idx, 1 if idx != sidebar_index else 0)
            except Exception:
                continue
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


def _find_file_dialog_button(
    dialog: "QtWidgets.QFileDialog",
    *,
    names: Sequence[str],
    tooltip_keywords: Sequence[str],
) -> Optional["QtWidgets.QAbstractButton"]:
    try:
        from PySide6 import QtWidgets
    except Exception:
        return None

    for name in names:
        button = dialog.findChild(QtWidgets.QAbstractButton, name)
        if button is not None and button.isVisible():
            return button

    for button in dialog.findChildren(QtWidgets.QAbstractButton):
        if not button.isVisible():
            continue
        tooltip = (button.toolTip() or "").casefold()
        if tooltip and any(keyword in tooltip for keyword in tooltip_keywords):
            return button
    return None


def _bind_file_dialog_navigation(dialog: "QtWidgets.QFileDialog") -> None:
    try:
        from PySide6 import QtCore, QtGui
    except Exception:
        return

    def _activate_button(
        names: Sequence[str],
        tooltip_keywords: Sequence[str],
    ) -> None:
        button = _find_file_dialog_button(
            dialog, names=names, tooltip_keywords=tooltip_keywords
        )
        if button is None:
            return
        try:
            button.animateClick()
            return
        except Exception:
            pass
        try:
            button.click()
        except Exception:
            pass

    shortcut_specs = [
        (
            QtCore.Qt.Key_L,
            lambda: _activate_button(["backButton"], ("back", "previous")),
        ),
        (
            QtCore.Qt.Key_R,
            lambda: _activate_button(["forwardButton"], ("forward", "next")),
        ),
        (
            QtCore.Qt.Key_Y,
            lambda: _activate_button(
                ["toParentButton", "cdUpButton"], ("up", "parent")
            ),
        ),
    ]

    for key, handler in shortcut_specs:
        shortcut = QtGui.QShortcut(QtGui.QKeySequence(key), dialog)
        shortcut.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        shortcut.activated.connect(handler)


def question_dialog(
    *, title: str, text: str, ok_label: str, cancel_label: str, extra_label: Optional[str] = None
) -> str:
    buttons = [DialogButtonSpec(ok_label, role="positive", is_default=True)]
    if extra_label:
        buttons.append(DialogButtonSpec(extra_label, role="special"))
    buttons.append(DialogButtonSpec(cancel_label, role="negative"))

    result = modular_dialog(
        title=title,
        text=text,
        icon="question",
        buttons=buttons,
    )

    if result.role == "positive":
        return "ok"
    if result.role == "special":
        return "extra"
    return "cancel"


def info_dialog(message: str, *, title: str = "Information", logger: Optional[AppLogger] = None) -> None:
    ensure_qt_available()
    app_logger = logger or get_app_logger()
    app_logger.log_dialog(title, message, backend="qt", location="info-dialog")
    modular_dialog(
        title=title,
        text=message,
        icon="info",
        buttons=[DialogButtonSpec("OK", role="positive", is_default=True)],
    )


def error_dialog(message: str, *, title: str = "Error", logger: Optional[AppLogger] = None) -> None:
    ensure_qt_available()
    app_logger = logger or get_app_logger()
    app_logger.log_dialog(title, message, level="ERROR", backend="qt", location="error-dialog")
    modular_dialog(
        title=title,
        text=message,
        icon="error",
        buttons=[DialogButtonSpec("OK", role="positive", is_default=True)],
    )


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

    settings_obj = _load_dialog_settings(settings)
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

    icon_size_value = _coerce_int_setting(
        settings_obj,
        "QT_FILE_ICON_SIZE",
        int(DIALOG_DEFAULTS["QT_FILE_ICON_SIZE"]),
        minimum=0,
    )
    icon_size = QtCore.QSize(icon_size_value, icon_size_value) if icon_size_value > 0 else None

    for view_name in ("listView", "treeView"):
        file_view = dialog.findChild(QtWidgets.QWidget, view_name)
        if file_view is not None:
            _scale_file_name_font(file_view)
            if icon_size is not None:
                try:
                    file_view.setIconSize(icon_size)
                except Exception:
                    pass

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
    _bind_file_dialog_navigation(dialog)
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
    settings_obj = _load_dialog_settings(settings)
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
    button_specs = [
        DialogButtonSpec(ok_label, role="positive", is_default=True),
        DialogButtonSpec(cancel_label, role="negative"),
    ]

    dialog = modular_dialog(
        title=title or "Select items",
        text=text,
        buttons=button_specs,
        checklist=items,
        height=height,
    )

    if dialog.role != "positive":
        return None
    return dialog.checklist


def progress_dialog_from_stream(
    title: str, text: str, stream: Iterable[str], *, cancel_label: str = "Cancel"
) -> int:
    buttons = [DialogButtonSpec(cancel_label, role="negative", is_default=True)]

    result = modular_dialog(
        title=title or "Progress",
        text=text,
        buttons=buttons,
        progress_stream=stream,
    )

    if result.progress_cancelled or result.role == "negative":
        return 1
    return 0
