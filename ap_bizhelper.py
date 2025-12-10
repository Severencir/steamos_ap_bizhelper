#!/usr/bin/env python3

from __future__ import annotations

import sys
from pathlib import Path

from ap_bizhelper_ap import ensure_appimage
from ap_bizhelper_bizhawk import ensure_bizhawk_and_proton
from ap_bizhelper_config import load_settings, save_settings
from ap_bizhelper_sni import download_sni_if_needed


def _ensure_sni(settings: dict) -> None:
    exe_str = str(settings.get("BIZHAWK_EXE", "") or "")
    if not exe_str:
        return
    exe_path = Path(exe_str)
    if not exe_path.is_file():
        return
    download_sni_if_needed(exe_path.parent)


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] != "ensure":
        print("Usage: ap_bizhelper.py ensure", file=sys.stderr)
        return 1

    # Order mirrors the legacy script: Archipelago setup, BizHawk/Proton setup, then SNI.
    try:
        ensure_appimage()
    except RuntimeError:
        return 1

    settings = load_settings()
    bizhawk_result = ensure_bizhawk_and_proton()
    if bizhawk_result is None:
        return 1

    # Refresh settings after BizHawk changes and ensure SNI is staged inside BizHawk.
    settings = load_settings()
    _ensure_sni(settings)
    save_settings(settings)

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
