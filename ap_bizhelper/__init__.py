"""ap-bizhelper package initialization and CLI entry point."""
from __future__ import annotations

import sys
from pathlib import Path


def _add_bundled_site() -> None:
    """Add the bundled dependency directory to ``sys.path`` if present."""

    bundle_path = Path(sys.argv[0]).resolve().with_suffix(".deps")
    if bundle_path.is_dir():
        sys.path.insert(0, str(bundle_path))


_add_bundled_site()

from .ap_bizhelper import main


def console_main() -> None:
    """Entry point for console_scripts and python -m execution."""
    raise SystemExit(main(sys.argv))


__all__ = ["console_main", "main"]
