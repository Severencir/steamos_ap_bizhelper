from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable
from uuid import uuid4


LOG_ROOT = Path.home() / ".local/share/ap-bizhelper/logs"


class RunLogger:
    """Write per-run log files with uniquely identifiable entries."""

    def __init__(self, category: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.run_id = uuid4().hex[:8]
        self.category = category
        LOG_ROOT.mkdir(parents=True, exist_ok=True)
        self.path = LOG_ROOT / f"{category}_{timestamp}_{self.run_id}.log"
        self._sequence = 0

    def _next_entry_id(self) -> str:
        self._sequence += 1
        return f"{self.run_id}-{self._sequence:04d}"

    def log(self, message: str) -> str:
        entry_id = self._next_entry_id()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"[{timestamp}] [{entry_id}] {message}\n")
        return entry_id

    def log_lines(self, prefix: str, lines: Iterable[str]) -> None:
        for line in lines:
            self.log(f"{prefix}: {line}")


__all__ = ["RunLogger", "LOG_ROOT"]
