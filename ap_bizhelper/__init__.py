"""ap-bizhelper package initialization and CLI entry point."""
from __future__ import annotations

import sys

from .ap_bizhelper import main


def console_main() -> None:
    """Entry point for console_scripts and python -m execution."""
    raise SystemExit(main(sys.argv))


__all__ = ["console_main", "main"]
