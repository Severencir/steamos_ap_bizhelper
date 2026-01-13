"""ap-bizhelper package initialization and CLI entry point."""
from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ap_bizhelper import main as _main


def console_main() -> None:
    """Entry point for console_scripts and python -m execution."""
    from .ap_bizhelper import main

    raise SystemExit(main(sys.argv))


__all__ = ["console_main"]
