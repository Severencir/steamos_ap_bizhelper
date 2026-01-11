from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .ap_bizhelper_config import PATH_SETTINGS_DEFAULTS
from .constants import BIZHAWK_HELPERS_ROOT_KEY


def _apply_defaults(settings: dict[str, Any], defaults: dict[str, Any]) -> None:
    for key, value in defaults.items():
        if key not in settings:
            settings[key] = copy.deepcopy(value)


def get_helpers_root(settings: dict[str, Any]) -> Path:
    _apply_defaults(
        settings,
        {BIZHAWK_HELPERS_ROOT_KEY: PATH_SETTINGS_DEFAULTS[BIZHAWK_HELPERS_ROOT_KEY]},
    )
    return Path(settings[BIZHAWK_HELPERS_ROOT_KEY])
