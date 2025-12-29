#!/usr/bin/env python3

from __future__ import annotations

import csv
import json
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .ap_bizhelper_ap import (
    APP_LOGGER,
    _is_newer_version,
    choose_install_action,
    download_with_progress,
)
from .dialogs import (
    question_dialog as _qt_question_dialog,
    select_file_dialog as _select_file_dialog,
    error_dialog,
    info_dialog,
)
from .ap_bizhelper_config import load_apworld_cache, save_apworld_cache
from .constants import ARCHIPELAGO_WORLDS_DIR, FILE_FILTER_APWORLD, USER_AGENT, USER_AGENT_HEADER

SPREADSHEET_ID = "1iuzDTOAvdoNe8Ne8i461qGNucg5OuEoF-Ikqs8aUQZw"
CORE_SHEET_NAME = "Core-Verified Worlds"
PLAYABLE_SHEET_NAME = "Playable Worlds"
WORLD_DIR = ARCHIPELAGO_WORLDS_DIR
CACHE_KEY = "APWORLD_CACHE"
GITHUB_ACCEPT_HEADER = "application/vnd.github+json"
USER_AGENT_HEADERS = {USER_AGENT_HEADER: USER_AGENT}
GITHUB_API_HEADERS = {**USER_AGENT_HEADERS, "Accept": GITHUB_ACCEPT_HEADER}
APWORLD_EXTENSION = ".apworld"
APWORLD_FILENAME_DEFAULT = "world.apworld"
APWORLD_FILE_FILTER = FILE_FILTER_APWORLD
APWORLD_DIALOG_KEY = "apworld"
APWORLD_TITLE_PREFIX = "APWorld for "
APWORLD_FILE_PROMPT = "Select .apworld file"
ARCHIPELAGO_METADATA_FILE = "archipelago.json"
COLON_SPACE = ": "
CORE_VERIFIED_KEY = "core_verified"
DOT = "."
EMPTY_STRING = ""
ENCODING_UTF8 = "utf-8"
FILENAME_KEY = "filename"
GAME_KEY = "game"
GAME_NAME_KEY = "game_name"
GITHUB_DOMAIN = "github.com"
GITHUB_RELEASES_URL_TEMPLATE = "https://api.github.com/repos/{owner}/{repo}/releases/latest"
LATEST_SEEN_VERSION_KEY = "latest_seen_version"
MANUAL_SOURCE = "manual"
PLAYABLE_WORLDS_KEY = "playable_worlds"
SELECTED_APWORLD_MISSING_MSG = "Selected .apworld file does not exist."
SLASH = "/"
SOURCE_KEY = "source"
VERSION_KEY = "version"
WORLD_DIR_COPY_LOCATION = "apworld-copy"
FAILED_STAGE_APWORLD_MSG = "Failed to stage .apworld file"


def _normalize_game(name: str) -> str:
    return name.strip().lower()


def _read_url(url: str, headers: Dict[str, str]) -> Optional[bytes]:
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception:
        return None


def _fetch_sheet(sheet_name: str) -> Optional[list[list[str]]]:
    quoted = urllib.parse.quote(sheet_name)
    url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/gviz/tq?tqx=out:csv&sheet={quoted}"
    data = _read_url(url, USER_AGENT_HEADERS)
    if data is None:
        return None
    content = data.decode(ENCODING_UTF8, errors="replace")

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
        if not name or name.lower() == GAME_KEY:
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
        if not name or name.lower() == GAME_KEY:
            continue
        link = _extract_single_link(row[2].strip()) if len(row) >= 3 else None
        if link:
            mapping[_normalize_game(name)] = link
    return mapping


def _read_archipelago_game(patch: Path) -> Optional[str]:
    metadata = _read_archipelago_metadata(patch)
    game = metadata.get(GAME_KEY)
    if isinstance(game, str) and game.strip():
        return game.strip()
    return None


def _read_archipelago_metadata(archive_path: Path) -> Dict[str, Any]:
    try:
        with zipfile.ZipFile(archive_path) as zf:
            with zf.open(ARCHIPELAGO_METADATA_FILE) as f:
                data = json.load(f)
    except (FileNotFoundError, KeyError, zipfile.BadZipFile, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_apworld_game(apworld_path: Path) -> Optional[str]:
    metadata = _read_archipelago_metadata(apworld_path)
    for key in (GAME_KEY, GAME_NAME_KEY):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _read_apworld_version(apworld_path: Path) -> str:
    metadata = _read_archipelago_metadata(apworld_path)
    for key in (VERSION_KEY, "world_version"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return EMPTY_STRING


def _github_latest_apworld(repo_url: str) -> Optional[Tuple[str, str, str]]:
    match = re.match(r"https?://github.com/([^/]+)/([^/#?]+)", repo_url)
    if not match:
        return None
    owner, repo = match.group(1), match.group(2).rstrip(SLASH)
    api_url = GITHUB_RELEASES_URL_TEMPLATE.format(owner=owner, repo=repo)
    raw_data = _read_url(api_url, GITHUB_API_HEADERS)
    if raw_data is None:
        return None
    try:
        data = json.loads(raw_data.decode(ENCODING_UTF8))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    tag = data.get("tag_name") or EMPTY_STRING
    assets = data.get("assets") or []
    for asset in assets:
        name = asset.get("name") or EMPTY_STRING
        download_url = asset.get("browser_download_url")
        if name.lower().endswith(APWORLD_EXTENSION) and download_url:
            return download_url, tag, name
    return None


def _stage_apworld_file(apworld_path: Path, dest_name: Optional[str]) -> Optional[Path]:
    WORLD_DIR.mkdir(parents=True, exist_ok=True)
    filename = dest_name or apworld_path.name or APWORLD_FILENAME_DEFAULT
    if not filename.lower().endswith(APWORLD_EXTENSION):
        filename = f"{filename}{APWORLD_EXTENSION}"
    dest = WORLD_DIR / filename
    try:
        shutil.copy2(apworld_path, dest)
    except Exception:
        return None
    return dest if dest.is_file() else None


def _download_apworld(url: str, dest_name: Optional[str]) -> Optional[Path]:
    filename = dest_name or Path(urllib.parse.urlparse(url).path).name or APWORLD_FILENAME_DEFAULT
    if not filename.lower().endswith(APWORLD_EXTENSION):
        filename = f"{filename}{APWORLD_EXTENSION}"
    with tempfile.NamedTemporaryFile(delete=False, suffix=APWORLD_EXTENSION) as tmpf:
        tmp_path = Path(tmpf.name)
    try:
        download_with_progress(
            url,
            tmp_path,
            title="Downloading APWorld",
            text=filename,
        )
        return _stage_apworld_file(tmp_path, filename)
    except Exception:
        return None
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass


def _update_playable_cache(
    cache: Dict[str, Any],
    playable_cache: Dict[str, Any],
    normalized: str,
    filename: str,
    version: str,
    source: str,
    latest_seen: Optional[str] = None,
) -> None:
    entry = dict(playable_cache.get(normalized, {}))
    entry.update(
        {
            FILENAME_KEY: filename,
            VERSION_KEY: version,
            SOURCE_KEY: source,
        }
    )
    if latest_seen is not None:
        entry[LATEST_SEEN_VERSION_KEY] = latest_seen
    playable_cache[normalized] = entry
    cache[PLAYABLE_WORLDS_KEY] = playable_cache
    save_apworld_cache(cache)


def _update_playable_latest_seen(
    cache: Dict[str, Any],
    playable_cache: Dict[str, Any],
    normalized: str,
    latest_seen: str,
) -> None:
    if not normalized:
        return
    entry = dict(playable_cache.get(normalized, {}))
    entry[LATEST_SEEN_VERSION_KEY] = latest_seen
    playable_cache[normalized] = entry
    cache[PLAYABLE_WORLDS_KEY] = playable_cache
    save_apworld_cache(cache)


def _select_custom_apworld(
    title: str,
    normalized: str,
    cache: Dict[str, Any],
    playable_cache: Dict[str, Any],
    target_override: Optional[str] = None,
) -> bool:
    selection = _select_file_dialog(
        title=f"{APWORLD_FILE_PROMPT} for {title}",
        initial=Path.home(),
        file_filter=APWORLD_FILE_FILTER,
        dialog_key=APWORLD_DIALOG_KEY,
    )
    if selection is None:
        return False

    apworld_path = Path(selection)
    if apworld_path.is_file():
        try:
            dest = _stage_apworld_file(apworld_path, apworld_path.name)
            if dest is None:
                raise RuntimeError(FAILED_STAGE_APWORLD_MSG)
            APP_LOGGER.log(
                f"Copied {apworld_path.name} to {WORLD_DIR}",
                include_context=True,
                location=WORLD_DIR_COPY_LOCATION,
            )
            target_key = target_override or normalized
            if target_key:
                existing = playable_cache.get(target_key, {})
                version = _read_apworld_version(dest)
                latest_seen = str(existing.get(LATEST_SEEN_VERSION_KEY, EMPTY_STRING) or EMPTY_STRING)
                if version and not latest_seen:
                    latest_seen = version
                _update_playable_cache(
                    cache,
                    playable_cache,
                    target_key,
                    dest.name,
                    version or str(existing.get(VERSION_KEY, EMPTY_STRING) or EMPTY_STRING),
                    str(existing.get(SOURCE_KEY, EMPTY_STRING) or EMPTY_STRING),
                    latest_seen=latest_seen or None,
                )
            return True
        except Exception as exc:  # pragma: no cover - filesystem edge cases
            error_dialog(f"Failed to copy {apworld_path.name}{COLON_SPACE}{exc}")
    else:
        error_dialog(SELECTED_APWORLD_MISSING_MSG)

    return False


def ensure_apworld_for_patch(patch: Path) -> None:
    game = _read_archipelago_game(patch)
    normalized = _normalize_game(game) if game else EMPTY_STRING
    display_name = game or (
        f"{DOT}{patch.suffix.lstrip(DOT)} extension" if patch.suffix else "this game"
    )

    cache = load_apworld_cache()
    core_cached = set(cache.get(CORE_VERIFIED_KEY, []))
    playable_cache = cache.get(PLAYABLE_WORLDS_KEY, {})

    if normalized and normalized in core_cached:
        return

    cached_info = playable_cache.get(normalized, {}) if normalized else {}
    cached_name = str(cached_info.get(FILENAME_KEY, EMPTY_STRING) or EMPTY_STRING)
    cached_version = str(cached_info.get(VERSION_KEY, EMPTY_STRING) or EMPTY_STRING)
    cached_source = str(cached_info.get(SOURCE_KEY, EMPTY_STRING) or EMPTY_STRING)
    cached_latest_seen = str(
        cached_info.get(LATEST_SEEN_VERSION_KEY, EMPTY_STRING) or EMPTY_STRING
    )

    if normalized and cached_name:
        existing = WORLD_DIR / cached_name
        if existing.is_file():
            return

    core_games = _get_core_games()
    if normalized and core_games is not None and normalized in core_games:
        core_cached.add(normalized)
        cache[CORE_VERIFIED_KEY] = sorted(core_cached)
        save_apworld_cache(cache)
        return

    playable_map = _get_playable_map()
    link = cached_source or (
        playable_map.get(normalized, EMPTY_STRING) if playable_map and normalized else EMPTY_STRING
    )

    download_candidate: Optional[Tuple[str, str, str, str]] = None
    if link:
        if GITHUB_DOMAIN in link:
            latest = _github_latest_apworld(link)
            if latest:
                download_url, version_tag, asset_name = latest
                should_prompt = _is_newer_version(version_tag, cached_latest_seen)
                _update_playable_latest_seen(
                    cache, playable_cache, normalized, version_tag
                )
                filename = asset_name
                if cached_version == version_tag and (WORLD_DIR / asset_name).is_file():
                    _update_playable_cache(
                        cache, playable_cache, normalized, asset_name, version_tag, link
                    )
                    return
                if not should_prompt:
                    return
                download_candidate = (download_url, version_tag, asset_name, link)
        else:
            filename = cached_name or Path(urllib.parse.urlparse(link).path).name or APWORLD_FILENAME_DEFAULT
            candidate_name = (
                filename if filename.lower().endswith(APWORLD_EXTENSION) else f"{filename}{APWORLD_EXTENSION}"
            )
            if (WORLD_DIR / candidate_name).is_file():
                _update_playable_cache(cache, playable_cache, normalized, candidate_name, cached_version, link)
                return
            download_candidate = (link, EMPTY_STRING, candidate_name, link)

    if download_candidate:
        download_url, version_tag, dest_name, source = download_candidate
        version_phrase = f"version {version_tag}" if version_tag else "a download"
        action = choose_install_action(
            f"{APWORLD_TITLE_PREFIX}{display_name}",
            (
                f"An APWorld {version_phrase} was found for {display_name}.\n\n"
                "Would you like to download it automatically, select your own .apworld file, or cancel?"
            ),
            select_label="Use local .apworld",
        )
        if action == "Download":
            dest = _download_apworld(download_url, dest_name)
            if dest is None:
                return
            if normalized:
                version_value = version_tag or _read_apworld_version(dest)
                latest_seen = version_tag or version_value
                _update_playable_cache(
                    cache,
                    playable_cache,
                    normalized,
                    dest.name,
                    version_value,
                    source,
                    latest_seen=latest_seen or None,
                )
            info_dialog(f"Installed {APWORLD_TITLE_PREFIX}{display_name}{COLON_SPACE}{dest.name}")
        elif action == "Select":
            _select_custom_apworld(display_name, normalized, cache, playable_cache)
        return

    choice = _qt_question_dialog(
        title=f"{APWORLD_TITLE_PREFIX}{display_name}",
        text=(
            f"No downloadable APWorld was found for {display_name}.\n\n"
            "Do you want to select a .apworld file now or cancel?"
        ),
        ok_label="Select .apworld",
        cancel_label="Cancel",
    )
    if choice == "ok":
        _select_custom_apworld(display_name, normalized, cache, playable_cache)
    return


def manual_select_apworld(normalized_override: Optional[str] = None) -> bool:
    """Prompt for a local .apworld file and record it in the cache."""

    selection = _select_file_dialog(
        title=APWORLD_FILE_PROMPT,
        initial=Path.home(),
        file_filter=APWORLD_FILE_FILTER,
        dialog_key=APWORLD_DIALOG_KEY,
    )
    if selection is None:
        return False

    apworld_path = Path(selection)
    if not apworld_path.is_file():
        error_dialog(SELECTED_APWORLD_MISSING_MSG)
        return False

    dest = _stage_apworld_file(apworld_path, apworld_path.name)
    if dest is None:
        error_dialog(FAILED_STAGE_APWORLD_MSG + DOT)
        return False

    cache = load_apworld_cache()
    playable_cache = cache.get(PLAYABLE_WORLDS_KEY, {})
    game_name = _read_apworld_game(dest)
    if normalized_override:
        normalized = normalized_override
    elif game_name:
        normalized = _normalize_game(game_name)
    else:
        normalized = _normalize_game(dest.stem)
    version = _read_apworld_version(dest)
    _update_playable_cache(
        cache,
        playable_cache,
        normalized,
        dest.name,
        version or MANUAL_SOURCE,
        MANUAL_SOURCE,
        latest_seen=version or None,
    )
    info_dialog(f"Installed APWorld: {dest.name}")
    return True


def _resolve_force_update_target(
    normalized: str, entry: Dict[str, Any]
) -> tuple[str, Dict[str, Any]]:
    filename = str(entry.get(FILENAME_KEY, EMPTY_STRING) or EMPTY_STRING)
    if not filename:
        return normalized, entry
    apworld_path = WORLD_DIR / filename
    if not apworld_path.is_file():
        return normalized, entry
    game_name = _read_apworld_game(apworld_path)
    if not game_name:
        return normalized, entry
    resolved = _normalize_game(game_name)
    if not resolved or resolved == normalized:
        return normalized, entry
    return resolved, entry


def force_update_apworlds(normalized_override: Optional[str] = None) -> bool:
    """Force a refresh of cached APWorld downloads."""

    cache = load_apworld_cache()
    playable_cache = cache.get(PLAYABLE_WORLDS_KEY, {})
    updated = False
    playable_map: Optional[dict[str, str]] = None

    entries = list(playable_cache.items())
    if normalized_override:
        entry = playable_cache.get(normalized_override)
        entries = [(normalized_override, entry)] if entry else []

    for normalized, entry in entries:
        if not entry:
            continue
        resolved_normalized, resolved_entry = _resolve_force_update_target(normalized, entry)
        if resolved_normalized != normalized:
            playable_cache.pop(normalized, None)
            playable_cache[resolved_normalized] = resolved_entry
            cache[PLAYABLE_WORLDS_KEY] = playable_cache
            save_apworld_cache(cache)
            normalized = resolved_normalized
            entry = resolved_entry

        version = str(entry.get(VERSION_KEY, EMPTY_STRING) or EMPTY_STRING)
        filename = str(entry.get(FILENAME_KEY, EMPTY_STRING) or EMPTY_STRING)
        source = str(entry.get(SOURCE_KEY, EMPTY_STRING) or EMPTY_STRING)
        if not source or source == MANUAL_SOURCE:
            if playable_map is None:
                playable_map = _get_playable_map() or {}
            resolved_source = (
                playable_map.get(normalized, EMPTY_STRING) if playable_map else EMPTY_STRING
            )
            if not resolved_source:
                continue
            source = resolved_source
            _update_playable_cache(
                cache,
                playable_cache,
                normalized,
                filename,
                version,
                source,
                latest_seen=str(entry.get(LATEST_SEEN_VERSION_KEY, EMPTY_STRING) or EMPTY_STRING)
                or None,
            )
            entry = playable_cache.get(normalized, entry)
        if GITHUB_DOMAIN in source:
            latest = _github_latest_apworld(source)
            if not latest:
                continue
            download_url, version_tag, asset_name = latest
            _update_playable_latest_seen(cache, playable_cache, normalized, version_tag)
            if version_tag and version_tag == version and (WORLD_DIR / asset_name).is_file():
                continue
            dest = _download_apworld(download_url, asset_name)
            if dest is None:
                continue
            version_value = version_tag or _read_apworld_version(dest)
            _update_playable_cache(
                cache,
                playable_cache,
                normalized,
                dest.name,
                version_value,
                source,
                latest_seen=version_tag or version_value or None,
            )
            updated = True
        else:
            if filename and (WORLD_DIR / filename).is_file():
                continue
            dest = _download_apworld(source, filename or None)
            if dest is None:
                continue
            version_value = _read_apworld_version(dest) or version
            _update_playable_cache(
                cache,
                playable_cache,
                normalized,
                dest.name,
                version_value,
                source,
                latest_seen=version_value or None,
            )
            updated = True

    if updated:
        info_dialog("APWorlds refreshed.")
    return updated
