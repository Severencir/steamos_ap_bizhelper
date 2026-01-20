#!/usr/bin/env python3

from __future__ import annotations

import csv
import json
import re
import shutil
import tempfile
import time
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
    DialogButtonSpec,
    modular_dialog,
    question_dialog as _qt_question_dialog,
    select_file_dialog as _select_file_dialog,
    error_dialog,
    info_dialog,
)
from .ap_bizhelper_config import load_apworld_cache, save_apworld_cache
from .ap_bizhelper_apindex import (
    get_cached_index_mapping,
    lookup_index_candidate,
    lookup_index_candidate_cached,
    lookup_index_candidate_live,
)
from .ap_bizhelper_core_worlds import (
    build_core_worlds_from_appimage,
    get_cached_core_worlds,
    update_cached_core_worlds,
)
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
SHEET_CACHE_KEY = "sheet_cache"
SHEET_CACHE_TS_KEY = "fetched_at"
SHEET_CACHE_MAP_KEY = "mapping"
SELECTED_APWORLD_MISSING_MSG = "Selected .apworld file does not exist."
SLASH = "/"
SOURCE_KEY = "source"
HOME_KEY = "home"
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


def _build_playable_map(rows: list[list[str]]) -> dict[str, str]:
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


def _get_playable_map() -> Optional[dict[str, str]]:
    rows = _fetch_sheet(PLAYABLE_SHEET_NAME)
    if not rows:
        return None
    mapping = _build_playable_map(rows)
    return mapping or None


def _get_cached_sheet_map(cache: Dict[str, Any]) -> Optional[dict[str, str]]:
    entry = cache.get(SHEET_CACHE_KEY)
    if not isinstance(entry, dict):
        return None
    mapping = entry.get(SHEET_CACHE_MAP_KEY)
    if not isinstance(mapping, dict):
        return None
    cleaned: dict[str, str] = {}
    for key, value in mapping.items():
        if not isinstance(key, str) or not key.strip():
            continue
        if not isinstance(value, str) or not value.strip():
            continue
        cleaned[_normalize_game(key)] = value.strip()
    return cleaned


def _update_sheet_cache(cache: Dict[str, Any], mapping: dict[str, str]) -> None:
    cache[SHEET_CACHE_KEY] = {
        SHEET_CACHE_TS_KEY: time.time(),
        SHEET_CACHE_MAP_KEY: dict(sorted(mapping.items())),
    }


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
    home: Optional[str] = None,
) -> None:
    entry = dict(playable_cache.get(normalized, {}))
    entry.update(
        {
            FILENAME_KEY: filename,
            VERSION_KEY: version,
            SOURCE_KEY: source,
        }
    )
    if home is not None:
        entry[HOME_KEY] = home
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
                    home=str(existing.get(HOME_KEY, EMPTY_STRING) or EMPTY_STRING),
                )
            return True
        except Exception as exc:  # pragma: no cover - filesystem edge cases
            error_dialog(f"Failed to copy {apworld_path.name}{COLON_SPACE}{exc}")
    else:
        error_dialog(SELECTED_APWORLD_MISSING_MSG)

    return False


def ensure_apworld_for_patch(
    patch: Path,
    appimage: Optional[Path] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> None:
    settings = settings or {}

    game = _read_archipelago_game(patch)
    normalized = _normalize_game(game) if game else EMPTY_STRING
    display_name = game or (
        f"{DOT}{patch.suffix.lstrip(DOT)} extension" if patch.suffix else "this game"
    )

    cache = load_apworld_cache()
    playable_cache = cache.get(PLAYABLE_WORLDS_KEY, {})
    if not isinstance(playable_cache, dict):
        playable_cache = {}

    # 1) Core worlds: infer from the managed AppImage (cached per AppImage fingerprint).
    core_worlds = set()
    if normalized:
        # Backwards-compatible fallback: previously we cached core-verified worlds from the sheet.
        legacy = cache.get(CORE_VERIFIED_KEY)
        if isinstance(legacy, list):
            core_worlds.update(_normalize_game(v) for v in legacy if isinstance(v, str) and v.strip())

        if appimage is not None and appimage.is_file():
            cached_core = get_cached_core_worlds(cache, appimage, settings)
            if cached_core is not None:
                core_worlds.update(cached_core)
            else:
                APP_LOGGER.log(
                    f"Error: core worlds cache missing for {appimage}.",
                    include_context=True,
                )
                try:
                    computed = build_core_worlds_from_appimage(appimage)
                except Exception as exc:
                    APP_LOGGER.log(
                        f"Error: failed to compute core worlds: {exc}",
                        include_context=True,
                    )
                    computed = set()
                if computed:
                    core_worlds.update(computed)
                    update_cached_core_worlds(cache, appimage, settings, sorted(core_worlds))
                    # Keep the legacy key around so older installs still benefit.
                    cache[CORE_VERIFIED_KEY] = sorted(core_worlds)
                    save_apworld_cache(cache)
                    APP_LOGGER.log("Core worlds cache populated from AppImage.", include_context=True)

        if normalized in core_worlds:
            APP_LOGGER.log(
                f"Core worlds cache hit (core): {display_name}.",
                include_context=True,
            )
            return
        if core_worlds:
            APP_LOGGER.log(
                f"Core worlds cache hit (non-core): {display_name}.",
                include_context=True,
            )

    cached_info = playable_cache.get(normalized, {}) if normalized else {}
    cached_name = str(cached_info.get(FILENAME_KEY, EMPTY_STRING) or EMPTY_STRING)
    cached_version = str(cached_info.get(VERSION_KEY, EMPTY_STRING) or EMPTY_STRING)
    cached_source = str(cached_info.get(SOURCE_KEY, EMPTY_STRING) or EMPTY_STRING)
    cached_home = str(cached_info.get(HOME_KEY, EMPTY_STRING) or EMPTY_STRING)
    cached_latest_seen = str(cached_info.get(LATEST_SEEN_VERSION_KEY, EMPTY_STRING) or EMPTY_STRING)

    # 2) If we already staged the APWorld file previously, we're done.
    if normalized and cached_name:
        existing = WORLD_DIR / cached_name
        if existing.is_file():
            return

    def _log_lookup(label: str, message: str) -> None:
        APP_LOGGER.log(f"{label}: {message}", include_context=True)

    # 3) Resolve an APWorld source: cached override -> sheet -> index -> sheet cache -> index cache.
    source = cached_source
    home = cached_home
    pinned_version = EMPTY_STRING
    pinned_filename = EMPTY_STRING

    if source:
        _log_lookup("Cached source", f"using stored APWorld source for {display_name}.")

    if not source and normalized:
        rows = _fetch_sheet(PLAYABLE_SHEET_NAME)
        if rows is None:
            _log_lookup("Sheet lookup", f"Error fetching sheet for {display_name}.")
        else:
            playable_map = _build_playable_map(rows)
            _update_sheet_cache(cache, playable_map)
            save_apworld_cache(cache)
            source = playable_map.get(normalized, EMPTY_STRING)
            if source:
                _log_lookup("Sheet lookup", f"found APWorld source for {display_name}.")
                if GITHUB_DOMAIN in source:
                    home = source
            else:
                _log_lookup("Sheet lookup", f"no entry found for {display_name}.")

    if not source and normalized:
        candidate, refreshed = lookup_index_candidate_live(cache, normalized)
        if refreshed:
            save_apworld_cache(cache)
        if candidate:
            _log_lookup("Index lookup", f"found APWorld source for {display_name}.")
            source = candidate.download_url
            pinned_version = candidate.version
            pinned_filename = candidate.filename
            home = candidate.home
        else:
            if refreshed:
                _log_lookup("Index lookup", f"no entry found for {display_name}.")
            else:
                _log_lookup("Index lookup", f"Error fetching index for {display_name}.")

    if not source and normalized:
        cached_sheet_map = _get_cached_sheet_map(cache)
        if cached_sheet_map is None:
            _log_lookup(
                "Sheet cache",
                f"Warning: cache missing; skipping cached lookup for {display_name}.",
            )
        else:
            source = cached_sheet_map.get(normalized, EMPTY_STRING)
            if source:
                _log_lookup("Sheet cache", f"found APWorld source for {display_name}.")
                if GITHUB_DOMAIN in source:
                    home = source
            else:
                _log_lookup("Sheet cache", f"no entry found for {display_name}.")

    if not source and normalized:
        cached_index_map = get_cached_index_mapping(cache)
        if cached_index_map is None:
            _log_lookup(
                "Index cache",
                f"Warning: cache missing; skipping cached lookup for {display_name}.",
            )
        else:
            candidate = lookup_index_candidate_cached(cache, normalized)
            if candidate:
                _log_lookup("Index cache", f"found APWorld source for {display_name}.")
                source = candidate.download_url
                pinned_version = candidate.version
                pinned_filename = candidate.filename
                home = candidate.home
            else:
                _log_lookup("Index cache", f"no entry found for {display_name}.")

    if not source:
        normalized_fallback = normalized or _normalize_game(patch.stem)
        _log_lookup("Manual fallback", f"prompting for local APWorld for {display_name}.")
        _select_custom_apworld(display_name, normalized_fallback, cache, playable_cache)
        return

    def _present_choice(
        *,
        title: str,
        text: str,
        latest_label: Optional[str] = None,
        indexed_label: Optional[str] = None,
    ) -> str:
        # Returns one of: "Latest", "Indexed", "Select", "Cancel".
        if latest_label and indexed_label:
            result = modular_dialog(
                title=title,
                text=text,
                icon="question",
                buttons=[
                    DialogButtonSpec(latest_label, role="positive", is_default=True),
                    DialogButtonSpec(indexed_label, role="special"),
                    DialogButtonSpec("Use local .apworld", role="neutral"),
                    DialogButtonSpec("Cancel", role="negative"),
                ],
            )
            label = (result.label or "").strip()
            if label == latest_label:
                return "Latest"
            if label == indexed_label:
                return "Indexed"
            if label == "Use local .apworld":
                return "Select"
            return "Cancel"

        choice = choose_install_action(title, text, select_label="Use local .apworld")
        if choice == "Download":
            return "Latest"
        if choice == "Select":
            return "Select"
        return "Cancel"

    def _download_and_stage(
        url: str,
        *,
        asset_name: str,
        version_tag: str,
        source_value: str,
        home_value: str,
        latest_seen: str,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="apworld_dl_") as tmp:
            tmpdir = Path(tmp)
            tmp_path = tmpdir / asset_name
            download_with_progress(
                url,
                tmp_path,
                title=f"Download {asset_name}",
                text=f"Downloading APWorld for {display_name}...",
                settings=settings,
            )
            staged = _stage_apworld_file(tmp_path, asset_name)
            if staged is None:
                raise RuntimeError(FAILED_STAGE_APWORLD_MSG)

            if normalized:
                _update_playable_cache(
                    cache,
                    playable_cache,
                    normalized,
                    staged.name,
                    version_tag,
                    source_value,
                    latest_seen=latest_seen or None,
                    home=home_value or None,
                )

    # 4) Resolve download candidates.
    latest_url = EMPTY_STRING
    latest_ver = EMPTY_STRING
    latest_name = EMPTY_STRING

    pinned_url = EMPTY_STRING
    pinned_ver = pinned_version
    pinned_name = pinned_filename

    # Case A: source is a GitHub repo -> "latest" is the only meaningful candidate.
    if GITHUB_DOMAIN in source:
        home = source
        latest = _github_latest_apworld(source)
        if latest:
            latest_url, latest_ver, latest_name = latest
            pinned_url = latest_url
            pinned_ver = latest_ver
            pinned_name = latest_name
    else:
        pinned_url = source
        if not pinned_name:
            pinned_name = Path(pinned_url.split("?", 1)[0]).name or APWORLD_FILENAME_DEFAULT
        if not pinned_ver:
            pinned_ver = cached_version

        # If we have a GitHub home (from the index), also compute latest.
        if home and GITHUB_DOMAIN in home:
            latest = _github_latest_apworld(home)
            if latest:
                latest_url, latest_ver, latest_name = latest

    # Track latest-seen for update prompts.
    if normalized and latest_ver:
        if _is_newer_version(latest_ver, cached_latest_seen):
            _update_playable_latest_seen(cache, playable_cache, normalized, latest_ver)
            cached_latest_seen = latest_ver

    # If the cached version is already present, short-circuit.
    if normalized and cached_version and cached_name and cached_version == pinned_ver:
        if (WORLD_DIR / cached_name).is_file():
            return

    # 5) Prompt user.
    lines = [f"Found APWorld for {display_name}."]
    if home and home != source:
        lines.append(f"Source: {home}")
    else:
        lines.append(f"Source: {source}")

    latest_label = None
    indexed_label = None

    # Offer both index-pinned and GitHub latest when they differ.
    offer_both = bool(
        latest_url
        and pinned_url
        and pinned_url != latest_url
        and pinned_ver
        and latest_ver
        and _is_newer_version(latest_ver, pinned_ver)
    )

    if offer_both:
        lines.append("")
        lines.append(f"Indexed version: {pinned_ver}")
        lines.append(f"Latest release: {latest_ver}")
        latest_label = f"Download latest ({latest_ver})"
        indexed_label = f"Download indexed ({pinned_ver})"
    elif pinned_ver:
        lines.append(f"Version: {pinned_ver}")

    text_prompt = "\n".join(lines)
    title = f"{APWORLD_TITLE_PREFIX}{display_name}"
    action = _present_choice(
        title=title,
        text=text_prompt,
        latest_label=latest_label,
        indexed_label=indexed_label,
    )

    if action == "Select":
        # Persist manual choice.
        if _select_custom_apworld(display_name, normalized, cache, playable_cache):
            _update_playable_cache(
                cache,
                playable_cache,
                normalized,
                str(playable_cache.get(normalized, {}).get(FILENAME_KEY, "") or ""),
                str(playable_cache.get(normalized, {}).get(VERSION_KEY, "") or ""),
                MANUAL_SOURCE,
                home=str(playable_cache.get(normalized, {}).get(HOME_KEY, "") or "") or None,
            )
        return

    if action == "Cancel":
        return

    # Latest vs Indexed selection.
    try:
        if offer_both and action == "Indexed":
            _download_and_stage(
                pinned_url,
                asset_name=pinned_name or APWORLD_FILENAME_DEFAULT,
                version_tag=pinned_ver or EMPTY_STRING,
                source_value=pinned_url,
                home_value=home,
                latest_seen=cached_latest_seen,
            )
            return

        # Default: download latest (or pinned if latest is unavailable).
        url = latest_url or pinned_url
        ver = latest_ver or pinned_ver or EMPTY_STRING
        name = latest_name or pinned_name or APWORLD_FILENAME_DEFAULT
        source_value = home if (home and GITHUB_DOMAIN in home and url == latest_url) else pinned_url
        _download_and_stage(
            url,
            asset_name=name,
            version_tag=ver,
            source_value=source_value,
            home_value=home,
            latest_seen=cached_latest_seen,
        )
    except Exception as exc:
        error_dialog(f"Failed to download APWorld for {display_name}{COLON_SPACE}{exc}")


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
        home = str(entry.get(HOME_KEY, EMPTY_STRING) or EMPTY_STRING)
        latest_seen = str(entry.get(LATEST_SEEN_VERSION_KEY, EMPTY_STRING) or EMPTY_STRING)

        # Resolve source if missing (sheet preferred; index fallback).
        if not source or source == MANUAL_SOURCE:
            if playable_map is None:
                playable_map = _get_playable_map() or {}
            resolved_source = playable_map.get(normalized, EMPTY_STRING) if playable_map else EMPTY_STRING
            if not resolved_source:
                candidate = lookup_index_candidate(cache, normalized)
                if candidate:
                    resolved_source = candidate.download_url
                    if not home:
                        home = candidate.home
            if not resolved_source:
                continue
            source = resolved_source
            if not home and source and GITHUB_DOMAIN in source:
                home = source
            _update_playable_cache(
                cache,
                playable_cache,
                normalized,
                filename,
                version,
                source,
                latest_seen=latest_seen or None,
                home=home or None,
            )
            entry = playable_cache.get(normalized, entry)

        # Prefer GitHub latest when we have a repo home or the source itself is a repo.
        repo = EMPTY_STRING
        if source and GITHUB_DOMAIN in source:
            repo = source
        elif home and GITHUB_DOMAIN in home:
            repo = home

        if repo:
            latest = _github_latest_apworld(repo)
            if not latest:
                continue
            download_url, version_tag, asset_name = latest
            if version_tag:
                _update_playable_latest_seen(cache, playable_cache, normalized, version_tag)
            # Already up-to-date?
            if version_tag and version_tag == version and (WORLD_DIR / asset_name).is_file():
                continue
            dest = _download_apworld(download_url, asset_name)
            if dest is None:
                continue
            version_value = version_tag or _read_apworld_version(dest) or version
            _update_playable_cache(
                cache,
                playable_cache,
                normalized,
                dest.name,
                version_value,
                # Keep the repo as the canonical source for future updates.
                repo,
                latest_seen=version_tag or version_value or None,
                home=repo,
            )
            updated = True
            continue

        # Direct download URL.
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
            home=home or None,
        )
        updated = True


    if updated:
        info_dialog("APWorlds refreshed.")
    return updated
