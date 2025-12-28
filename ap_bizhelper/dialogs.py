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


# Enable verbose debug logs for the Qt file dialog selection/highlight behavior.
# Use Steam launch options like: AP_BIZHELPER_DEBUG_FILE_DIALOG=1 %command%
_FILE_DIALOG_DEBUG_ENV = "AP_BIZHELPER_DEBUG_FILE_DIALOG"

def _file_dialog_debug_enabled() -> bool:
    v = str(os.environ.get(_FILE_DIALOG_DEBUG_ENV, "")).strip().lower()
    return v not in ("", "0", "false", "no", "off")

def _fdlog(logger: Optional["AppLogger"], message: str, **fields: object) -> None:
    if not logger:
        return
    if fields:
        extras = " ".join(f"{k}={fields[k]!r}" for k in sorted(fields))
        message = f"{message} | {extras}"
    try:
        logger.log(message, location="file-dialog", include_context=True)
    except Exception:
        pass



def _fdlogd(logger: Optional["AppLogger"], message: str, **fields: object) -> None:
    """Debug-only wrapper around _fdlog.

    Extra file dialog logging is gated by AP_BIZHELPER_DEBUG_FILE_DIALOG so
    normal launches stay quiet.
    """
    if not _file_dialog_debug_enabled():
        return
    _fdlog(logger, message, **fields)


def _describe_index(view: object, idx: object) -> str:
    try:
        if idx is None or not idx.isValid():
            return "<invalid>"
        model = view.model()
        r = idx.row()
        c = idx.column()
        try:
            label = str(model.data(idx))
        except Exception:
            label = "?"
        return f"r{r}c{c} {label!r}"
    except Exception:
        return "<?>"

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
    radio_selection: Optional[str] = None


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
    radio_items: Optional[Iterable[str]] = None,
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

    radio_list: Optional[QtWidgets.QListWidget] = None
    if radio_items is not None:
        radio_list = QtWidgets.QListWidget()
        radio_list.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        for item_text in radio_items:
            radio_list.addItem(str(item_text))
        if radio_list.count() > 0:
            radio_list.setCurrentRow(0)
        layout.addWidget(radio_list)

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
        if radio_list is not None:
            QtWidgets.QWidget.setTabOrder(qt_buttons[-1][0], radio_list)
            last_focus_widget: QtWidgets.QWidget = radio_list
        else:
            last_focus_widget = qt_buttons[-1][0]
        if checklist_boxes:
            QtWidgets.QWidget.setTabOrder(last_focus_widget, checklist_boxes[0])
            for prev, current in zip(checklist_boxes, checklist_boxes[1:]):
                QtWidgets.QWidget.setTabOrder(prev, current)
            last_focus_widget = checklist_boxes[-1]
        if last_focus_widget is not qt_buttons[0][0]:
            QtWidgets.QWidget.setTabOrder(last_focus_widget, qt_buttons[0][0])

    result = DialogResult(label=None, role=None, checklist=[], progress_cancelled=False, radio_selection=None)

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

    def _capture_radio_selection() -> None:
        if radio_list is None:
            result.radio_selection = None
            return
        current = radio_list.currentItem()
        result.radio_selection = current.text() if current is not None else None

    if progress_stream is None:
        dialog.activateWindow()
        dialog.raise_()
        exec_result = dialog.exec()
        _capture_checklist()
        _capture_radio_selection()
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
    _capture_radio_selection()
    dialog.accept()
    return result



def _sidebar_urls(*, start_dir: Optional[Path] = None, settings: Optional[Dict[str, object]] = None) -> list["QtCore.QUrl"]:
    """Sidebar locations for QFileDialog.

    Prefer common user folders (Desktop, Downloads, Documents, etc.) so the
    sidebar looks like a typical desktop file picker.

    If ``start_dir`` (or the most-recent directory from settings) isn't already
    present, add it as an extra quick-access entry.
    """
    ensure_qt_available()
    from PySide6 import QtCore

    sp = QtCore.QStandardPaths

    # Order matters: keep this close to what most file pickers show.
    locations = [
        sp.HomeLocation,
        sp.DesktopLocation,
        sp.DocumentsLocation,
        sp.DownloadLocation,
        sp.MusicLocation,
        sp.PicturesLocation,
        sp.MoviesLocation,
    ]

    paths: list[Path] = []
    for loc in locations:
        try:
            p = sp.writableLocation(loc)
        except Exception:
            p = ""
        if p:
            paths.append(Path(p))

    # Fall back to the hard-coded Downloads dir if Qt doesn't provide one.
    if DOWNLOADS_DIR not in paths:
        paths.append(DOWNLOADS_DIR)

    # Add "most recently used" / start directory if it exists and isn't already present.
    extra_candidates: list[Path] = []
    if start_dir is not None:
        extra_candidates.append(start_dir)
    if settings is not None:
        last_dir_setting = str(settings.get("LAST_FILE_DIALOG_DIR", "") or "")
        if last_dir_setting:
            extra_candidates.append(Path(last_dir_setting))

    def _normalize(p: Path) -> Optional[Path]:
        try:
            p2 = p.expanduser()
        except Exception:
            p2 = p
        try:
            if p2.is_file():
                p2 = p2.parent
        except Exception:
            pass
        try:
            if not p2.exists():
                return None
        except Exception:
            return None
        return p2

    extra_paths: list[Path] = []
    for p in extra_candidates:
        np = _normalize(p)
        if np is not None:
            extra_paths.append(np)

    # Insert extras near the top (after Home) without disturbing the common layout.
    for extra in reversed(extra_paths):
        if extra in paths:
            continue
        insert_at = 1 if len(paths) >= 1 else 0
        paths.insert(insert_at, extra)

    # Useful top-level location.
    paths.append(Path("/"))

    # De-duplicate while preserving order.
    seen: set[str] = set()
    urls: list[QtCore.QUrl] = []
    for p in paths:
        try:
            key = str(p.expanduser().resolve())
        except Exception:
            key = str(p)
        if key in seen:
            continue
        seen.add(key)
        try:
            urls.append(QtCore.QUrl.fromLocalFile(p.as_posix()))
        except Exception:
            continue
    return urls


def _select_sidebar_for_path(dialog: "QtWidgets.QFileDialog", path: Path) -> None:
    """Highlight the sidebar entry that best matches ``path``.

    We intentionally prefer reading the sidebar model's row data (Qt.UserRole)
    over assuming the row ordering matches QFileDialog.sidebarUrls(). Some
    desktops insert extra rows (Recent/Trash/separators) that can desync the
    model rows from sidebarUrls.
    """
    try:
        from PySide6 import QtCore, QtWidgets
    except Exception:
        return

    sidebar = dialog.findChild(QtWidgets.QListView, "sidebar")
    if sidebar is None:
        return

    try:
        cur_norm = os.path.normpath(str(path.expanduser()))
    except Exception:
        cur_norm = os.path.normpath(str(path))

    model = sidebar.model()
    sm = sidebar.selectionModel()
    if model is None or sm is None:
        return

    root = sidebar.rootIndex()

    best_idx = None
    best_len = -1

    try:
        rows = model.rowCount(root)
    except Exception:
        try:
            rows = model.rowCount()
        except Exception:
            rows = 0

    for r in range(rows):
        try:
            cand = model.index(r, 0, root)
            if not cand.isValid():
                continue
            data = None
            try:
                data = model.data(cand, QtCore.Qt.UserRole)
            except Exception:
                data = None

            p = ""
            if hasattr(data, "toLocalFile"):
                try:
                    p = data.toLocalFile()
                except Exception:
                    p = ""
            elif isinstance(data, str):
                p = data

            if not p or not os.path.isabs(p):
                continue

            p_norm = os.path.normpath(p)
            if cur_norm == p_norm or cur_norm.startswith(p_norm + os.sep):
                if len(p_norm) > best_len:
                    best_len = len(p_norm)
                    best_idx = cand
        except Exception:
            continue

    # Fallback: if the model didn't expose a path, pick row 0 when it matches.
    if best_idx is None:
        try:
            urls = list(dialog.sidebarUrls())
            if urls:
                p0 = urls[0].toLocalFile()
                if p0:
                    p0n = os.path.normpath(p0)
                    if cur_norm == p0n or cur_norm.startswith(p0n + os.sep):
                        cand0 = model.index(0, 0, root)
                        if cand0.isValid():
                            best_idx = cand0
        except Exception:
            best_idx = None

    if best_idx is None:
        return

    flags = (
        QtCore.QItemSelectionModel.Clear
        | QtCore.QItemSelectionModel.Select
        | QtCore.QItemSelectionModel.Current
        | QtCore.QItemSelectionModel.Rows
    )
    try:
        sm.setCurrentIndex(best_idx, flags)
    except Exception:
        try:
            sm.select(best_idx, flags)
        except Exception:
            pass

    try:
        sidebar.setCurrentIndex(best_idx)
    except Exception:
        pass
    try:
        sidebar.scrollTo(best_idx)
    except Exception:
        pass
def _prime_file_dialog_selections(
    dialog: "QtWidgets.QFileDialog",
    start_dir: Path,
    *,
    logger: Optional["AppLogger"] = None,
) -> None:
    """Ensure initial selection highlights in both file pane and sidebar.

    QFileDialog populates its models asynchronously; we retry briefly to ensure a
    currentIndex/selection exists in the file pane. Sidebar selection is handled
    by file_dialog() once the sidebar URLs are applied.
    """
    try:
        from PySide6 import QtCore, QtWidgets
    except Exception:
        return

    attempts = {"left": 12}

    def _file_ok() -> bool:
        for view_name, view_type in (("treeView", QtWidgets.QTreeView), ("listView", QtWidgets.QListView)):
            try:
                v = dialog.findChild(view_type, view_name)
            except Exception:
                v = None
            if v is None:
                continue
            try:
                if not v.isVisible() or not v.isEnabled():
                    continue
            except Exception:
                pass
            try:
                sm = v.selectionModel()
                cur = v.currentIndex()
                if cur.isValid() and sm and sm.hasSelection():
                    return True
            except Exception:
                continue
        return False

    def _tick() -> None:
        attempts["left"] -= 1

        try:
            _focus_file_view(dialog, logger=logger)
        except Exception:
            pass

        # Sidebar selection is intentionally deferred to file_dialog() startup timers.

        ok = False
        try:
            ok = _file_ok()
        except Exception:
            ok = False

        _fdlog(logger, "prime tick", left=attempts["left"], file_ok=ok)

        if attempts["left"] > 0 and not ok:
            QtCore.QTimer.singleShot(50, _tick)

    QtCore.QTimer.singleShot(0, _tick)


def _install_file_dialog_force_first(
    dialog: "QtWidgets.QFileDialog",
    *,
    start_dir: Path,
    logger: Optional["AppLogger"] = None,
) -> None:
    """Re-assert 'row 0 is current' when the model reorders itself.

    The failure mode we see is:
      - we set row 0 current early
      - Qt later emits layoutChanged (sorting/relayout) and preserves the *item* as current,
        which can move it to a different row
      - our previous logic then accepts that moved currentIndex

    This hook listens for model changes (layoutChanged/modelReset/rowsInserted/directoryLoaded)
    shortly after initial show and after each directoryEntered, and forces row 0 current then.
    """
    try:
        from PySide6 import QtCore, QtWidgets
    except Exception:
        return

    import time as _time

    state: Dict[str, object] = {
        "until": 0.0,
        "connections": [],  # List[Tuple[signal, fn]]
        "model": None,
    }

    def _arm(window_ms: int, reason: str) -> None:
        state["until"] = _time.monotonic() + (window_ms / 1000.0)
        _fdlog(logger, "force-first armed", reason=reason, window_ms=window_ms)

    def _active() -> bool:
        try:
            return _time.monotonic() < float(state["until"])
        except Exception:
            return False

    def _disconnect_all() -> None:
        conns = list(state.get("connections") or [])
        state["connections"] = []
        for sig, fn in conns:
            try:
                sig.disconnect(fn)
            except Exception:
                pass

    def _pick_view() -> Optional[QtWidgets.QAbstractItemView]:
        candidates: List[QtWidgets.QAbstractItemView] = []
        for view_name, view_type in (("treeView", QtWidgets.QTreeView), ("listView", QtWidgets.QListView)):
            try:
                v = dialog.findChild(view_type, view_name)
            except Exception:
                v = None
            if v is None:
                continue
            try:
                if v.isVisible() and v.isEnabled():
                    candidates.append(v)
            except Exception:
                candidates.append(v)
        return candidates[0] if candidates else None

    def _wire_model_signals() -> None:
        view = _pick_view()
        if view is None:
            return
        model = view.model()
        if model is None or model is state.get("model"):
            return

        _fdlog(logger, "force-first wiring model", view=view.objectName() or type(view).__name__, model=type(model).__name__)
        _disconnect_all()
        state["model"] = model

        def _on_model_changed(*_args) -> None:
            if not _active():
                return
            _fdlog(logger, "force-first model change", view=view.objectName() or type(view).__name__)
            try:
                _focus_file_view(dialog, force_first=True, logger=logger)
            except Exception:
                pass

        # Connect common change signals.
        for sig_name in ("layoutChanged", "modelReset", "rowsInserted"):
            sig = getattr(model, sig_name, None)
            if sig is None:
                continue
            try:
                sig.connect(_on_model_changed)
                state["connections"].append((sig, _on_model_changed))
            except Exception:
                pass

        # QFileSystemModel emits this when async population completes.
        dir_sig = getattr(model, "directoryLoaded", None)
        if dir_sig is not None:
            try:
                dir_sig.connect(_on_model_changed)
                state["connections"].append((dir_sig, _on_model_changed))
            except Exception:
                pass

    def _schedule_force(reason: str) -> None:
        # Rewire to whatever model/view is currently active, then force a few times.
        _wire_model_signals()
        if not _active():
            return
        for delay in (0, 60, 160, 320):
            def _do_force(_r=reason, _d=delay) -> None:
                if not _active():
                    return
                _fdlog(logger, "force-first apply", reason=_r, delay_ms=_d)
                try:
                    _focus_file_view(dialog, force_first=True, logger=logger)
                except Exception:
                    pass
            try:
                QtCore.QTimer.singleShot(delay, _do_force)
            except Exception:
                pass

    def _on_directory_entered(path: str) -> None:
        _fdlog(logger, "directoryEntered", path=path)
        _arm(1500, "directoryEntered")
        _schedule_force("directoryEntered")

    # Initial arm: cover the first show/relayout.
    _arm(1500, "initial")
    _schedule_force("initial")

    try:
        dialog.directoryEntered.connect(_on_directory_entered)
    except Exception:
        pass

    # Sidebar selection is handled by file_dialog() after the sidebar URLs are applied.



def _apply_file_dialog_inactive_selection_style(dialog: "QtWidgets.QFileDialog") -> None:
    """Make selections look 'faded' when a pane is inactive.

    We intentionally avoid relying on Qt's ``:active``/``:focus`` pseudo-states
    here because their behavior varies across Qt styles (and SteamOS themes).

    Instead, styling is driven by a dynamic property on the ``QFileDialog``:
      - ``ap_bizhelper_active_pane="file"``: file pane is active, sidebar selection fades
      - ``ap_bizhelper_active_pane="sidebar"``: sidebar is active, file pane selection fades

    The gamepad layer updates this property when toggling panes.
    """
    try:
        from PySide6 import QtGui, QtWidgets
    except Exception:
        return

    try:
        pal = dialog.palette()
        active_hi = pal.color(QtGui.QPalette.Active, QtGui.QPalette.Highlight)
        base = pal.color(QtGui.QPalette.Active, QtGui.QPalette.Base)
    except Exception:
        return

    def _blend(a: "QtGui.QColor", b: "QtGui.QColor", t: float) -> "QtGui.QColor":
        t = max(0.0, min(1.0, float(t)))
        try:
            r = int(round(a.red() * (1.0 - t) + b.red() * t))
            g = int(round(a.green() * (1.0 - t) + b.green() * t))
            bl = int(round(a.blue() * (1.0 - t) + b.blue() * t))
            return QtGui.QColor(r, g, bl)
        except Exception:
            return b

    # 0.30–0.40 tends to read as "pale highlight" across light/dark themes.
    faded = _blend(base, active_hi, 0.35)
    faded_hex = faded.name()

    try:
        ss = dialog.styleSheet() or ""
    except Exception:
        ss = ""

    ss += f"""
    /* ap_bizhelper: pane-aware selection styling (driven by ap_bizhelper_active_pane)

       IMPORTANT:
       Some desktop themes ship more-specific rules like ::item:selected:focus / :active
       that can override a plain ::item:selected rule (and can vary per-column in a
       details QTreeView). To make this robust, we set view-level selection colors
       (selection-background-color / selection-color) and also provide item-level
       fallbacks.
    */

    /* Sidebar selection colors */
    QFileDialog[ap_bizhelper_active_pane="sidebar"] QListView#sidebar {{
        selection-background-color: palette(highlight);
        selection-color: palette(highlighted-text);
    }}
    QFileDialog[ap_bizhelper_active_pane="file"] QListView#sidebar {{
        selection-background-color: {faded_hex};
        selection-color: palette(text);
    }}

    /* File pane selection colors (treeView = details, listView = list/icon) */
    QFileDialog[ap_bizhelper_active_pane="file"] QTreeView#treeView,
    QFileDialog[ap_bizhelper_active_pane="file"] QListView#listView {{
        selection-background-color: palette(highlight);
        selection-color: palette(highlighted-text);
    }}
    QFileDialog[ap_bizhelper_active_pane="sidebar"] QTreeView#treeView,
    QFileDialog[ap_bizhelper_active_pane="sidebar"] QListView#listView {{
        selection-background-color: {faded_hex};
        selection-color: palette(text);
    }}

    /* Fallbacks: force item backgrounds too (covers styles that ignore selection-*). */
    QFileDialog[ap_bizhelper_active_pane="file"] QListView#sidebar::item:selected,
    QFileDialog[ap_bizhelper_active_pane="file"] QListView#sidebar::item:selected:active,
    QFileDialog[ap_bizhelper_active_pane="file"] QListView#sidebar::item:selected:focus {{
        background: {faded_hex};
        color: palette(text);
    }}
    QFileDialog[ap_bizhelper_active_pane="sidebar"] QListView#sidebar::item:selected,
    QFileDialog[ap_bizhelper_active_pane="sidebar"] QListView#sidebar::item:selected:active,
    QFileDialog[ap_bizhelper_active_pane="sidebar"] QListView#sidebar::item:selected:focus {{
        background: palette(highlight);
        color: palette(highlighted-text);
    }}

    QFileDialog[ap_bizhelper_active_pane="sidebar"] QTreeView#treeView::item:selected,
    QFileDialog[ap_bizhelper_active_pane="sidebar"] QTreeView#treeView::item:selected:active,
    QFileDialog[ap_bizhelper_active_pane="sidebar"] QTreeView#treeView::item:selected:focus,
    QFileDialog[ap_bizhelper_active_pane="sidebar"] QListView#listView::item:selected,
    QFileDialog[ap_bizhelper_active_pane="sidebar"] QListView#listView::item:selected:active,
    QFileDialog[ap_bizhelper_active_pane="sidebar"] QListView#listView::item:selected:focus {{
        background: {faded_hex};
        color: palette(text);
    }}
    QFileDialog[ap_bizhelper_active_pane="file"] QTreeView#treeView::item:selected,
    QFileDialog[ap_bizhelper_active_pane="file"] QTreeView#treeView::item:selected:active,
    QFileDialog[ap_bizhelper_active_pane="file"] QTreeView#treeView::item:selected:focus,
    QFileDialog[ap_bizhelper_active_pane="file"] QListView#listView::item:selected,
    QFileDialog[ap_bizhelper_active_pane="file"] QListView#listView::item:selected:active,
    QFileDialog[ap_bizhelper_active_pane="file"] QListView#listView::item:selected:focus {{
        background: palette(highlight);
        color: palette(highlighted-text);
    }}
    """

    try:
        dialog.setStyleSheet(ss)
    except Exception:
        return


def _install_sidebar_click_focus(dialog: "QtWidgets.QFileDialog") -> None:
    from PySide6 import QtCore, QtWidgets

    sidebar = dialog.findChild(QtWidgets.QListView, "sidebar")
    if sidebar is None:
        return

    def _repolish(w: Optional[QtWidgets.QWidget]) -> None:
        if w is None:
            return
        try:
            st = w.style()
            st.unpolish(w)
            st.polish(w)
            w.update()
        except Exception:
            try:
                w.update()
            except Exception:
                pass

    def _poke_view(v: Optional[QtWidgets.QAbstractItemView]) -> None:
        if v is None:
            return
        _repolish(v)
        try:
            vp = v.viewport()
            if vp is not None:
                vp.update()
                vp.repaint()
        except Exception:
            pass
        try:
            v.update()
            v.repaint()
        except Exception:
            pass

    def _set_active_sidebar() -> None:
        try:
            dialog.setProperty("ap_bizhelper_active_pane", "sidebar")
        except Exception:
            return
        try:
            sidebar.setFocus(QtCore.Qt.FocusReason.MouseFocusReason)
        except Exception:
            pass
        layer = getattr(dialog, "_ap_gamepad_layer", None)
        if layer is not None:
            try:
                layer._set_file_dialog_active_pane("sidebar")
            except Exception:
                pass
        _repolish(dialog)
        _poke_view(sidebar)
        _poke_view(dialog.findChild(QtWidgets.QAbstractItemView, "treeView"))
        _poke_view(dialog.findChild(QtWidgets.QAbstractItemView, "listView"))
        try:
            QtCore.QTimer.singleShot(0, lambda: _poke_view(sidebar))
        except Exception:
            pass

    class _SidebarClickFilter(QtCore.QObject):
        def eventFilter(self, obj: QtCore.QObject, ev: QtCore.QEvent) -> bool:  # noqa: N802
            if ev.type() in (
                QtCore.QEvent.MouseButtonPress,
                QtCore.QEvent.MouseButtonRelease,
                QtCore.QEvent.FocusIn,
            ):
                _set_active_sidebar()
            return False

    filter_obj = _SidebarClickFilter(sidebar)
    targets = [sidebar]
    try:
        vp = sidebar.viewport()
        if vp is not None:
            targets.append(vp)
    except Exception:
        pass
    for target in targets:
        try:
            target.installEventFilter(filter_obj)
        except Exception:
            pass
    setattr(dialog, "_ap_sidebar_click_filter", filter_obj)


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



def _focus_file_view(dialog: "QtWidgets.QFileDialog", *, force_first: bool = False, logger: Optional["AppLogger"] = None) -> None:
    """Ensure the file list has a visible current selection.

    QFileDialog's models can appear with a selection but an invalid currentIndex
    (or vice versa) while populating/sorting. Controller navigation tends to
    follow currentIndex, so we set both.

    If ``force_first`` is True, we intentionally ignore an existing valid
    currentIndex and force the first row (row 0) to become current.
    """
    try:
        from PySide6 import QtCore, QtWidgets
    except Exception:
        return

    # QFileDialog can show either a treeView (Detail) or listView (List).
    # Prefer whichever is visible/enabled.
    candidates: List[QtWidgets.QAbstractItemView] = []
    for view_name, view_type in (("treeView", QtWidgets.QTreeView), ("listView", QtWidgets.QListView)):
        try:
            v = dialog.findChild(view_type, view_name)
        except Exception:
            v = None
        if v is None:
            continue
        try:
            if v.isVisible() and v.isEnabled():
                candidates.append(v)
        except Exception:
            candidates.append(v)

    if not candidates:
        return

    view = candidates[0]
    model = view.model()
    selection_model = view.selectionModel()
    if not model or not selection_model:
        return

    root_index = getattr(view, "rootIndex", lambda: QtCore.QModelIndex())()
    try:
        row_count = model.rowCount(root_index)
    except Exception:
        try:
            row_count = model.rowCount()
        except Exception:
            row_count = 0
    if row_count <= 0:
        return

    current_index = view.currentIndex()
    _fdlog(logger, "focus_file_view pre", view=view.objectName() or type(view).__name__, force_first=force_first, cur=_describe_index(view, current_index))

    idx = None
    if not force_first and current_index is not None and getattr(current_index, "isValid", lambda: False)():
        idx = current_index

    if idx is None and not force_first:
        # If something is selected but currentIndex is invalid, use the selection.
        try:
            if selection_model.hasSelection():
                sel = selection_model.selectedIndexes()
                if sel:
                    idx = sel[0]
        except Exception:
            idx = None

    if idx is None:
        try:
            idx = model.index(0, 0, root_index)
        except Exception:
            try:
                idx = model.index(0, 0)
            except Exception:
                idx = None

    if idx is None or not getattr(idx, "isValid", lambda: False)():
        return

    flags = (
        QtCore.QItemSelectionModel.Clear
        | QtCore.QItemSelectionModel.Select
        | QtCore.QItemSelectionModel.Current
        | QtCore.QItemSelectionModel.Rows
    )
    try:
        selection_model.setCurrentIndex(idx, flags)
    except Exception:
        try:
            selection_model.select(idx, flags)
        except Exception:
            pass
    try:
        view.setCurrentIndex(idx)
    except Exception:
        pass
    try:
        view.scrollTo(idx)
    except Exception:
        pass
    try:
        view.setFocus(QtCore.Qt.FocusReason.ActiveWindowFocusReason)
    except Exception:
        pass

    _fdlog(logger, "focus_file_view post", view=view.objectName() or type(view).__name__, cur=_describe_index(view, view.currentIndex()))

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
    fd_logger: Optional["AppLogger"] = get_app_logger() if _file_dialog_debug_enabled() else None
    _fdlog(fd_logger, "file_dialog init", title=title, start_dir=str(start_dir), filter=filter_text)
    dialog = QtWidgets.QFileDialog()
    dialog.setWindowTitle(title)
    dialog.setAcceptMode(QtWidgets.QFileDialog.AcceptOpen)
    dialog.setFileMode(QtWidgets.QFileDialog.ExistingFile)
    # Note: we intentionally do not call setDirectory(start_dir) synchronously here.
    # Instead, we seed start_dir as the first sidebar entry and enter it on the next event-loop tick.
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

    sidebar_urls = _sidebar_urls(start_dir=start_dir, settings=settings_obj)
    # Seed the dynamic "current directory" entry (row 0) with our preferred start directory.
    # We will then "enter" it by selecting the matching sidebar item on the next tick.
    try:
        start_norm = os.path.normpath(str(start_dir.expanduser()))
        start_url = QtCore.QUrl.fromLocalFile(start_dir.as_posix())
        deduped: list[QtCore.QUrl] = [start_url]
        seen: set[str] = {start_norm}

        for u in sidebar_urls:
            try:
                p = u.toLocalFile()
            except Exception:
                continue
            if not p:
                continue
            try:
                k = os.path.normpath(p)
            except Exception:
                k = p
            if k in seen:
                continue
            seen.add(k)
            deduped.append(u)

        sidebar_urls = deduped
    except Exception:
        pass

    if sidebar_urls:
        dialog.setSidebarUrls(sidebar_urls)

    # Enter the preferred start directory via the sidebar on the next tick.
    # This avoids startup ordering issues where Qt may briefly show a platform default
    # (commonly Documents) before the picker fully initializes.
    def _enter_start_dir_via_sidebar() -> None:
        try:
            _select_sidebar_for_path(dialog, start_dir)
        except Exception:
            pass

    try:
        QtCore.QTimer.singleShot(0, _enter_start_dir_via_sidebar)
        QtCore.QTimer.singleShot(50, _enter_start_dir_via_sidebar)
    except Exception:
        pass
    try:
        paths = [u.toLocalFile() for u in dialog.sidebarUrls()]
        _fdlogd(fd_logger, 'file_dialog sidebarUrls applied', count=len(paths), first=paths[:6])
    except Exception:
        pass
    _widen_file_dialog_sidebar(dialog, settings_obj)
    try:
        dialog.setProperty("ap_bizhelper_active_pane", "file")
    except Exception:
        pass
    _apply_file_dialog_inactive_selection_style(dialog)
    _install_sidebar_click_focus(dialog)
    if _coerce_bool_setting(
        settings_obj, "QT_FILE_DIALOG_MAXIMIZE", bool(DIALOG_DEFAULTS["QT_FILE_DIALOG_MAXIMIZE"])
    ):
        dialog.setWindowState(dialog.windowState() | QtCore.Qt.WindowMaximized)
    _configure_file_view_columns(dialog, settings_obj)
    _install_file_dialog_force_first(dialog, start_dir=start_dir, logger=fd_logger)
    _prime_file_dialog_selections(dialog, start_dir, logger=fd_logger)
    _fdlogd(fd_logger, 'file_dialog after prime selections', dir=str(dialog.directory().absolutePath()))
    _focus_file_view(dialog, logger=fd_logger)
    try:
        QtCore.QTimer.singleShot(0, lambda: _focus_file_view(dialog, logger=fd_logger))
    except Exception:
        pass
    dialog.activateWindow()
    dialog.raise_()
    dialog.setFocus(QtCore.Qt.FocusReason.ActiveWindowFocusReason)
    try:
        dialog.directoryEntered.connect(lambda p: _fdlogd(fd_logger, 'file_dialog directoryEntered', path=str(p)))
    except Exception:
        pass
    gamepad_layer = enable_dialog_gamepad(dialog)
    _fdlogd(fd_logger, 'file_dialog about_to_exec', dir=str(dialog.directory().absolutePath()))
    result = dialog.exec()
    _fdlogd(fd_logger, 'file_dialog exec_return', result=int(result), dir=str(dialog.directory().absolutePath()))
    if result == QtWidgets.QDialog.Accepted:
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
    fd_logger: Optional["AppLogger"] = get_app_logger() if _file_dialog_debug_enabled() else None
    # Log how we decide the starting directory (per-dialog memory > global memory > defaults).
    try:
        per_dialog = str((settings_obj.get('LAST_FILE_DIALOG_DIRS', {}) or {}).get(dialog_key, '') or '')
        global_last = str(settings_obj.get('LAST_FILE_DIALOG_DIR', '') or '')
    except Exception:
        per_dialog = ''
        global_last = ''
    _fdlogd(
        fd_logger,
        'select_file_dialog start_dir decision',
        dialog_key=dialog_key,
        title=title,
        initial=str(initial) if initial else None,
        per_dialog_last=per_dialog or None,
        global_last=global_last or None,
        downloads=str(DOWNLOADS_DIR),
        home=str(Path.home()),
    )
    start_dir = preferred_start_dir(initial, settings_obj, dialog_key)
    _fdlogd(fd_logger, 'select_file_dialog start_dir chosen', dialog_key=dialog_key, start_dir=str(start_dir))

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


def radio_list_dialog(
    title: str,
    text: Optional[str],
    items: Iterable[str],
    *,
    ok_label: str = "OK",
    cancel_label: str = "Cancel",
    height: Optional[int] = None,
) -> Optional[str]:
    button_specs = [
        DialogButtonSpec(ok_label, role="positive", is_default=True),
        DialogButtonSpec(cancel_label, role="negative"),
    ]

    dialog = modular_dialog(
        title=title or "Select item",
        text=text,
        buttons=button_specs,
        radio_items=items,
        height=height,
    )

    if dialog.role != "positive":
        return None
    return dialog.radio_selection


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
