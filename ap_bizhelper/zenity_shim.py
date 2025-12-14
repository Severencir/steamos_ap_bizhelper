"""Zenity-compatible shim that proxies to PySide6 dialogs when possible.

This module provides two entry points:

* ``shim_main``: CLI handler invoked by the temporary ``zenity`` script.
* ``prepare_zenity_shim_env``: helper to build an environment pointing ``PATH``
  at the shim so launched applications use it instead of the system zenity.

Unsupported commands fall back to the real ``zenity`` binary when present so
the shim stays transparent during gaps in coverage.
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

from .logging_utils import (
    SHIM_LOG_ENV,
    AppLogger,
    RUN_ID_ENV,
    TIMESTAMP_ENV,
    create_component_logger,
)

_REAL_ZENITY_ENV = "AP_BIZHELPER_REAL_ZENITY"
_REAL_KDIALOG_ENV = "AP_BIZHELPER_REAL_KDIALOG"
_REAL_PORTAL_ENV = "AP_BIZHELPER_REAL_XDG_DESKTOP_PORTAL"


_SHIM_LOGGER: Optional[AppLogger] = None


def _logger() -> AppLogger:
    global _SHIM_LOGGER
    if _SHIM_LOGGER is None:
        _SHIM_LOGGER = create_component_logger("zenity-shim", env_var=SHIM_LOG_ENV, subdir="shim")
    return _SHIM_LOGGER


class ZenityShim:
    """Parse a zenity command and render equivalent PySide6 dialogs."""

    def __init__(self, real_zenity: Optional[str] = None) -> None:
        self.real_zenity = real_zenity or self._discover_real_zenity()
        self.logger = _logger()

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
        with self.logger.context("zenity-handle"):
            self.logger.log(
                f"Handling zenity shim request: {list(argv)}", include_context=True, location="zenity"
            )
            if not argv:
                return self._fallback(argv, "No zenity arguments were provided.")

            auto_answer = self._auto_answer_emuhawk(argv)
            if auto_answer is not None:
                return auto_answer

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

            self.logger.log(f"Detected zenity mode: {mode}", include_context=True, location="zenity")
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

    def _auto_answer_emuhawk(self, argv: Sequence[str]) -> Optional[int]:
        title = self._extract_option(argv, "--title=")
        text = self._extract_option(argv, "--text=")

        title_matches = bool(title and "emuhawk" in title.casefold())
        text_has_hint = bool(text and any(hint in text.casefold() for hint in ("emuhawk", "bizhawk")))

        if not title_matches and not text_has_hint:
            return None

        runner = self._locate_bizhawk_runner()
        if runner is None:
            self.logger.log(
                "EmuHawk auto-answer detected but no runner could be located.",
                level="WARNING",
                include_context=True,
            )
            return None
        sys.stdout.write(str(runner) + "\n")
        self.logger.log(
            f"EmuHawk auto-answer provided runner: {runner}",
            include_context=True,
            location="auto-answer",
        )
        return 0

    def _locate_bizhawk_runner(self) -> Optional[Path]:
        try:
            from ap_bizhelper.ap_bizhelper_ap import _load_settings as _load_ap_settings
        except Exception:
            return None

        settings = _load_ap_settings()
        runner_str = str(settings.get("BIZHAWK_RUNNER", "") or "")
        exe_str = str(settings.get("BIZHAWK_EXE", "") or "")

        candidates = []
        if runner_str:
            candidates.append(Path(runner_str))
        if exe_str:
            candidates.append(Path(exe_str).parent / "run_bizhawk_proton.py")
        candidates.append(Path(__file__).resolve().parent / "run_bizhawk_proton.py")

        for candidate in candidates:
            try:
                if candidate.is_file():
                    return candidate
            except Exception:
                continue
        return None

    def _qt_available(self) -> bool:
        return importlib.util.find_spec("PySide6") is not None

    def _fallback(self, argv: Sequence[str], reason: str) -> int:
        self.logger.log(
            f"Fallback invoked. Reason: {reason}. Original argv: {list(argv)}",
            level="WARNING",
            include_context=True,
            location="fallback",
        )
        real_result = self._maybe_run_real_zenity(argv)
        if real_result is not None:
            self.logger.log(
                f"Delegated to real zenity with return code {real_result}",
                include_context=True,
                location="fallback",
            )
            return real_result

        cmd = " ".join(argv) if argv else "(no arguments)"
        details = [
            "The Archipelago zenity shim could not handle the request.",
            f"Reason: {reason}",
        ]
        details.append(f"Command: {cmd}")
        self._show_error_dialog("\n".join(details))
        return 127

    def _maybe_run_real_zenity(self, argv: Sequence[str]) -> Optional[int]:
        if not self.real_zenity:
            return None
        try:
            self.logger.log(
                f"Executing real zenity: {[self.real_zenity, *argv]}",
                include_context=True,
                location="real-zenity",
            )
            return subprocess.call([self.real_zenity, *argv])
        except Exception:
            return None

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
                self.logger.log(
                    "Qt error dialog rendering failed; trying zenity fallback.",
                    level="WARNING",
                    include_context=True,
                    location="error-dialog",
                )

        if self.real_zenity:
            try:
                self.logger.log(
                    "Attempting to display error via real zenity.",
                    include_context=True,
                    location="error-dialog",
                )
                subprocess.call(
                    [self.real_zenity, "--error", f"--text={message}"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            except Exception:
                self.logger.log(
                    "Failed to display error via real zenity; falling back to stderr.",
                    level="WARNING",
                    include_context=True,
                    location="error-dialog",
                )

        sys.stderr.write(message + "\n")


    def _extract_option(self, argv: Sequence[str], prefix: str) -> Optional[str]:
        base = prefix[:-1] if prefix.endswith("=") else prefix

        for idx, arg in enumerate(argv):
            if arg == base and idx + 1 < len(argv):
                return argv[idx + 1]
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
        self.logger.log(
            f"Question dialog selection: {choice or 'cancelled'} (title={title!r})",
            include_context=True,
            location="question",
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
        self.logger.log(
            f"Displayed {level} message dialog with title={box.windowTitle()!r}",
            include_context=True,
            location=f"message-{level}",
        )
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
            self.logger.log(
                "Checklist dialog cancelled by user.", include_context=True, location="checklist"
            )
            return 1

        selected = [cb.text() for cb in checkboxes if cb.isChecked()]
        sys.stdout.write("|".join(selected) + "\n")
        self.logger.log(
            f"Checklist selections: {selected}", include_context=True, location="checklist"
        )
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
            self.logger.log(
                "Progress dialog cancelled by user before completion.",
                include_context=True,
                location="progress",
            )
            return 1
        self.logger.log("Progress dialog completed", include_context=True, location="progress")
        return 0


class KDialogShim:
    """Lightweight kdialog-compatible shim that proxies to PySide6 dialogs."""

    def __init__(self, real_kdialog: Optional[str] = None) -> None:
        self.real_kdialog = real_kdialog or self._discover_real_kdialog()
        self.logger = _logger()

    def _discover_real_kdialog(self) -> Optional[str]:
        shim_dir = os.environ.get("AP_BIZHELPER_SHIM_DIR", "")
        search_path = os.environ.get("PATH", "")
        if shim_dir:
            cleaned = os.pathsep.join(
                [p for p in search_path.split(os.pathsep) if p and Path(p) != Path(shim_dir)]
            )
        else:
            cleaned = search_path
        return shutil.which("kdialog", path=cleaned) or None

    def _qt_available(self) -> bool:
        return importlib.util.find_spec("PySide6") is not None

    def _extract_value(self, argv: Sequence[str], flag: str) -> Optional[str]:
        if flag in argv:
            idx = argv.index(flag)
            if idx + 1 < len(argv):
                return argv[idx + 1]
        for arg in argv:
            if arg.startswith(flag + "="):
                return arg.split("=", 1)[1]
        return None

    def handle(self, argv: Sequence[str]) -> int:
        with self.logger.context("kdialog-handle"):
            self.logger.log(
                f"Handling kdialog shim request: {list(argv)}",
                include_context=True,
                location="kdialog",
            )
            if not argv:
                return self._fallback(argv, "No kdialog arguments were provided.")

            if not self._qt_available():
                return self._fallback(argv, "PySide6 is unavailable for kdialog shimming.")

            if "--yesno" in argv or "--warningyesno" in argv:
                return self._handle_yesno(argv)
            if "--msgbox" in argv or "--sorry" in argv or "--error" in argv:
                return self._handle_message(argv)
            if "--getopenfilename" in argv:
                return self._handle_getopenfilename(argv)

            return self._fallback(argv, "The requested kdialog mode is not supported by the shim.")

    def _handle_yesno(self, argv: Sequence[str]) -> int:
        from PySide6 import QtWidgets  # type: ignore
        from ap_bizhelper.ap_bizhelper_ap import _ensure_qt_app, _qt_question_dialog

        _ensure_qt_app()
        text = self._extract_value(argv, "--yesno") or self._extract_value(
            argv, "--warningyesno"
        )
        title = self._extract_value(argv, "--title") or "Question"
        QtWidgets.QApplication.instance()
        choice = _qt_question_dialog(
            title=title, text=text or "", ok_label="Yes", cancel_label="No"
        )
        self.logger.log(
            f"kdialog yes/no selection: {choice or 'cancelled'} (title={title!r})",
            include_context=True,
            location="kdialog-yesno",
        )
        return 0 if choice == "ok" else 1

    def _handle_message(self, argv: Sequence[str]) -> int:
        from PySide6 import QtWidgets  # type: ignore
        from ap_bizhelper.ap_bizhelper_ap import _ensure_qt_app

        _ensure_qt_app()
        box = QtWidgets.QMessageBox()
        box.setWindowTitle(self._extract_value(argv, "--title") or "Message")
        if "--error" in argv:
            box.setIcon(QtWidgets.QMessageBox.Critical)
        elif "--sorry" in argv:
            box.setIcon(QtWidgets.QMessageBox.Warning)
        else:
            box.setIcon(QtWidgets.QMessageBox.Information)
        box.setText(
            self._extract_value(argv, "--msgbox")
            or self._extract_value(argv, "--error")
            or self._extract_value(argv, "--sorry")
            or ""
        )
        box.exec()
        self.logger.log(
            f"kdialog message displayed with title={box.windowTitle()!r}",
            include_context=True,
            location="kdialog-message",
        )
        return 0

    def _handle_getopenfilename(self, argv: Sequence[str]) -> int:
        from ap_bizhelper.ap_bizhelper_ap import (
            _load_settings,
            _preferred_start_dir,
            _qt_file_dialog,
            _remember_file_dialog_dir,
            _save_settings,
        )

        try:
            start_index = argv.index("--getopenfilename") + 1
            start_dir = Path(argv[start_index]) if start_index < len(argv) else Path.cwd()
        except ValueError:
            start_dir = Path.cwd()
        file_filter = None
        try:
            filter_index = argv.index("--getopenfilename") + 2
            if filter_index < len(argv):
                file_filter = argv[filter_index]
        except ValueError:
            pass

        settings = _load_settings()
        start_dir = _preferred_start_dir(start_dir, settings, "shim")

        selection = _qt_file_dialog(
            title=self._extract_value(argv, "--title") or "Select file",
            start_dir=start_dir,
            file_filter=file_filter,
            settings=settings,
        )
        if selection:
            _remember_file_dialog_dir(settings, selection, "shim")
            _save_settings(settings)
            sys.stdout.write(str(selection) + "\n")
            self.logger.log(
                f"kdialog file selection: {selection}", include_context=True, location="kdialog-open"
            )
            return 0
        self.logger.log(
            "kdialog file selection cancelled", include_context=True, location="kdialog-open"
        )
        return 1

    def _fallback(self, argv: Sequence[str], reason: str) -> int:
        if self.real_kdialog:
            try:
                return subprocess.call([self.real_kdialog, *argv])
            except Exception:
                pass
        sys.stderr.write(reason + "\n")
        return 127


class PortalShim:
    """Minimal xdg-desktop-portal shim focused on FileChooser."""

    def __init__(self, real_portal: Optional[str] = None) -> None:
        self.real_portal = real_portal
        self.logger = _logger()

    def _qt_available(self) -> bool:
        return importlib.util.find_spec("PySide6") is not None

    def handle(self, argv: Sequence[str]) -> int:
        with self.logger.context("portal-handle"):
            self.logger.log(
                f"Handling portal shim request: {list(argv)}",
                include_context=True,
                location="portal",
            )
            if argv and argv[0] in {"--help", "-h"}:
                sys.stdout.write("ap-bizhelper portal shim (FileChooser only)\n")
                return 0

            if self._qt_available():
                if argv and argv[0] in {"--choose-file", "--choose-multiple"}:
                    return self._handle_choose_file(argv)

            return self._fallback(argv)

    def _handle_choose_file(self, argv: Sequence[str]) -> int:
        from ap_bizhelper.ap_bizhelper_ap import (
            _load_settings,
            _preferred_start_dir,
            _qt_file_dialog,
            _remember_file_dialog_dir,
            _save_settings,
        )

        start_dir = Path(argv[1]) if len(argv) > 1 else Path.cwd()
        settings = _load_settings()
        start_dir = _preferred_start_dir(start_dir, settings, "shim")
        selection = _qt_file_dialog(
            title="Select file",
            start_dir=start_dir,
            file_filter=None,
            settings=settings,
        )
        if selection:
            _remember_file_dialog_dir(settings, selection, "shim")
            _save_settings(settings)
            sys.stdout.write(str(selection) + "\n")
            self.logger.log(
                f"portal file selection: {selection}", include_context=True, location="portal"
            )
            return 0
        self.logger.log("portal file selection cancelled", include_context=True, location="portal")
        return 1

    def _fallback(self, argv: Sequence[str]) -> int:
        if self.real_portal:
            try:
                self.logger.log(
                    f"Delegating portal shim to real portal: {self.real_portal}",
                    include_context=True,
                    location="portal-fallback",
                )
                return subprocess.call([self.real_portal, *argv])
            except Exception:
                pass
        sys.stderr.write(
            "xdg-desktop-portal shim could not handle the request and no real portal was found.\n"
        )
        return 127

def prepare_zenity_shim_env(logger: Optional[AppLogger] = None) -> Optional[Dict[str, str]]:
    """Create a temporary zenity shim script and return environment overrides.

    The returned mapping can be merged into a subprocess environment to force
    child processes to use the shimmed zenity. If the shim cannot be created,
    ``None`` is returned.
    """

    try:
        shim_dir = Path(tempfile.mkdtemp(prefix="ap-bizhelper-zenity-"))
    except Exception:
        return None

    search_path = os.environ.get("PATH", "")
    cleaned_path = os.pathsep.join(
        [p for p in search_path.split(os.pathsep) if p and Path(p) != shim_dir]
    )

    real_zenity = shutil.which("zenity", path=cleaned_path)
    real_kdialog = shutil.which("kdialog", path=cleaned_path)
    real_portal = shutil.which("xdg-desktop-portal", path=cleaned_path)

    zenity_path = shim_dir / "zenity"
    zenity_content = """#!/usr/bin/env python3
from ap_bizhelper.zenity_shim import shim_main
if __name__ == "__main__":
    shim_main()
"""
    zenity_path.write_text(zenity_content, encoding="utf-8")
    zenity_path.chmod(0o755)

    kdialog_path = shim_dir / "kdialog"
    kdialog_content = """#!/usr/bin/env python3
from ap_bizhelper.zenity_shim import kdialog_main
if __name__ == "__main__":
    kdialog_main()
"""
    kdialog_path.write_text(kdialog_content, encoding="utf-8")
    kdialog_path.chmod(0o755)

    portal_path = shim_dir / "xdg-desktop-portal"
    portal_content = """#!/usr/bin/env python3
from ap_bizhelper.zenity_shim import portal_file_chooser_main
if __name__ == "__main__":
    portal_file_chooser_main()
"""
    portal_path.write_text(portal_content, encoding="utf-8")
    portal_path.chmod(0o755)

    pkg_root = Path(__file__).resolve().parent.parent
    pythonpath = os.environ.get("PYTHONPATH", "")
    env = {
        "PATH": shim_dir.as_posix() + os.pathsep + os.environ.get("PATH", ""),
        "PYTHONPATH": pkg_root.as_posix()
        + (os.pathsep + pythonpath if pythonpath else ""),
        _REAL_ZENITY_ENV: real_zenity or "",
        _REAL_KDIALOG_ENV: real_kdialog or "",
        _REAL_PORTAL_ENV: real_portal or "",
        "AP_BIZHELPER_SHIM_DIR": shim_dir.as_posix(),
    }
    session_env: Dict[str, str] = {}
    if logger:
        session_env = logger.session_environ()
        env[SHIM_LOG_ENV] = str(logger.component_log_path("zenity-shim", subdir="shim"))
    else:
        if RUN_ID_ENV in os.environ:
            session_env[RUN_ID_ENV] = os.environ[RUN_ID_ENV]
        if TIMESTAMP_ENV in os.environ:
            session_env[TIMESTAMP_ENV] = os.environ[TIMESTAMP_ENV]
        if SHIM_LOG_ENV in os.environ:
            env[SHIM_LOG_ENV] = os.environ[SHIM_LOG_ENV]
    env.update({k: v for k, v in session_env.items() if v})
    return env


def shim_main() -> None:
    logger = _logger()
    with logger.context("shim-main"):
        logger.log(
            f"zenity shim entrypoint argv={sys.argv[1:]}", include_context=True, location="entry"
        )
        shim = ZenityShim(real_zenity=os.environ.get(_REAL_ZENITY_ENV) or None)
        sys.exit(shim.handle(sys.argv[1:]))


def kdialog_main() -> None:
    logger = _logger()
    with logger.context("kdialog-main"):
        logger.log(
            f"kdialog shim entrypoint argv={sys.argv[1:]}", include_context=True, location="entry"
        )
        shim = KDialogShim(real_kdialog=os.environ.get(_REAL_KDIALOG_ENV) or None)
        sys.exit(shim.handle(sys.argv[1:]))


def portal_file_chooser_main() -> None:
    logger = _logger()
    with logger.context("portal-main"):
        logger.log(
            f"portal shim entrypoint argv={sys.argv[1:]}", include_context=True, location="entry"
        )
        shim = PortalShim(real_portal=os.environ.get(_REAL_PORTAL_ENV) or None)
        sys.exit(shim.handle(sys.argv[1:]))


if __name__ == "__main__":
    shim_main()
