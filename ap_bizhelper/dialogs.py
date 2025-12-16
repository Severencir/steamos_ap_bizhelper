from __future__ import annotations

"""
dialogs.py — small, controller-friendly PySide6 dialog helpers.

This is a *rebuilt* version of the original module with two priorities:
1) Simplicity (less "clever" UI tweaking, fewer side effects, fewer globals)
2) Predictable focus behavior for D-pad / arrow-key navigation

Key behavior you asked for (checklist dialog)
- Initial focus is on the OK button (not the first checkbox)
- D-pad / arrow keys cycle through BOTH buttons and checkboxes
- Order and wrap:
    OK -> Cancel -> checkbox 1 -> ... -> checkbox N -> OK

Integration notes
- This module is self-contained: it does not require your prior config/logging modules.
- If your project has a `.gamepad_input` module with:
      install_gamepad_navigation(widget, actions={...})
  then `enable_dialog_gamepad(...)` will hook it up; otherwise it's a no-op.
- If you want persistent "last directory" behavior for file dialogs, pass a mutable
  `settings` dict; we update keys inside it.

Settings dictionary keys used (all optional)
- "QT_FONT_SCALE": float (default 1.4)
- "QT_MIN_POINT_SIZE": int (default 12)
- "LAST_FILE_DIALOG_DIR": str
- "LAST_FILE_DIALOG_DIRS": dict[str, str]  (per-dialog key -> dir)

Typical usage
    from . import dialogs

    dialogs.ensure_qt_app({"QT_FONT_SCALE": 1.4, "QT_MIN_POINT_SIZE": 12})

    picked = dialogs.checklist_dialog(
        title="Select items",
        text="Choose:",
        items=[(True, "A"), (False, "B")],
        settings=my_settings,
        dialog_key="export_items",
    )
"""

import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# ---- Qt bootstrap ------------------------------------------------------------

_QT_IMPORT_ERROR: Optional[BaseException] = None


def ensure_qt_available() -> None:
    """Raise a clear error if PySide6 isn't installed."""
    global _QT_IMPORT_ERROR
    if _QT_IMPORT_ERROR is not None:
        raise RuntimeError("PySide6 is required to show dialogs") from _QT_IMPORT_ERROR
    try:
        import PySide6  # noqa: F401
    except Exception as exc:  # pragma: no cover
        _QT_IMPORT_ERROR = exc
        raise RuntimeError("PySide6 is required to show dialogs") from exc


def merge_dialog_settings(settings: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    """
    Compatibility helper: returns a mutable dict of settings.
    - If `settings` is provided, it's returned (and may be mutated by file dialogs).
    - Otherwise a new dict is returned.

    The old module merged app-wide config; this rebuild intentionally does not.
    """
    return settings if settings is not None else {}


def ensure_qt_app(settings: Optional[Dict[str, object]] = None) -> "QtWidgets.QApplication":
    """
    Ensure a QApplication exists, and apply a minimal global font scaling.

    This function is intentionally conservative:
    - it does not attempt DPI normalization
    - it does not alter platform theme/style
    """
    ensure_qt_available()
    from PySide6 import QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    s = merge_dialog_settings(settings)
    font_scale = float(s.get("QT_FONT_SCALE", 1.4) or 1.4)
    min_pt = int(s.get("QT_MIN_POINT_SIZE", 12) or 12)

    base = app.font()
    scaled = QtGui.QFont(base)

    # Prefer point size when available; otherwise scale pixels.
    if base.pointSize() > 0:
        scaled.setPointSize(max(int(base.pointSize() * font_scale), min_pt))
    elif base.pixelSize() > 0:
        scaled.setPixelSize(max(int(base.pixelSize() * font_scale), min_pt))
    else:
        scaled.setPointSize(min_pt)

    app.setFont(scaled)
    return app


# ---- Optional gamepad hook ---------------------------------------------------

def enable_dialog_gamepad(dialog: "QtWidgets.QWidget", actions: dict) -> Optional[object]:
    """
    If your project provides `.gamepad_input.install_gamepad_navigation`,
    call it and return the created layer object. Otherwise return None.

    actions is an app-defined mapping; typically includes:
      {"affirmative": ok_button, "negative": cancel_button, "default": ok_button}
    """
    try:
        from . import gamepad_input  # type: ignore

        layer = gamepad_input.install_gamepad_navigation(dialog, actions=actions)
        try:
            dialog.destroyed.connect(lambda *_: getattr(layer, "shutdown", lambda: None)())  # type: ignore
        except Exception:
            pass
        return layer
    except Exception:
        return None


# ---- Focus + navigation helpers ---------------------------------------------

def _defer_focus(widget: "QtWidgets.QWidget") -> None:
    """Set focus after the dialog is shown, so Qt doesn't steal it back."""
    from PySide6 import QtCore

    QtCore.QTimer.singleShot(
        0, lambda: widget.setFocus(QtCore.Qt.FocusReason.ActiveWindowFocusReason)
    )


def _set_tab_cycle(widgets: Sequence["QtWidgets.QWidget"]) -> None:
    """
    Create an explicit tab-order *cycle* (wrap to start).

    widgets: [w0, w1, ..., wn]
      w0 -> w1 -> ... -> wn -> w0
    """
    from PySide6 import QtWidgets

    if len(widgets) < 2:
        return
    for a, b in zip(widgets, widgets[1:]):
        QtWidgets.QWidget.setTabOrder(a, b)
    QtWidgets.QWidget.setTabOrder(widgets[-1], widgets[0])


def _make_arrow_keys_follow_tab_order(dialog: "QtWidgets.QDialog") -> None:
    """
    Force arrow keys (and most DPads that produce arrow key events) to move focus
    using the tab order chain, not geometric "nearest" navigation.
    """
    from PySide6 import QtCore

    class _Filter(QtCore.QObject):
        def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
            if event.type() == QtCore.QEvent.KeyPress:
                key = event.key()
                if key in (QtCore.Qt.Key_Right, QtCore.Qt.Key_Down):
                    dialog.focusNextPrevChild(True)
                    return True
                if key in (QtCore.Qt.Key_Left, QtCore.Qt.Key_Up):
                    dialog.focusNextPrevChild(False)
                    return True
            return False

    dialog.installEventFilter(_Filter(dialog))


def _downloads_dir() -> Path:
    # Cross-platform-ish "Downloads" guess; not guaranteed.
    home = Path.home()
    cand = home / "Downloads"
    return cand if cand.exists() else home


# ---- Message dialogs ---------------------------------------------------------

def question_dialog(
    title: str,
    text: str,
    *,
    ok_label: str = "OK",
    cancel_label: str = "Cancel",
    attach_gamepad: bool = True,
    settings: Optional[Dict[str, object]] = None,
) -> bool:
    """Return True if OK, False if Cancel."""
    ensure_qt_app(settings)
    from PySide6 import QtWidgets

    dlg = QtWidgets.QDialog()
    dlg.setWindowTitle(title)

    layout = QtWidgets.QVBoxLayout(dlg)

    label = QtWidgets.QLabel(text)
    label.setWordWrap(True)
    layout.addWidget(label)

    row = QtWidgets.QHBoxLayout()
    row.addStretch(1)
    ok_btn = QtWidgets.QPushButton(ok_label)
    ok_btn.setDefault(True)
    cancel_btn = QtWidgets.QPushButton(cancel_label)
    row.addWidget(ok_btn)
    row.addWidget(cancel_btn)
    row.addStretch(1)
    layout.addLayout(row)

    ok_btn.clicked.connect(dlg.accept)
    cancel_btn.clicked.connect(dlg.reject)

    _set_tab_cycle([ok_btn, cancel_btn])
    _make_arrow_keys_follow_tab_order(dlg)

    if attach_gamepad:
        enable_dialog_gamepad(dlg, {"affirmative": ok_btn, "negative": cancel_btn, "default": ok_btn})

    _defer_focus(ok_btn)
    return dlg.exec() == QtWidgets.QDialog.Accepted


def info_dialog(
    title: str,
    text: str,
    *,
    ok_label: str = "OK",
    attach_gamepad: bool = True,
    settings: Optional[Dict[str, object]] = None,
) -> None:
    ensure_qt_app(settings)
    from PySide6 import QtWidgets

    box = QtWidgets.QMessageBox()
    box.setWindowTitle(title)
    box.setIcon(QtWidgets.QMessageBox.Information)
    box.setText(text)
    ok_btn = box.addButton(ok_label, QtWidgets.QMessageBox.AcceptRole)
    box.setDefaultButton(ok_btn)

    if attach_gamepad:
        enable_dialog_gamepad(box, {"affirmative": ok_btn, "negative": ok_btn, "default": ok_btn})

    _defer_focus(ok_btn)
    box.exec()


def error_dialog(
    title: str,
    text: str,
    *,
    ok_label: str = "OK",
    attach_gamepad: bool = True,
    settings: Optional[Dict[str, object]] = None,
) -> None:
    ensure_qt_app(settings)
    from PySide6 import QtWidgets

    box = QtWidgets.QMessageBox()
    box.setWindowTitle(title)
    box.setIcon(QtWidgets.QMessageBox.Critical)
    box.setText(text)
    ok_btn = box.addButton(ok_label, QtWidgets.QMessageBox.AcceptRole)
    box.setDefaultButton(ok_btn)

    if attach_gamepad:
        enable_dialog_gamepad(box, {"affirmative": ok_btn, "negative": ok_btn, "default": ok_btn})

    _defer_focus(ok_btn)
    box.exec()


# ---- File dialog directory helpers ------------------------------------------

def preferred_start_dir(initial: Optional[Path], settings: Dict[str, object], dialog_key: str) -> Path:
    """
    Decide an initial directory for file dialogs.

    Priority:
      1) explicit initial (if not HOME)
      2) per-dialog remembered dir (LAST_FILE_DIALOG_DIRS[dialog_key])
      3) global remembered dir (LAST_FILE_DIALOG_DIR)
      4) ~/Downloads if present
      5) HOME
    """
    home = Path.home()

    per_map = settings.get("LAST_FILE_DIALOG_DIRS", {}) or {}
    per_dir = ""
    if isinstance(per_map, dict):
        per_dir = str(per_map.get(dialog_key, "") or "")

    global_dir = str(settings.get("LAST_FILE_DIALOG_DIR", "") or "")

    candidates = [
        initial if initial and initial.expanduser().resolve() != home.resolve() else None,
        Path(per_dir) if per_dir else None,
        Path(global_dir) if global_dir else None,
        _downloads_dir(),
        home,
    ]

    for c in candidates:
        if c is None:
            continue
        try:
            p = c.expanduser()
            if p.exists():
                return p
        except Exception:
            continue
    return home


def remember_file_dialog_dir(chosen_path: Optional[Path], settings: Dict[str, object], dialog_key: str) -> None:
    """Update settings dict with the directory of the chosen file/folder."""
    if not chosen_path:
        return
    try:
        d = chosen_path.expanduser()
        if d.is_file():
            d = d.parent
    except Exception:
        return

    settings["LAST_FILE_DIALOG_DIR"] = str(d)

    per = settings.get("LAST_FILE_DIALOG_DIRS")
    if not isinstance(per, dict):
        per = {}
        settings["LAST_FILE_DIALOG_DIRS"] = per
    per[dialog_key] = str(d)


# ---- File dialogs ------------------------------------------------------------

def file_dialog(
    *,
    title: str,
    start_dir: Path,
    file_filter: Optional[str] = None,
    settings: Optional[Dict[str, object]] = None,
    dialog_key: str = "file_dialog",
    use_non_native: bool = True,
) -> Optional[Path]:
    """
    Simple "open existing file" dialog.

    If `settings` is provided, we remember the chosen directory under dialog_key.
    """
    ensure_qt_app(settings)
    from PySide6 import QtWidgets

    s = merge_dialog_settings(settings)
    start = preferred_start_dir(start_dir, s, dialog_key)

    dlg = QtWidgets.QFileDialog()
    dlg.setWindowTitle(title)
    dlg.setAcceptMode(QtWidgets.QFileDialog.AcceptOpen)
    dlg.setFileMode(QtWidgets.QFileDialog.ExistingFile)
    dlg.setDirectory(str(start))
    dlg.setNameFilter(file_filter or "All Files (*)")
    if use_non_native:
        dlg.setOption(QtWidgets.QFileDialog.DontUseNativeDialog, True)

    if dlg.exec() != QtWidgets.QDialog.Accepted:
        return None

    files = dlg.selectedFiles()
    if not files:
        return None

    chosen = Path(files[0])
    remember_file_dialog_dir(chosen, s, dialog_key)
    return chosen


def select_file_dialog(
    *,
    title: str,
    initial: Optional[Path] = None,
    file_filter: Optional[str] = None,
    settings: Optional[Dict[str, object]] = None,
    dialog_key: str = "select_file",
    use_non_native: bool = True,
) -> Optional[Path]:
    """
    Convenience wrapper around file_dialog where `initial` is optional.
    """
    start = initial if initial is not None else Path.home()
    return file_dialog(
        title=title,
        start_dir=start,
        file_filter=file_filter,
        settings=settings,
        dialog_key=dialog_key,
        use_non_native=use_non_native,
    )


# ---- Checklist dialog --------------------------------------------------------

def checklist_dialog(
    *,
    title: str,
    items: Iterable[Tuple[bool, str]],
    text: Optional[str] = None,
    ok_label: str = "OK",
    cancel_label: str = "Cancel",
    height: Optional[int] = None,
    attach_gamepad: bool = True,
    settings: Optional[Dict[str, object]] = None,
    dialog_key: str = "checklist",
) -> Optional[List[str]]:
    """
    Controller-friendly checklist dialog.

    Focus behavior:
      - initial focus: OK
      - Right/Down: next by tab order
      - Left/Up: previous by tab order
      - wraps: OK -> Cancel -> cb1..cbN -> OK

    Returns:
      None if cancelled
      else list[str] of checked labels
    """
    ensure_qt_app(settings)
    from PySide6 import QtCore, QtWidgets

    dlg = QtWidgets.QDialog()
    dlg.setWindowTitle(title or "Select items")

    if height is not None:
        try:
            dlg.resize(dlg.width(), int(height))
        except Exception:
            pass

    layout = QtWidgets.QVBoxLayout(dlg)

    if text:
        label = QtWidgets.QLabel(text)
        label.setWordWrap(True)
        layout.addWidget(label)

    scroll = QtWidgets.QScrollArea()
    scroll.setWidgetResizable(True)
    # Critical: avoid the scroll container taking initial focus
    scroll.setFocusPolicy(QtCore.Qt.NoFocus)

    container = QtWidgets.QWidget()
    container.setFocusPolicy(QtCore.Qt.NoFocus)
    v = QtWidgets.QVBoxLayout(container)
    v.setContentsMargins(0, 0, 0, 0)

    checkboxes: List[QtWidgets.QCheckBox] = []
    for checked, label_text in items:
        cb = QtWidgets.QCheckBox(label_text)
        cb.setChecked(bool(checked))
        cb.setFocusPolicy(QtCore.Qt.StrongFocus)
        v.addWidget(cb)
        checkboxes.append(cb)

    v.addStretch(1)
    scroll.setWidget(container)
    layout.addWidget(scroll)

    row = QtWidgets.QHBoxLayout()
    row.addStretch(1)
    ok_btn = QtWidgets.QPushButton(ok_label)
    ok_btn.setDefault(True)
    cancel_btn = QtWidgets.QPushButton(cancel_label)
    row.addWidget(ok_btn)
    row.addWidget(cancel_btn)
    row.addStretch(1)
    layout.addLayout(row)

    ok_btn.clicked.connect(dlg.accept)
    cancel_btn.clicked.connect(dlg.reject)

    # The exact cycle you requested:
    cycle: List[QtWidgets.QWidget] = [ok_btn, cancel_btn, *checkboxes]
    _set_tab_cycle(cycle)
    _make_arrow_keys_follow_tab_order(dlg)

    if attach_gamepad:
        enable_dialog_gamepad(dlg, {"affirmative": ok_btn, "negative": cancel_btn, "default": ok_btn})

    _defer_focus(ok_btn)

    if dlg.exec() != QtWidgets.QDialog.Accepted:
        return None

    chosen = [cb.text() for cb in checkboxes if cb.isChecked()]

    # Optional: remember last directory-like choice? Not applicable here,
    # but we keep dialog_key/settings parameters for symmetry.
    _ = dialog_key  # unused

    return chosen


# ---- Progress dialog ---------------------------------------------------------

def progress_dialog_from_stream(
    *,
    title: str,
    text: str,
    stream: Iterable[str],
    cancel_label: str = "Cancel",
    attach_gamepad: bool = True,
    settings: Optional[Dict[str, object]] = None,
) -> int:
    """
    Stream-driven progress dialog.

    `stream` should yield lines that are either:
      - a number 0..100 (interpreted as percent)
      - or anything else (ignored)

    Returns:
      0 on success
      1 if cancelled
    """
    ensure_qt_app(settings)
    from PySide6 import QtWidgets

    dlg = QtWidgets.QProgressDialog()
    dlg.setWindowTitle(title or "Progress")
    dlg.setLabelText(text)
    dlg.setCancelButtonText(cancel_label)
    dlg.setRange(0, 100)
    dlg.setValue(0)
    dlg.setAutoClose(True)
    dlg.setAutoReset(True)
    dlg.show()

    # Try to find the cancel button for gamepad mapping.
    cancel_btn = dlg.findChild(QtWidgets.QPushButton)
    if attach_gamepad and cancel_btn is not None:
        enable_dialog_gamepad(dlg, {"affirmative": cancel_btn, "negative": cancel_btn, "default": cancel_btn})

    app = QtWidgets.QApplication.instance()
    if app is None:
        return 1

    def pump() -> bool:
        app.processEvents()
        return not dlg.wasCanceled()

    for line in stream:
        if not pump():
            dlg.cancel()
            return 1
        s = (line or "").strip()
        if not s:
            continue
        try:
            pct = int(float(s))
        except Exception:
            continue
        dlg.setValue(max(0, min(100, pct)))
        if not pump():
            dlg.cancel()
            return 1

    dlg.setValue(100)
    pump()
    return 0 if not dlg.wasCanceled() else 1
