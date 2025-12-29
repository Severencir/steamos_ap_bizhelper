from __future__ import annotations

from pathlib import Path


APP_NAME = "ap-bizhelper"
USER_AGENT = f"{APP_NAME}/1.0"
USER_AGENT_HEADER = "User-Agent"

HOME_DIR = Path.home()
LOCAL_CONFIG_DIR = HOME_DIR / ".config"
LOCAL_SHARE_DIR = HOME_DIR / ".local" / "share"
DESKTOP_DIR = HOME_DIR / "Desktop"
DOWNLOADS_DIR = HOME_DIR / "Downloads"

CONFIG_DIR = LOCAL_CONFIG_DIR / APP_NAME
DATA_DIR = LOCAL_SHARE_DIR / APP_NAME
LOG_ROOT = DATA_DIR / "logs"
LOG_PREFIX = f"[{APP_NAME}]"

ARCHIPELAGO_CONFIG_DIR = LOCAL_CONFIG_DIR / "Archipelago"
ARCHIPELAGO_DATA_DIR = LOCAL_SHARE_DIR / "Archipelago"
ARCHIPELAGO_WORLDS_DIR = ARCHIPELAGO_DATA_DIR / "worlds"

APPLICATIONS_DIR = LOCAL_SHARE_DIR / "applications"
MIME_PACKAGES_DIR = LOCAL_SHARE_DIR / "mime" / "packages"
STEAM_ROOT_DIR = LOCAL_SHARE_DIR / "Steam"

BACKUPS_DIR = DATA_DIR / "backups"
GAME_SAVES_DIR = DATA_DIR / "saves"
PROTON_PREFIX = DATA_DIR / "proton_prefix"

FILE_FILTER_APWORLD = "*.apworld"
FILE_FILTER_ARCHIVE = "*.zip *.tar.gz"
FILE_FILTER_EXE = "*.exe"
FILE_FILTER_ZIP = "*.zip"
