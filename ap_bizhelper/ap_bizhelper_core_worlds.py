#!/usr/bin/env python3

"""Core-world discovery for an Archipelago AppImage.

The goal is to build a set of "core" game/world names included in the user's
managed Archipelago install, so we can avoid prompting for an external .apworld
when it isn't needed.

Strategy (best-effort):
1) Extract the AppImage (`--appimage-extract`) into a temporary directory.
2) Try a lightweight introspection run that imports Archipelago's world registry
   and prints the registered game keys.
3) If introspection fails (missing python, import error, etc.), fall back to a
   static scan of the extracted filesystem for worlds.

This runs at most once per AppImage fingerprint and is cached in apworld_cache.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Set

from .constants import USER_AGENT, USER_AGENT_HEADER

CORE_WORLDS_CACHE_KEY = "core_worlds_cache"
CORE_WORLDS_LIST_KEY = "worlds"
CORE_WORLDS_FINGERPRINT_KEY = "fingerprint"


def _normalize(name: str) -> str:
    return name.strip().lower()


def _appimage_fingerprint(appimage: Path, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return a cheap fingerprint for caching.

    We use path + stat values. This is not cryptographically strong, but is
    sufficient to detect typical AppImage updates.
    """

    try:
        st = appimage.stat()
        size = int(st.st_size)
        mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
    except Exception:
        size = 0
        mtime_ns = 0

    fp: Dict[str, Any] = {
        "path": str(appimage),
        "size": size,
        "mtime_ns": mtime_ns,
    }
    # If the caller passes settings containing an explicit AP version, include it.
    if settings:
        ver = settings.get("AP_VERSION") or settings.get("AP_VERSION_KEY")
        if isinstance(ver, str) and ver.strip():
            fp["ap_version"] = ver.strip()
    return fp


def get_cached_core_worlds(
    cache: Dict[str, Any],
    appimage: Path,
    settings: Optional[Dict[str, Any]] = None,
) -> Optional[Set[str]]:
    entry = cache.get(CORE_WORLDS_CACHE_KEY)
    if not isinstance(entry, dict):
        return None
    fp = entry.get(CORE_WORLDS_FINGERPRINT_KEY)
    if not isinstance(fp, dict):
        return None
    if fp != _appimage_fingerprint(appimage, settings):
        return None
    worlds = entry.get(CORE_WORLDS_LIST_KEY)
    if not isinstance(worlds, list):
        return None
    out: Set[str] = set()
    for v in worlds:
        if isinstance(v, str) and v.strip():
            out.add(_normalize(v))
    return out


def update_cached_core_worlds(
    cache: Dict[str, Any],
    appimage: Path,
    settings: Optional[Dict[str, Any]],
    core_worlds: Iterable[str],
) -> None:
    cache[CORE_WORLDS_CACHE_KEY] = {
        CORE_WORLDS_FINGERPRINT_KEY: _appimage_fingerprint(appimage, settings),
        CORE_WORLDS_LIST_KEY: sorted({_normalize(v) for v in core_worlds if isinstance(v, str) and v.strip()}),
    }


def _extract_appimage(appimage: Path, dest_dir: Path) -> Optional[Path]:
    """Extract the AppImage into dest_dir and return squashfs-root path."""

    # AppImage extraction creates squashfs-root in the working directory.
    cmd = [str(appimage), "--appimage-extract"]
    try:
        subprocess.run(
            cmd,
            cwd=str(dest_dir),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None

    root = dest_dir / "squashfs-root"
    return root if root.is_dir() else None


def _find_first_dir(root: Path, rel_candidates: list[str]) -> Optional[Path]:
    for rel in rel_candidates:
        p = root / rel
        if p.is_dir():
            return p
    return None


def _find_site_packages(root: Path) -> list[Path]:
    out: list[Path] = []
    for base in (root / "usr" / "lib", root / "usr" / "lib64"):
        if not base.is_dir():
            continue
        for py in base.glob("python*/"):
            for sp in (py / "site-packages", py / "dist-packages"):
                if sp.is_dir():
                    out.append(sp)
    return out


def _static_scan_world_names(extracted_root: Path) -> Set[str]:
    """Heuristic scan for included worlds without executing Archipelago code."""

    worlds: Set[str] = set()

    # 1) Python module worlds: look for a 'worlds' package directory.
    for sp in _find_site_packages(extracted_root):
        wdir = sp / "worlds"
        if not wdir.is_dir():
            continue
        for child in wdir.iterdir():
            if not child.is_dir():
                continue
            name = child.name
            if name.startswith("_"):
                continue
            worlds.add(_normalize(name))

    # 2) Packaged .apworlds within the install (official docs reference lib/worlds/*.apworld).
    for libbase in (extracted_root / "lib", extracted_root / "usr" / "lib", extracted_root / "usr" / "share"):
        if not libbase.exists():
            continue
        for apworld in libbase.rglob("*.apworld"):
            worlds.add(_normalize(apworld.stem))

    return worlds


def _try_introspect_world_names(extracted_root: Path) -> Optional[Set[str]]:
    """Try to run a small snippet with the AppImage's embedded python, if any."""

    site_packages = _find_site_packages(extracted_root)
    if not site_packages:
        return None

    # Attempt to locate an embedded python.
    py_candidates = [
        extracted_root / "usr" / "bin" / "python3",
        extracted_root / "usr" / "bin" / "python",
    ]
    python_exe = next((p for p in py_candidates if p.is_file() and os.access(str(p), os.X_OK)), None)
    if python_exe is None:
        return None

    # Build PYTHONPATH from discovered site-packages.
    pythonpath = ":".join(str(p) for p in site_packages)

    snippet = (
        "import json\n"
        "try:\n"
        "    from worlds.AutoWorld import AutoWorldRegister\n"
        "    import worlds  # noqa: F401\n"
        "    keys = sorted(AutoWorldRegister.world_types.keys())\n"
        "except Exception:\n"
        "    keys = []\n"
        "print(json.dumps(keys))\n"
    )

    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONPATH"] = pythonpath
    env["PYTHONNOUSERSITE"] = "1"
    env[USER_AGENT_HEADER] = USER_AGENT

    try:
        proc = subprocess.run(
            [str(python_exe), "-c", snippet],
            cwd=str(extracted_root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
        out = proc.stdout.decode("utf-8", errors="replace").strip()
        keys = json.loads(out) if out else []
        if not isinstance(keys, list) or not keys:
            return None
        result: Set[str] = set()
        for k in keys:
            if isinstance(k, str) and k.strip():
                result.add(_normalize(k))
        return result if result else None
    except Exception:
        return None


def build_core_worlds_from_appimage(appimage: Path) -> Set[str]:
    """Return the set of included world names for the given AppImage."""

    if not appimage.is_file():
        return set()

    with tempfile.TemporaryDirectory(prefix="ap_extract_") as tmp:
        tmpdir = Path(tmp)
        extracted_root = _extract_appimage(appimage, tmpdir)
        if extracted_root is None:
            return set()

        # Prefer introspection when possible; fall back to static scan.
        worlds = _try_introspect_world_names(extracted_root)
        if worlds is None:
            worlds = _static_scan_world_names(extracted_root)
        return worlds or set()
