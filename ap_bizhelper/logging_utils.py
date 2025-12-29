from __future__ import annotations

import contextlib
import contextvars
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Optional
from uuid import uuid4


APP_NAME = "ap-bizhelper"
USER_AGENT = f"{APP_NAME}/1.0"
BRACKET_CLOSE = "]"
BRACKET_OPEN = "["
COLON_SPACE = ": "
DASH = "-"
FORWARD_SLASH = "/"
NEWLINE = "\n"
SPACE = " "
UNDERSCORE = "_"
LOG_ROOT = Path.home() / ".local" / "share" / APP_NAME / "logs"
RUN_ID_ENV = "AP_BIZHELPER_LOG_RUN_ID"
TIMESTAMP_ENV = "AP_BIZHELPER_LOG_TIMESTAMP"
SHIM_LOG_ENV = "AP_BIZHELPER_SHIM_LOG_PATH"
RUNNER_LOG_ENV = "AP_BIZHELPER_RUNNER_LOG_PATH"
CONTEXT_SEPARATOR = " > "
DEFAULT_CATEGORY = APP_NAME
DEFAULT_LOCATION = "root"
ENCODING_UTF8 = "utf-8"
LOG_FILE_SUFFIX = ".log"
LOG_LEVEL_ERROR = "ERROR"
LOG_LEVEL_INFO = "INFO"
STDERR_STREAM = "stderr"
STDOUT_STREAM = "stdout"
_CONTEXT_STACK: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "ap_bizhelper_context", default=()
)
_SUPPRESS_CAPTURE = contextvars.ContextVar("ap_bizhelper_suppress_capture", default=False)
_GLOBAL_LOGGER: Optional[AppLogger] = None


def _slugify(label: str) -> str:
    return label.strip().replace(SPACE, UNDERSCORE).replace(FORWARD_SLASH, DASH) or "general"


class _StreamCapture:
    """Mirror writes to a stream into the app logger with context awareness."""

    def __init__(self, logger: AppLogger, stream, level: str, location: str) -> None:
        self._logger = logger
        self._stream = stream
        self._level = level
        self._location = location

    def write(self, data: str) -> int:  # pragma: no cover - passthrough utility
        if not data:
            return 0

        if not _SUPPRESS_CAPTURE.get():
            for line in data.splitlines():
                if line.strip():
                    self._logger.log(
                        line.strip(),
                        level=self._level,
                        location=self._location,
                        include_context=True,
                        mirror_console=False,
                    )

        token = _SUPPRESS_CAPTURE.set(True)
        try:
            written = self._stream.write(data)
            self._stream.flush()
        finally:
            _SUPPRESS_CAPTURE.reset(token)
        return written

    def flush(self) -> None:  # pragma: no cover - passthrough utility
        self._stream.flush()

    @property
    def encoding(self):  # pragma: no cover - passthrough utility
        return getattr(self._stream, "encoding", None)


def _unwrap_stream(stream):
    while isinstance(stream, _StreamCapture):
        stream = stream._stream
    return stream


class AppLogger:
    """Structured application-wide logger with contextual breadcrumbs."""

    def __init__(
        self,
        category: str = DEFAULT_CATEGORY,
        *,
        log_dir: Optional[Path] = None,
        log_path: Optional[Path] = None,
        run_id: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> None:
        self.timestamp = timestamp or datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.run_id = run_id or uuid4().hex[:8]
        self.category = _slugify(category)
        base_dir = log_dir or LOG_ROOT
        base_dir.mkdir(parents=True, exist_ok=True)
        if log_path:
            self.path = Path(log_path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            stem_parts = self.path.stem.split(UNDERSCORE)
            if timestamp is None and len(stem_parts) >= 2:
                self.timestamp = stem_parts[-2]
            if run_id is None and len(stem_parts) >= 1:
                self.run_id = stem_parts[-1]
        else:
            self.path = (
                base_dir
                / f"{self.category}{UNDERSCORE}{self.timestamp}{UNDERSCORE}{self.run_id}{LOG_FILE_SUFFIX}"
            )
        self._sequence = 0
        self._original_stdout = _unwrap_stream(sys.stdout)
        self._original_stderr = _unwrap_stream(sys.stderr)

    @contextlib.contextmanager
    def context(self, label: str):
        """Push a label onto the context stack for nested call tracing."""

        stack = _CONTEXT_STACK.get()
        token = _CONTEXT_STACK.set((*stack, label))
        try:
            yield
        finally:
            _CONTEXT_STACK.reset(token)

    def _next_entry_id(self) -> str:
        self._sequence += 1
        return f"{self.run_id}{DASH}{self._sequence:04d}"

    def _context_label(self) -> str:
        stack = _CONTEXT_STACK.get()
        return CONTEXT_SEPARATOR.join(stack)

    def _write_console(self, text: str, *, stream: str = STDOUT_STREAM) -> None:
        destination = self._original_stdout if stream == STDOUT_STREAM else self._original_stderr
        token = _SUPPRESS_CAPTURE.set(True)
        try:
            destination.write(text)
            destination.flush()
        finally:
            _SUPPRESS_CAPTURE.reset(token)

    def log(
        self,
        message: str,
        *,
        level: str = LOG_LEVEL_INFO,
        location: Optional[str] = None,
        include_context: bool = False,
        mirror_console: bool = False,
        stream: str = STDOUT_STREAM,
    ) -> str:
        entry_id = self._next_entry_id()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        context_label = self._context_label()
        location_id = _slugify(
            location or (context_label.split(CONTEXT_SEPARATOR)[-1] if context_label else DEFAULT_LOCATION)
        )

        parts = [
            f"{BRACKET_OPEN}{timestamp}{BRACKET_CLOSE}",
            f"{BRACKET_OPEN}{entry_id}{BRACKET_CLOSE}",
            f"{BRACKET_OPEN}{location_id}{BRACKET_CLOSE}",
            f"{BRACKET_OPEN}{level.upper()}{BRACKET_CLOSE}",
        ]
        if include_context and context_label:
            parts.append(f"{BRACKET_OPEN}ctx:{context_label}{BRACKET_CLOSE}")
        parts.append(message)
        line = SPACE.join(parts)
        with self.path.open("a", encoding=ENCODING_UTF8) as log_file:
            log_file.write(line + NEWLINE)

        if mirror_console:
            self._write_console(line + NEWLINE, stream=stream)
        return entry_id

    def log_lines(
        self,
        prefix: str,
        lines: Iterable[str],
        *,
        level: str = LOG_LEVEL_INFO,
        location: Optional[str] = None,
    ) -> None:
        for line in lines:
            self.log(f"{prefix}{COLON_SPACE}{line}", level=level, location=location, include_context=True)

    def log_dialog(
        self,
        title: str,
        message: str,
        *,
        level: str = LOG_LEVEL_INFO,
        backend: str = "qt",
        location: Optional[str] = None,
    ) -> str:
        return self.log(
            f"Dialog{BRACKET_OPEN}{backend}{BRACKET_CLOSE}{SPACE}{title}{COLON_SPACE}{message}",
            level=level,
            location=location or f"dialog-{backend}",
            include_context=True,
        )

    def capture_console_streams(self) -> None:
        if isinstance(sys.stdout, _StreamCapture) or isinstance(sys.stderr, _StreamCapture):
            return
        sys.stdout = _StreamCapture(self, self._original_stdout, LOG_LEVEL_INFO, STDOUT_STREAM)
        sys.stderr = _StreamCapture(self, self._original_stderr, LOG_LEVEL_ERROR, STDERR_STREAM)

    def session_environ(self, *, env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        base = {} if env is None else dict(env)
        base[RUN_ID_ENV] = self.run_id
        base[TIMESTAMP_ENV] = self.timestamp
        return base

    def component_log_path(self, category: str, *, subdir: Optional[str] = None) -> Path:
        target_dir = LOG_ROOT / subdir if subdir else LOG_ROOT
        target_dir.mkdir(parents=True, exist_ok=True)
        return (
            target_dir
            / f"{_slugify(category)}{UNDERSCORE}{self.timestamp}{UNDERSCORE}{self.run_id}{LOG_FILE_SUFFIX}"
        )

    def component_environ(
        self,
        *,
        env: Optional[Dict[str, str]] = None,
        category: str,
        subdir: Optional[str] = None,
        env_var: Optional[str] = None,
    ) -> Dict[str, str]:
        merged = self.session_environ(env=env)
        if env_var:
            merged[env_var] = str(self.component_log_path(category, subdir=subdir))
        return merged


def create_component_logger(
    category: str,
    *,
    env_var: Optional[str] = None,
    subdir: Optional[str] = None,
) -> AppLogger:
    env_path = Path(os.environ[env_var]) if env_var and os.environ.get(env_var) else None
    logger = AppLogger(
        category,
        log_dir=LOG_ROOT / subdir if subdir else None,
        log_path=env_path,
        run_id=os.environ.get(RUN_ID_ENV) or None,
        timestamp=os.environ.get(TIMESTAMP_ENV) or None,
    )
    logger.capture_console_streams()
    return logger


def get_app_logger(category: str = DEFAULT_CATEGORY, *, log_dir: Optional[Path] = None) -> AppLogger:
    global _GLOBAL_LOGGER
    if _GLOBAL_LOGGER is None:
        _GLOBAL_LOGGER = AppLogger(
            category,
            log_dir=log_dir or LOG_ROOT / "app",
            run_id=os.environ.get(RUN_ID_ENV) or None,
            timestamp=os.environ.get(TIMESTAMP_ENV) or None,
        )
        _GLOBAL_LOGGER.capture_console_streams()
    return _GLOBAL_LOGGER


__all__ = [
    "AppLogger",
    "LOG_ROOT",
    "RUNNER_LOG_ENV",
    "RUN_ID_ENV",
    "SHIM_LOG_ENV",
    "TIMESTAMP_ENV",
    "USER_AGENT",
    "create_component_logger",
    "get_app_logger",
]
