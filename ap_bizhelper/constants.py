from __future__ import annotations

from pathlib import Path


APP_NAME = "ap-bizhelper"
USER_AGENT = f"{APP_NAME}/1.0"
CONFIG_DIR = Path.home() / ".config" / APP_NAME
DATA_DIR = Path.home() / ".local" / "share" / APP_NAME
LOG_ROOT = DATA_DIR / "logs"
LOG_PREFIX = f"[{APP_NAME}]"

