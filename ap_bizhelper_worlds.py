#!/usr/bin/env python3

from __future__ import annotations

import csv
import json
import re
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ap_bizhelper_ap import download_with_progress, info_dialog
from ap_bizhelper_config import load_settings, save_settings

SPREADSHEET_ID = "1iuzDTOAvdoNe8Ne8i461qGNucg5OuEoF-Ikqs8aUQZw"
CORE_SHEET_NAME = "Core-Verified Worlds"
PLAYABLE_SHEET_NAME = "Playable Worlds"
WORLD_DIR = Path.home() / ".local/share/Archipelago/worlds"
CACHE_KEY = "APWORLD_CACHE"
USER_AGENT = {"User-Agent": "ap-bizhelper/1.0"}


def _normalize_game(name: str) -> str:
    return name.strip().lower()


def _fetch_sheet(sheet_name: str) -> Optional[list[list[str]]]:
    quoted = urllib.parse.quote(sheet_name)
    url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/gviz/tq?tqx=out:csv&sheet={quoted}"
    try:
        req = urllib.request.Request(url, headers=USER_AGENT)
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None

    rows: list[list[str]] = []
    for row in csv.reader(content.splitlines()):
        rows.append(row)
    return rows


def _get_core_games() -> Optional[set[str]]:
    rows = _fetch_sheet(CORE_SHEET_NAME)
    if not rows:
        return None
    games: set[str] = set()
    for row in rows:
        if not row:
            continue
        name = row[0].strip()
        if not name or name.lower() == "game":
            continue
        games.add(_normalize_game(name))
    return games


def _extract_single_link(cell: str) -> Optional[str]:
    if not cell:
        return None
    links = re.findall(r"https?://[^\s]+", cell)
    if len(links) != 1:
        match = re.search(r"APWorld:\s*(https?://[^\s]+)", cell, re.IGNORECASE)
        if match:
            return match.group(1)
        return None
    return links[0]


def _get_playable_map() -> Optional[dict[str, str]]:
    rows = _fetch_sheet(PLAYABLE_SHEET_NAME)
    if not rows:
        return None
    mapping: dict[str, str] = {}
    for row in rows:
        if not row or len(row) < 3:
            continue
        name = row[0].strip()
        if not name or name.lower() == "game":
            continue
        link = _extract_single_link(row[2].strip()) if len(row) >= 3 else None
        if link:
            mapping[_normalize_game(name)] = link
    return mapping


def _read_archipelago_game(patch: Path) -> Optional[str]:
    try:
        with zipfile.ZipFile(patch) as zf:
            with zf.open("archipelago.json") as f:
                data = json.load(f)
    except (FileNotFoundError, KeyError, zipfile.BadZipFile, json.JSONDecodeError, OSError):
        return None

    game = data.get("game")
    if isinstance(game, str) and game.strip():
        return game.strip()
    return None


def _github_latest_apworld(repo_url: str) -> Optional[Tuple[str, str, str]]:
    match = re.match(r"https?://github.com/([^/]+)/([^/#?]+)", repo_url)
    if not match:
        return None
    owner, repo = match.group(1), match.group(2).rstrip('/')
    api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    try:
        req = urllib.request.Request(api_url, headers=USER_AGENT)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    tag = data.get("tag_name") or ""
    assets = data.get("assets") or []
    for asset in assets:
        name = asset.get("name") or ""
        download_url = asset.get("browser_download_url")
        if name.lower().endswith(".apworld") and download_url:
            return download_url, tag, name
    return None


def _download_apworld(url: str, dest_name: Optional[str]) -> Optional[Path]:
    WORLD_DIR.mkdir(parents=True, exist_ok=True)
    filename = dest_name or Path(urllib.parse.urlparse(url).path).name or "world.apworld"
    if not filename.lower().endswith(".apworld"):
        filename = f"{filename}.apworld"
    dest = WORLD_DIR / filename
    try:
        download_with_progress(url, dest, title="Downloading APWorld", text=filename)
    except Exception:
        return None
    return dest if dest.is_file() else None


def _ensure_from_github(repo_url: str, cached_version: str) -> Optional[Tuple[Path, str, str]]:
    latest = _github_latest_apworld(repo_url)
    if latest is None:
        return None
    download_url, version, name = latest
    if cached_version and cached_version == version and (WORLD_DIR / name).is_file():
        return WORLD_DIR / name, version, repo_url

    dest = _download_apworld(download_url, name)
    if dest is None:
        return None
    return dest, version, repo_url


def _ensure_from_direct(url: str, cached_name: Optional[str]) -> Optional[Tuple[Path, str, str]]:
    filename = cached_name or Path(urllib.parse.urlparse(url).path).name
    if filename:
        candidate = WORLD_DIR / (filename if filename.lower().endswith(".apworld") else f"{filename}.apworld")
        if candidate.is_file():
            return candidate, "", url

    dest = _download_apworld(url, cached_name)
    if dest is None:
        return None
    return dest, "", url


def _ensure_playable(game: str, link: str, cached: Dict[str, Any]) -> bool:
    normalized = _normalize_game(game)
    cached_version = str(cached.get("version", "") or "")
    cached_name = str(cached.get("filename", "") or "") or None

    if "github.com" in link:
        result = _ensure_from_github(link, cached_version)
    else:
        result = _ensure_from_direct(link, cached_name)

    if result is None:
        return False

    dest, version, source = result
    cache = load_settings().get(CACHE_KEY, {})
    playable = cache.get("playable_worlds", {})
    playable[normalized] = {
        "filename": dest.name,
        "version": version,
        "source": source,
    }
    cache["playable_worlds"] = playable
    settings = load_settings()
    settings[CACHE_KEY] = cache
    save_settings(settings)
    info_dialog(f"Installed APWorld for {game}: {dest.name}")
    return True


def ensure_apworld_for_patch(patch: Path) -> bool:
    game = _read_archipelago_game(patch)
    if not game:
        return False

    normalized = _normalize_game(game)
    settings = load_settings()
    cache = settings.get(CACHE_KEY, {})
    core_cached = set(cache.get("core_verified", []))
    playable_cache = cache.get("playable_worlds", {})

    if normalized in core_cached:
        return True

    if normalized in playable_cache:
        info = playable_cache.get(normalized, {})
        link = str(info.get("source", "") or "")
        if link:
            success = _ensure_playable(game, link, info)
            if success:
                return True

    core_games = _get_core_games()
    if core_games is not None and normalized in core_games:
        core_cached.add(normalized)
        cache["core_verified"] = sorted(core_cached)
        settings[CACHE_KEY] = cache
        save_settings(settings)
        return True

    playable_map = _get_playable_map()
    if playable_map and normalized in playable_map:
        link = playable_map[normalized]
        success = _ensure_playable(game, link, playable_cache.get(normalized, {}))
        if success:
            cache = load_settings().get(CACHE_KEY, {})
            playable = cache.get("playable_worlds", {})
            playable[normalized] = {
                "filename": playable.get(normalized, {}).get("filename", ""),
                "version": playable.get(normalized, {}).get("version", ""),
                "source": link,
            }
            cache["playable_worlds"] = playable
            settings = load_settings()
            settings[CACHE_KEY] = cache
            save_settings(settings)
            return True

    return False
