#!/usr/bin/env python3

"""Archipelago-index integration.

Provides best-effort lookup of a world name (as appears in AP patch metadata
and YAML) to a downloadable .apworld from the community Archipelago-index
registry.

We keep this conservative:
- Treat the index as a fallback source.
- Cache parsed results in the apworld cache.
- Support direct URLs, default_url templating, and "local" files stored in
  the index repo.

If anything fails (network, parse, unexpected schema), lookups simply return
None and the caller can fall back to manual selection.
"""

from __future__ import annotations

import io
import time
import zipfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .ap_bizhelper_ap import _is_newer_version
from .constants import USER_AGENT, USER_AGENT_HEADER

try:  # Python 3.11+
    import tomllib  # type: ignore
except Exception:  # pragma: no cover
    tomllib = None  # type: ignore


INDEX_REPO_ZIP = "https://github.com/Eijebong/Archipelago-index/archive/refs/heads/main.zip"
RAW_BASE = "https://raw.githubusercontent.com/Eijebong/Archipelago-index/main/"
INDEX_CACHE_KEY = "apindex_cache"
INDEX_CACHE_TS_KEY = "fetched_at"
INDEX_CACHE_MAP_KEY = "mapping"
INDEX_CACHE_TTL_SECONDS = 60 * 60 * 24  # 24h


@dataclass(frozen=True)
class IndexCandidate:
    download_url: str
    version: str
    filename: str
    home: str


def _normalize(name: str) -> str:
    return name.strip().lower()


def _read_url(url: str, timeout: int = 30) -> Optional[bytes]:
    try:
        req = urllib.request.Request(url, headers={USER_AGENT_HEADER: USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        return None


def _select_latest_version(versions: Dict[str, Any]) -> Optional[str]:
    latest: Optional[str] = None
    for v in versions.keys():
        if not isinstance(v, str):
            continue
        if latest is None or _is_newer_version(v, latest):
            latest = v
    return latest


def _resolve_version_source(
    *,
    version: str,
    entry: Dict[str, Any],
    default_url: str,
) -> Optional[str]:
    # Version entry may specify url or local.
    url = entry.get("url") if isinstance(entry, dict) else None
    if isinstance(url, str) and url.strip():
        return url.strip()

    local = entry.get("local") if isinstance(entry, dict) else None
    if isinstance(local, str) and local.strip():
        # Index uses paths like "../apworlds/foo.apworld".
        local_path = local.strip().replace("\\", "/")
        while local_path.startswith("../"):
            local_path = local_path[3:]
        if local_path.startswith("/"):
            local_path = local_path[1:]
        return RAW_BASE + local_path

    if default_url and isinstance(default_url, str):
        templ = default_url
        if "{{version}}" in templ:
            return templ.replace("{{version}}", version)
        if "{version}" in templ:
            return templ.replace("{version}", version)

    return None


def _parse_index_toml(blob: bytes) -> Optional[Dict[str, Any]]:
    if tomllib is None:
        return None
    try:
        return tomllib.loads(blob.decode("utf-8", errors="replace"))
    except Exception:
        return None


def _build_mapping_from_zip(zip_bytes: bytes) -> Dict[str, Dict[str, str]]:
    """Return mapping: normalized name -> {download_url, version, filename, home}."""

    mapping: Dict[str, Dict[str, str]] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            # Path looks like: Archipelago-index-main/index/<id>.toml
            if not name.endswith(".toml"):
                continue
            parts = name.split("/")
            if len(parts) < 3:
                continue
            if parts[-2] != "index":
                continue
            try:
                blob = zf.read(name)
            except Exception:
                continue
            data = _parse_index_toml(blob)
            if not isinstance(data, dict):
                continue

            world_name = data.get("name")
            if not isinstance(world_name, str) or not world_name.strip():
                continue
            normalized = _normalize(world_name)

            versions = data.get("versions")
            if not isinstance(versions, dict) or not versions:
                continue

            default_url = data.get("default_url")
            default_url = default_url if isinstance(default_url, str) else ""

            latest_ver = _select_latest_version(versions)
            if not latest_ver:
                continue

            latest_entry = versions.get(latest_ver)
            if not isinstance(latest_entry, dict):
                latest_entry = {}

            resolved_url = _resolve_version_source(
                version=latest_ver, entry=latest_entry, default_url=default_url
            )
            if not resolved_url:
                continue

            filename = Path(resolved_url.split("?", 1)[0]).name
            if not filename:
                filename = "world.apworld"

            home = data.get("home")
            home = home.strip() if isinstance(home, str) else ""

            mapping[normalized] = {
                "download_url": resolved_url,
                "version": latest_ver,
                "filename": filename,
                "home": home,
            }

    return mapping


def _get_cached_mapping(cache: Dict[str, Any]) -> Optional[Dict[str, Dict[str, str]]]:
    entry = cache.get(INDEX_CACHE_KEY)
    if not isinstance(entry, dict):
        return None
    ts = entry.get(INDEX_CACHE_TS_KEY)
    if not isinstance(ts, (int, float)):
        return None
    if (time.time() - float(ts)) > INDEX_CACHE_TTL_SECONDS:
        return None
    mapping = entry.get(INDEX_CACHE_MAP_KEY)
    if not isinstance(mapping, dict):
        return None
    # Ensure mapping values are dicts.
    cleaned: Dict[str, Dict[str, str]] = {}
    for k, v in mapping.items():
        if not isinstance(k, str) or not isinstance(v, dict):
            continue
        cleaned[k] = {str(kk): str(vv) for kk, vv in v.items()}
    return cleaned


def _refresh_mapping(cache: Dict[str, Any]) -> Optional[Dict[str, Dict[str, str]]]:
    zip_bytes = _read_url(INDEX_REPO_ZIP)
    if not zip_bytes:
        return None
    mapping = _build_mapping_from_zip(zip_bytes)
    if not mapping:
        return None
    cache[INDEX_CACHE_KEY] = {
        INDEX_CACHE_TS_KEY: time.time(),
        INDEX_CACHE_MAP_KEY: mapping,
    }
    return mapping


def lookup_index_candidate(cache: Dict[str, Any], normalized_game: str) -> Optional[IndexCandidate]:
    """Lookup the latest index entry for ``normalized_game``.

    ``normalized_game`` should already be normalized (lower/strip).

    This mutates ``cache`` in-memory by updating the index cache when refreshed.
    Callers typically persist via save_apworld_cache().
    """

    normalized_game = _normalize(normalized_game)
    if not normalized_game:
        return None

    mapping = _get_cached_mapping(cache)
    if mapping is None:
        mapping = _refresh_mapping(cache)
    if not mapping:
        return None

    info = mapping.get(normalized_game)
    if not isinstance(info, dict):
        return None

    url = str(info.get("download_url", "") or "").strip()
    ver = str(info.get("version", "") or "").strip()
    filename = str(info.get("filename", "") or "").strip()
    home = str(info.get("home", "") or "").strip()

    if not url:
        return None
    if not filename:
        filename = Path(url.split("?", 1)[0]).name or "world.apworld"

    return IndexCandidate(download_url=url, version=ver, filename=filename, home=home)
