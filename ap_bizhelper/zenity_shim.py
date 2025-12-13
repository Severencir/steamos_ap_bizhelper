"""Zenity-compatible shim that proxies to PySide6 dialogs when possible.

This module provides two entry points:

* ``shim_main``: CLI handler invoked by the temporary ``zenity`` script.
* ``prepare_zenity_shim_env``: helper to build an environment pointing ``PATH``
  at the shim so launched applications use it instead of the system zenity.

Unsupported commands surface an error dialog instead of falling back to the
real ``zenity`` binary. This is temporary so that failures are visible while
the shim is hardened.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_REAL_ZENITY_ENV = "AP_BIZHELPER_REAL_ZENITY"


class ZenityShim:
    """Parse a zenity command and render equivalent PySide6 dialogs."""

    def __init__(self, real_zenity: Optional[str] = None) -> None:
        self.real_zenity = real_zenity or self._discover_real_zenity()

    def _discover_real_zenity(self) -> Optional[str]:
        shim_dir = os.environ.get("AP_BIZHELPER_SHIM_DIR", "")
        search_path = os.environ.get("PATH", "")
        if shim_dir:
            # Avoid returning the shim itself by removing the shim directory from PATH
            cleaned = os.pathsep.join(
                [p for p in search_path.split(os.pathsep) if p and Path(p) != Path(shim_dir)]
            )
        else:
            cleaned = search_path
        return shutil.which("zenity", path=cleaned) or None

    def handle(self, argv: Sequence[str]) -> int:
        if not argv:
            return self._fallback(argv, "No zenity arguments were provided.")

        mode = self._detect_mode(argv)
        if mode is None:
            return self._fallback(
                argv,
                "The zenity shim could not recognize the requested dialog type.",
            )

        if not self._qt_available():
            return self._fallback(
                argv,
                "PySide6 is unavailable, so the shim cannot render the requested dialog.",
            )

        if mode == "question":
            return self._handle_question(argv)
        if mode == "info":
            return self._handle_message(argv, level="info")
        if mode == "error":
            return self._handle_message(argv, level="error")
        if mode == "checklist":
            return self._handle_checklist(argv)
        if mode == "progress":
            return self._handle_progress(argv)

        return self._fallback(argv, "The requested zenity mode is not yet supported.")

    def _detect_mode(self, argv: Sequence[str]) -> Optional[str]:
        if "--question" in argv:
            return "question"
        if "--info" in argv:
            return "info"
        if "--error" in argv:
            return "error"
        if "--progress" in argv:
            return "progress"
        if "--list" in argv and "--checklist" in argv:
            return "checklist"
        return None

    def _qt_available(self) -> bool:
        return importlib.util.find_spec("PySide6") is not None

    def _fallback(self, argv: Sequence[str], reason: str) -> int:
        cmd = " ".join(argv) if argv else "(no arguments)"
        details = [
            "The Archipelago zenity shim could not handle the request.",
            f"Reason: {reason}",
        ]
        if self.real_zenity:
            details.append(
                "The system zenity is installed, but the shim failsafe is temporarily disabled."
            )
        details.append(f"Command: {cmd}")
        self._show_error_dialog("\n".join(details))
        return 127

    def _show_error_dialog(self, message: str) -> None:
        if self._qt_available():
            try:
                from PySide6 import QtWidgets  # type: ignore
                from ap_bizhelper.ap_bizhelper_ap import _ensure_qt_app

                _ensure_qt_app()
                box = QtWidgets.QMessageBox()
                box.setIcon(QtWidgets.QMessageBox.Critical)
                box.setWindowTitle("Zenity Shim Error")
                box.setText(message)
                box.exec()
                return
            except Exception:
                pass

        if self.real_zenity:
            try:
                subprocess.call(
                    [self.real_zenity, "--error", f"--text={message}"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            except Exception:
                pass

        sys.stderr.write(message + "\n")

    def _extract_option(self, argv: Sequence[str], prefix: str) -> Optional[str]:
        for arg in argv:
            if arg.startswith(prefix):
                return arg.split("=", 1)[-1]
        return None

    def _handle_question(self, argv: Sequence[str]) -> int:
        from PySide6 import QtWidgets  # type: ignore
        from ap_bizhelper.ap_bizhelper_ap import _ensure_qt_app, _qt_question_dialog

        _ensure_qt_app()
        title = self._extract_option(argv, "--title=") or "Question"
        text = self._extract_option(argv, "--text=") or ""
        ok_label = self._extract_option(argv, "--ok-label=") or "OK"
        cancel_label = self._extract_option(argv, "--cancel-label=") or "Cancel"
        extra_label = self._extract_option(argv, "--extra-button=")

        choice = _qt_question_dialog(
            title=title, text=text, ok_label=ok_label, cancel_label=cancel_label, extra_label=extra_label
        )
        if choice == "ok":
            return 0
        if choice == "extra" and extra_label:
            sys.stdout.write(extra_label + "\n")
            return 5
        return 1

    def _handle_message(self, argv: Sequence[str], *, level: str) -> int:
        from PySide6 import QtWidgets  # type: ignore
        from ap_bizhelper.ap_bizhelper_ap import _ensure_qt_app

        _ensure_qt_app()
        box = QtWidgets.QMessageBox()
        if level == "error":
            box.setIcon(QtWidgets.QMessageBox.Critical)
            box.setWindowTitle(self._extract_option(argv, "--title=") or "Error")
        else:
            box.setIcon(QtWidgets.QMessageBox.Information)
            box.setWindowTitle(self._extract_option(argv, "--title=") or "Information")
        box.setText(self._extract_option(argv, "--text=") or "")
        box.exec()
        return 0

    def _parse_checklist_items(self, argv: Sequence[str]) -> Optional[List[Tuple[bool, str]]]:
        columns = [arg.split("=", 1)[1] for arg in argv if arg.startswith("--column=")]
        if len(columns) < 2:
            return None
        values: List[str] = [arg for arg in argv if not arg.startswith("--")]
        if len(values) % len(columns) != 0:
            return None
        rows: List[Tuple[bool, str]] = []
        step = len(columns)
        for idx in range(0, len(values), step):
            chunk = values[idx : idx + step]
            if len(chunk) < 2:
                return None
            checked = chunk[0].strip().upper() == "TRUE"
            label = chunk[1]
            rows.append((checked, label))
        return rows

    def _handle_checklist(self, argv: Sequence[str]) -> int:
        from PySide6 import QtWidgets  # type: ignore
        from ap_bizhelper.ap_bizhelper_ap import _ensure_qt_app

        items = self._parse_checklist_items(argv)
        if items is None:
            return self._fallback(argv, "The checklist arguments could not be parsed.")

        _ensure_qt_app()
        dialog = QtWidgets.QDialog()
        dialog.setWindowTitle(self._extract_option(argv, "--title=") or "Select items")
        maybe_height = self._extract_option(argv, "--height=")
        if maybe_height:
            try:
                dialog.resize(dialog.width(), int(maybe_height))
            except ValueError:
                pass

        layout = QtWidgets.QVBoxLayout(dialog)
        text = self._extract_option(argv, "--text=")
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

        buttons = QtWidgets.QDialogButtonBox()
        ok_label = self._extract_option(argv, "--ok-label=") or "OK"
        cancel_label = self._extract_option(argv, "--cancel-label=") or "Cancel"
        ok_button = buttons.addButton(ok_label, QtWidgets.QDialogButtonBox.AcceptRole)
        cancel_button = buttons.addButton(cancel_label, QtWidgets.QDialogButtonBox.RejectRole)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        ok_button.setDefault(True)
        dialog.setLayout(layout)

        result = dialog.exec()
        if result != QtWidgets.QDialog.Accepted:
            return 1

        selected = [cb.text() for cb in checkboxes if cb.isChecked()]
        sys.stdout.write("|".join(selected) + "\n")
        return 0

    def _handle_progress(self, argv: Sequence[str]) -> int:
        from PySide6 import QtWidgets  # type: ignore
        from ap_bizhelper.ap_bizhelper_ap import _ensure_qt_app

        _ensure_qt_app()
        dialog = QtWidgets.QProgressDialog()
        dialog.setWindowTitle(self._extract_option(argv, "--title=") or "Progress")
        dialog.setLabelText(self._extract_option(argv, "--text=") or "")
        dialog.setCancelButtonText("Cancel")
        dialog.setRange(0, 100)
        dialog.setValue(0)
        dialog.setAutoClose(True)
        dialog.setAutoReset(True)
        dialog.show()

        app = QtWidgets.QApplication.instance()
        if app is None:
            return 1

        def is_cancelled() -> bool:
            app.processEvents()
            return dialog.wasCanceled()

        for line in sys.stdin:
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


def prepare_zenity_shim_env() -> Optional[Dict[str, str]]:
    """Create a temporary zenity shim script and return environment overrides.

    The returned mapping can be merged into a subprocess environment to force
    child processes to use the shimmed zenity. If the shim cannot be created,
    ``None`` is returned.
    """

    try:
        shim_dir = Path(tempfile.mkdtemp(prefix="ap-bizhelper-zenity-"))
    except Exception:
        return None

    real_zenity = shutil.which("zenity")
    shim_path = shim_dir / "zenity"
    content = """#!/usr/bin/env python3
from ap_bizhelper.zenity_shim import shim_main
if __name__ == "__main__":
    shim_main()
"""
    shim_path.write_text(content, encoding="utf-8")
    shim_path.chmod(0o755)

    pkg_root = Path(__file__).resolve().parent.parent
    pythonpath = os.environ.get("PYTHONPATH", "")
    env = {
        "PATH": shim_dir.as_posix() + os.pathsep + os.environ.get("PATH", ""),
        "PYTHONPATH": pkg_root.as_posix()
        + (os.pathsep + pythonpath if pythonpath else ""),
        _REAL_ZENITY_ENV: real_zenity or "",
        "AP_BIZHELPER_SHIM_DIR": shim_dir.as_posix(),
    }
    return env


def shim_main() -> None:
    shim = ZenityShim(real_zenity=os.environ.get(_REAL_ZENITY_ENV) or None)
    sys.exit(shim.handle(sys.argv[1:]))


if __name__ == "__main__":
    shim_main()
