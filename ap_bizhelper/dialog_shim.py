"""Desktop dialog shim that proxies to Kivy dialogs when possible.

This module provides entry points for each shimmed dialog binary.

Unsupported commands fall back to the real binaries when present so the shim
stays transparent during gaps in coverage.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from ap_bizhelper import dialogs
from .ap_bizhelper_config import load_settings, save_settings
from .constants import (
    BIZHAWK_EXE_KEY,
    BIZHAWK_RUNNER_KEY,
    DIALOG_SHIM_KDIALOG_FILENAME,
    DIALOG_SHIM_PORTAL_FILENAME,
    DIALOG_SHIM_ZENITY_FILENAME,
    DIALOG_SHIM_REAL_KDIALOG_ENV,
    DIALOG_SHIM_REAL_PORTAL_ENV,
    DIALOG_SHIM_REAL_ZENITY_ENV,
    LAST_ROM_DIR_KEY,
    ROM_HASH_CACHE_KEY,
    ROM_ROOTS_KEY,
)
from .logging_utils import AppLogger, SHIM_LOG_ENV, create_component_logger

_REAL_ZENITY_ENV = DIALOG_SHIM_REAL_ZENITY_ENV
_REAL_KDIALOG_ENV = DIALOG_SHIM_REAL_KDIALOG_ENV
_REAL_PORTAL_ENV = DIALOG_SHIM_REAL_PORTAL_ENV
_PATCH_PATH_ENV = "AP_BIZHELPER_PATCH"
_ROM_DEBUG_ENV = "AP_BIZHELPER_DEBUG_ROM"

_ROM_AUTO_SUCCEEDED = False
_ROM_COMMON_EXTS = {
    "gba",
    "gb",
    "gbc",
    "nds",
    "nes",
    "sfc",
    "smc",
    "n64",
    "z64",
    "v64",
    "sms",
    "gg",
    "md",
    "gen",
    "pce",
    "cue",
    "iso",
}


_SHIM_LOGGER: Optional[AppLogger] = None


def _logger() -> AppLogger:
    global _SHIM_LOGGER
    if _SHIM_LOGGER is None:
        _SHIM_LOGGER = create_component_logger("zenity-shim", env_var=SHIM_LOG_ENV, subdir="shim")
    return _SHIM_LOGGER


def _rom_debug_enabled() -> bool:
    value = str(os.environ.get(_ROM_DEBUG_ENV, "")).strip().lower()
    return value not in ("", "0", "false", "no", "off")


def _rom_log(logger: AppLogger, message: str) -> None:
    if not _rom_debug_enabled():
        return
    logger.log(message, include_context=True, location="rom-auto")


def _normalize_extension(value: str) -> Optional[str]:
    if not value:
        return None
    trimmed = value.strip().lower()
    if trimmed.startswith("."):
        trimmed = trimmed[1:]
    return trimmed or None


def _extract_extensions_from_filter(filter_text: Optional[str]) -> set[str]:
    if not filter_text:
        return set()
    return {
        ext.lower()
        for ext in re.findall(r"\*\.([A-Za-z0-9]+)", filter_text)
        if ext
    }


def _is_rom_request(
    *, title: Optional[str], text: Optional[str], file_filter: Optional[str], is_save: bool
) -> bool:
    if is_save:
        return False
    haystack = " ".join([part for part in (title, text) if part]).casefold()
    rom_text = bool(re.search(r"\brom\b", haystack) or "base rom" in haystack)
    filter_exts = _extract_extensions_from_filter(file_filter)
    rom_filter = bool(filter_exts & _ROM_COMMON_EXTS)
    return rom_text or rom_filter


def _load_patch_metadata(patch_path: Path, logger: AppLogger) -> Optional[dict]:
    try:
        if not patch_path.is_file():
            _rom_log(logger, f"Patch path missing: {patch_path}")
            return None
        with zipfile.ZipFile(patch_path) as archive:
            with archive.open("archipelago.json") as handle:
                return json.load(handle)
    except KeyError:
        _rom_log(logger, "Patch archive missing archipelago.json")
    except Exception as exc:
        _rom_log(logger, f"Failed reading patch metadata: {exc}")
    return None


def _extract_patch_hints(metadata: dict) -> tuple[Optional[str], Optional[str], set[str]]:
    checksum = metadata.get("base_checksum")
    if isinstance(checksum, str):
        checksum = checksum.strip().lower()
    else:
        checksum = None
    if checksum and not re.fullmatch(r"[a-f0-9]{32}", checksum):
        checksum = None

    game_name = metadata.get("game") or metadata.get("game_name")
    if isinstance(game_name, str):
        game_name = game_name.strip()
    else:
        game_name = None

    hint_keys = (
        "result_file_ending",
        "rom_file_ending",
        "base_file_ending",
        "base_rom_file_ending",
        "base_rom_extension",
        "rom_extension",
        "file_ending",
    )
    hint_exts: set[str] = set()
    for key in hint_keys:
        value = metadata.get(key)
        if isinstance(value, str):
            normalized = _normalize_extension(value)
            if normalized:
                hint_exts.add(normalized)
    return checksum, game_name, hint_exts


def _load_rom_state(logger: AppLogger) -> tuple[dict, list[str], str, dict]:
    settings = load_settings()
    rom_roots = settings.get(ROM_ROOTS_KEY)
    if not isinstance(rom_roots, list):
        rom_roots = []
    rom_roots = [str(root) for root in rom_roots if str(root).strip()]
    last_rom_dir = settings.get(LAST_ROM_DIR_KEY)
    if not isinstance(last_rom_dir, str):
        last_rom_dir = ""
    cache = settings.get(ROM_HASH_CACHE_KEY)
    if not isinstance(cache, dict):
        cache = {}
    if not isinstance(cache.get("by_file"), dict):
        cache["by_file"] = {}
    if not isinstance(cache.get("by_hash"), dict):
        cache["by_hash"] = {}
    _rom_log(logger, f"ROM state loaded roots={rom_roots} last_dir={last_rom_dir or 'none'}")
    return settings, rom_roots, last_rom_dir, cache


def _cache_key(path: Path, size: int, mtime: float) -> str:
    return f"{path}|{size}|{int(mtime)}"


def _record_hash(cache: dict, path: Path, checksum: str, size: int, mtime: float) -> bool:
    by_file = cache.setdefault("by_file", {})
    by_hash = cache.setdefault("by_hash", {})
    key = _cache_key(path, size, mtime)
    changed = by_file.get(key) != checksum
    by_file[key] = checksum
    paths = by_hash.setdefault(checksum, [])
    if str(path) not in paths:
        paths.append(str(path))
        changed = True
    return changed


def _hash_file(path: Path, logger: AppLogger) -> Optional[str]:
    try:
        md5 = hashlib.md5()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                md5.update(chunk)
        return md5.hexdigest()
    except Exception as exc:
        _rom_log(logger, f"Failed hashing {path}: {exc}")
        return None


def _cached_matches(
    checksum: str, cache: dict, logger: AppLogger
) -> tuple[list[Path], bool]:
    by_hash = cache.get("by_hash", {})
    by_file = cache.get("by_file", {})
    if not isinstance(by_hash, dict) or not isinstance(by_file, dict):
        return [], False

    cached_paths = list(by_hash.get(checksum, []) or [])
    matches: list[Path] = []
    valid_paths: list[str] = []
    cache_updated = False
    for path_str in cached_paths:
        path = Path(path_str)
        if not path.is_file():
            cache_updated = True
            continue
        try:
            stat = path.stat()
        except Exception:
            cache_updated = True
            continue
        key = _cache_key(path, stat.st_size, stat.st_mtime)
        cached_md5 = by_file.get(key)
        if cached_md5 == checksum:
            matches.append(path)
            valid_paths.append(path_str)
            continue
        md5 = _hash_file(path, logger)
        if md5 is None:
            cache_updated = True
            continue
        cache_updated = _record_hash(cache, path, md5, stat.st_size, stat.st_mtime) or cache_updated
        if md5 == checksum:
            matches.append(path)
            valid_paths.append(path_str)

    if cache_updated:
        by_hash[checksum] = valid_paths
    return matches, cache_updated


def _scan_for_matches(
    *,
    checksum: str,
    roots: Sequence[str],
    cache: dict,
    extensions: set[str],
    logger: AppLogger,
) -> tuple[list[Path], bool]:
    matches: list[Path] = []
    cache_updated = False
    for root in roots:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        for dirpath, _, filenames in os.walk(root_path):
            for filename in filenames:
                ext = _normalize_extension(Path(filename).suffix)
                if extensions and (ext is None or ext not in extensions):
                    continue
                path = Path(dirpath) / filename
                try:
                    stat = path.stat()
                except Exception:
                    continue
                key = _cache_key(path, stat.st_size, stat.st_mtime)
                cached_md5 = cache.setdefault("by_file", {}).get(key)
                if cached_md5 is None:
                    md5 = _hash_file(path, logger)
                    if md5 is None:
                        continue
                    cache_updated = _record_hash(cache, path, md5, stat.st_size, stat.st_mtime) or cache_updated
                    cached_md5 = md5
                if cached_md5 == checksum:
                    matches.append(path)
    if matches:
        unique_matches = list(dict.fromkeys(str(match) for match in matches))
        if len(unique_matches) != len(matches):
            removed = len(matches) - len(unique_matches)
            _rom_log(logger, f"Removed {removed} duplicate ROM matches from scan results.")
        matches = [Path(match) for match in unique_matches]
    return matches, cache_updated


def _select_from_matches(
    matches: Sequence[Path], game_name: Optional[str], logger: AppLogger
) -> Optional[Path]:
    if not matches:
        return None
    title = "Select base ROM"
    description = "Multiple ROMs match the patch checksum."
    if game_name:
        description = f"{description}\nGame: {game_name}"
    items = [str(path) for path in matches]
    selection = dialogs.radio_list_dialog(
        title,
        description,
        items,
        ok_label="Use selected ROM",
        cancel_label="Cancel",
        height=400,
    )
    if not selection:
        _rom_log(logger, "User cancelled ROM match selection.")
        return None
    return Path(selection)


def _confirm_use_matched_rom(
    *,
    matches: Sequence[Path],
    game_name: Optional[str],
    logger: AppLogger,
) -> Optional[bool]:
    if not matches:
        return None
    if len(matches) == 1:
        description = f"Found a matching ROM:\n{matches[0]}"
    else:
        description = f"Found {len(matches)} matching ROMs."
    if game_name:
        description = f"{description}\nGame: {game_name}"
    choice = dialogs.question_dialog(
        title="Use matching ROM?",
        text=description,
        ok_label="Use matched ROM",
        extra_label="Choose manually",
        cancel_label="Cancel",
    )
    if choice == "ok":
        return True
    if choice == "extra":
        return False
    if choice == "cancel":
        _rom_log(logger, "User cancelled ROM match confirmation dialog.")
        return None
    _rom_log(logger, "User dismissed ROM match confirmation dialog.")
    return None


def _update_rom_state_for_manual_selection(
    settings: dict, cache: dict, selection: Path, logger: AppLogger
) -> None:
    parent = selection.parent
    roots = settings.get(ROM_ROOTS_KEY)
    if not isinstance(roots, list):
        roots = []
    parent_str = str(parent)
    if parent_str not in roots:
        roots.append(parent_str)
    settings[ROM_ROOTS_KEY] = roots
    settings[LAST_ROM_DIR_KEY] = parent_str
    try:
        stat = selection.stat()
        md5 = _hash_file(selection, logger)
        if md5:
            _record_hash(cache, selection, md5, stat.st_size, stat.st_mtime)
    except Exception as exc:
        _rom_log(logger, f"Failed updating ROM cache for {selection}: {exc}")
    settings[ROM_HASH_CACHE_KEY] = cache


def _select_rom_aware_file_dialog(
    *,
    title: str,
    text: Optional[str],
    file_filter: Optional[str],
    start_dir: Path,
    dialog_key: str,
    logger: AppLogger,
) -> Optional[Path]:
    global _ROM_AUTO_SUCCEEDED

    is_rom = _is_rom_request(
        title=title, text=text, file_filter=file_filter, is_save=False
    )
    if not is_rom:
        return dialogs.select_file_dialog(
            title=title,
            dialog_key=dialog_key,
            initial=start_dir,
            file_filter=file_filter,
        )

    # ROM dialog detected: try to auto-select via the patch checksum once per process.
    settings, rom_roots, last_rom_dir, cache = _load_rom_state(logger)
    patch_env = os.environ.get(_PATCH_PATH_ENV, "")
    patch_path = Path(patch_env) if patch_env else None

    candidate_extensions = _extract_extensions_from_filter(file_filter)
    roots = list(dict.fromkeys(rom_roots))
    if last_rom_dir and last_rom_dir not in roots:
        roots.append(last_rom_dir)

    if patch_path and patch_path.is_file() and not _ROM_AUTO_SUCCEEDED:
        metadata = _load_patch_metadata(patch_path, logger)
        if metadata:
            checksum, game_name, hint_exts = _extract_patch_hints(metadata)
            if checksum:
                candidate_extensions |= hint_exts
                if not candidate_extensions:
                    candidate_extensions = set(_ROM_COMMON_EXTS)
                _rom_log(
                    logger,
                    f"ROM auto-select checksum={checksum} roots={roots} exts={sorted(candidate_extensions)}",
                )
                matches, cache_updated = _cached_matches(checksum, cache, logger)
                if not matches:
                    scanned_matches, scan_updated = _scan_for_matches(
                        checksum=checksum,
                        roots=roots,
                        cache=cache,
                        extensions=candidate_extensions,
                        logger=logger,
                    )
                    matches = scanned_matches
                    cache_updated = cache_updated or scan_updated

                if cache_updated:
                    settings[ROM_HASH_CACHE_KEY] = cache
                    save_settings(settings)

                if matches:
                    confirm = _confirm_use_matched_rom(
                        matches=matches, game_name=game_name, logger=logger
                    )
                    if confirm is True:
                        if len(matches) == 1:
                            _ROM_AUTO_SUCCEEDED = True
                            _rom_log(logger, f"ROM auto-select matched {matches[0]}")
                            return matches[0]
                        selection = _select_from_matches(matches, game_name, logger)
                        if selection:
                            _update_rom_state_for_manual_selection(settings, cache, selection, logger)
                            save_settings(settings)
                            return selection
                    if confirm is None:
                        return None

    initial_dir = start_dir
    if last_rom_dir and Path(last_rom_dir).is_dir():
        initial_dir = Path(last_rom_dir)
    selection = dialogs.select_file_dialog(
        title=title,
        dialog_key=dialog_key,
        initial=initial_dir,
        file_filter=file_filter,
    )
    if selection:
        # Manual selection: persist ROM root/last dir and update hash cache.
        _update_rom_state_for_manual_selection(settings, cache, selection, logger)
        save_settings(settings)
    return selection


def _locate_bizhawk_runner(logger: AppLogger) -> Optional[Path]:
    try:
        settings = load_settings()
    except Exception as exc:
        logger.log(
            f"BizHawk runner discovery aborted: settings load failed ({exc}).",
            level="DEBUG",
            include_context=True,
            location="auto-answer",
        )
        return None
    runner_str = str(settings.get(BIZHAWK_RUNNER_KEY, "") or "")
    exe_str = str(settings.get(BIZHAWK_EXE_KEY, "") or "")

    candidates: List[Path] = []
    candidate_sources = []
    if runner_str:
        runner_path = Path(runner_str)
        candidates.append(runner_path)
        candidate_sources.append((runner_path, BIZHAWK_RUNNER_KEY))
    if exe_str:
        exe_candidate = Path(exe_str).parent / "run_bizhawk.py"
        candidates.append(exe_candidate)
        candidate_sources.append((exe_candidate, BIZHAWK_EXE_KEY))

    local_candidate = Path(__file__).resolve().parent / "run_bizhawk.py"
    candidates.append(local_candidate)
    candidate_sources.append((local_candidate, "local fallback"))

    logger.log(
        "BizHawk runner search candidates: "
        + ", ".join(f"{source} -> {path}" for path, source in candidate_sources),
        level="DEBUG",
        include_context=True,
        location="auto-answer",
    )

    skipped: List[str] = []

    for candidate in candidates:
        try:
            if not candidate.exists():
                skipped.append(f"{candidate} (not found)")
                logger.log(
                    f"Skipping BizHawk runner candidate {candidate}: not found.",
                    level="DEBUG",
                    include_context=True,
                    location="auto-answer",
                )
                continue
            if not candidate.is_file():
                skipped.append(f"{candidate} (not a file)")
                logger.log(
                    f"Skipping BizHawk runner candidate {candidate}: not a file.",
                    level="DEBUG",
                    include_context=True,
                    location="auto-answer",
                )
                continue
            if not os.access(candidate, os.R_OK):
                skipped.append(f"{candidate} (unreadable)")
                logger.log(
                    f"Skipping BizHawk runner candidate {candidate}: unreadable.",
                    level="DEBUG",
                    include_context=True,
                    location="auto-answer",
                )
                continue
            logger.log(
                f"Selected BizHawk runner candidate {candidate}.",
                level="DEBUG",
                include_context=True,
                location="auto-answer",
            )
            return candidate
        except Exception as exc:
            skipped.append(f"{candidate} (error: {exc})")
            logger.log(
                f"Skipping BizHawk runner candidate {candidate}: error {exc}.",
                level="DEBUG",
                include_context=True,
                location="auto-answer",
            )
            continue

    logger.log(
        "No BizHawk runner found; skipped candidates: " + "; ".join(skipped),
        level="WARNING",
        include_context=True,
        location="auto-answer",
    )
    return None


class ZenityShim:
    """Parse a zenity command and render equivalent Kivy dialogs."""

    def __init__(self, real_zenity: Optional[str] = None) -> None:
        self.real_zenity = real_zenity or self._discover_real_zenity()
        self.logger = _logger()

    def _discover_real_zenity(self) -> Optional[str]:
        shim_dir = os.environ.get("AP_BIZHELPER_SHIM_DIR", "")
        search_path = os.environ.get("PATH", "")
        if shim_dir:
            # Avoid returning the shim itself by removing the shim directory from PATH
            cleaned = os.pathsep.join(
                [p for p in search_path.split(os.pathsep) if p and Path(p) != Path(shim_dir)]
            )
        else:
            cleaned = search_path
        return shutil.which(DIALOG_SHIM_ZENITY_FILENAME, path=cleaned) or None

    def handle(self, argv: Sequence[str]) -> int:
        with self.logger.context("zenity-handle"):
            self.logger.log(
                f"Handling zenity shim request: {list(argv)}", include_context=True, location="zenity"
            )
            if not argv:
                return self._fallback(argv, "No zenity arguments were provided.")

            auto_answer = self._auto_answer_emuhawk(argv)
            if auto_answer is not None:
                return auto_answer

            mode = self._detect_mode(argv)
            if mode is None:
                return self._fallback(
                    argv,
                    "The zenity shim could not recognize the requested dialog type.",
                )

            self.logger.log(f"Detected zenity mode: {mode}", include_context=True, location="zenity")
            if mode == "question":
                return self._handle_question(argv)
            if mode == "info":
                return self._handle_message(argv, level="info")
            if mode == "error":
                return self._handle_message(argv, level="error")
            if mode == "file-selection":
                return self._handle_file_selection(argv)
            if mode == "checklist":
                return self._handle_checklist(argv)
            if mode == "progress":
                return self._handle_progress(argv)

            return self._fallback(argv, "The requested zenity mode is not yet supported.")

    def _detect_mode(self, argv: Sequence[str]) -> Optional[str]:
        if "--file-selection" in argv:
            return "file-selection"
        if "--question" in argv:
            return "question"
        if "--info" in argv:
            return "info"
        if "--error" in argv:
            return "error"
        if "--progress" in argv:
            return "progress"
        if "--list" in argv and "--checklist" in argv:
            return "checklist"
        return None

    def _auto_answer_emuhawk(self, argv: Sequence[str]) -> Optional[int]:
        title = self._extract_option(argv, "--title=")
        text = self._extract_option(argv, "--text=")

        title_matches = bool(title and "emuhawk" in title.casefold())
        text_has_hint = bool(text and any(hint in text.casefold() for hint in ("emuhawk", "bizhawk")))

        if not title_matches and not text_has_hint:
            return None

        runner = _locate_bizhawk_runner(self.logger)
        if runner is None:
            self.logger.log(
                "EmuHawk auto-answer detected but no runner could be located.",
                level="WARNING",
                include_context=True,
            )
            return None
        sys.stdout.write(str(runner) + "\n")
        self.logger.log(
            f"EmuHawk auto-answer provided runner: {runner}",
            include_context=True,
            location="auto-answer",
        )
        return 0

    def _fallback(self, argv: Sequence[str], reason: str) -> int:
        self.logger.log(
            f"Fallback invoked. Reason: {reason}. Original argv: {list(argv)}",
            level="WARNING",
            include_context=True,
            location="fallback",
        )
        real_result = self._maybe_run_real_zenity(argv)
        if real_result is not None:
            self.logger.log(
                f"Delegated to real zenity with return code {real_result}",
                include_context=True,
                location="fallback",
            )
            return real_result

        cmd = " ".join(argv) if argv else "(no arguments)"
        details = [
            "The Archipelago zenity shim could not handle the request.",
            f"Reason: {reason}",
        ]
        details.append(f"Command: {cmd}")
        self._show_error_dialog("\n".join(details))
        return 127

    def _maybe_run_real_zenity(self, argv: Sequence[str]) -> Optional[int]:
        if not self.real_zenity:
            return None
        try:
            self.logger.log(
                f"Executing real zenity: {[self.real_zenity, *argv]}",
                include_context=True,
                location="real-zenity",
            )
            return subprocess.call([self.real_zenity, *argv])
        except Exception:
            return None

    def _show_error_dialog(self, message: str) -> None:
        try:
            dialogs.error_dialog(message, title="Zenity Shim Error")
            return
        except Exception:
            self.logger.log(
                "Dialog rendering failed; trying zenity fallback.",
                level="WARNING",
                include_context=True,
                location="error-dialog",
            )

        if self.real_zenity:
            try:
                self.logger.log(
                    "Attempting to display error via real zenity.",
                    include_context=True,
                    location="error-dialog",
                )
                subprocess.call(
                    [self.real_zenity, "--error", f"--text={message}"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            except Exception:
                self.logger.log(
                    "Failed to display error via real zenity; falling back to stderr.",
                    level="WARNING",
                    include_context=True,
                    location="error-dialog",
                )

        sys.stderr.write(message + "\n")


    def _extract_option(self, argv: Sequence[str], prefix: str) -> Optional[str]:
        base = prefix[:-1] if prefix.endswith("=") else prefix

        for idx, arg in enumerate(argv):
            if arg == base and idx + 1 < len(argv):
                return argv[idx + 1]
            if arg.startswith(prefix):
                return arg.split("=", 1)[-1]
        return None

    def _handle_question(self, argv: Sequence[str]) -> int:
        title = self._extract_option(argv, "--title=") or "Question"
        text = self._extract_option(argv, "--text=") or ""
        ok_label = self._extract_option(argv, "--ok-label=") or "OK"
        cancel_label = self._extract_option(argv, "--cancel-label=") or "Cancel"
        extra_label = self._extract_option(argv, "--extra-button=")

        choice = dialogs.question_dialog(
            title=title, text=text, ok_label=ok_label, cancel_label=cancel_label, extra_label=extra_label
        )
        self.logger.log(
            f"Question dialog selection: {choice or 'cancelled'} (title={title!r})",
            include_context=True,
            location="question",
        )
        if choice == "ok":
            return 0
        if choice == "extra" and extra_label:
            sys.stdout.write(extra_label + "\n")
            return 5
        return 1

    def _extract_file_filters(self, argv: Sequence[str]) -> list[str]:
        filters: list[str] = []
        for idx, arg in enumerate(argv):
            if arg == "--file-filter" and idx + 1 < len(argv):
                filters.append(argv[idx + 1])
            elif arg.startswith("--file-filter="):
                filters.append(arg.split("=", 1)[1])
        return filters

    def _handle_file_selection(self, argv: Sequence[str]) -> int:
        title = self._extract_option(argv, "--title=") or "Select file"
        text = self._extract_option(argv, "--text=")
        filename = self._extract_option(argv, "--filename=")
        is_save = "--save" in argv
        is_multiple = "--multiple" in argv
        if is_save or is_multiple:
            return self._fallback(argv, "The zenity shim does not handle save or multi-select.")

        start_dir = Path.cwd()
        if filename:
            candidate = Path(filename).expanduser()
            if candidate.is_dir():
                start_dir = candidate
            else:
                start_dir = candidate.parent

        filters = self._extract_file_filters(argv)
        file_filter = ";;".join(filters) if filters else None
        selection = _select_rom_aware_file_dialog(
            title=title,
            text=text,
            file_filter=file_filter,
            start_dir=start_dir,
            dialog_key="shim",
            logger=self.logger,
        )
        if selection:
            sys.stdout.write(str(selection) + "\n")
            self.logger.log(
                f"zenity file selection: {selection}", include_context=True, location="zenity-open"
            )
            return 0
        self.logger.log(
            "zenity file selection cancelled", include_context=True, location="zenity-open"
        )
        return 1

    def _handle_message(self, argv: Sequence[str], *, level: str) -> int:
        title = self._extract_option(argv, "--title=") or ("Error" if level == "error" else "Information")
        text = self._extract_option(argv, "--text=") or ""
        if level == "error":
            dialogs.error_dialog(text, title=title)
        else:
            dialogs.info_dialog(text, title=title)
        self.logger.log(
            f"Displayed {level} message dialog with title={title!r}",
            include_context=True,
            location=f"message-{level}",
        )
        return 0

    def _parse_checklist_items(self, argv: Sequence[str]) -> Optional[List[Tuple[bool, str]]]:
        columns = [arg.split("=", 1)[1] for arg in argv if arg.startswith("--column=")]
        if len(columns) < 2:
            return None
        values: List[str] = [arg for arg in argv if not arg.startswith("--")]
        if len(values) % len(columns) != 0:
            return None
        rows: List[Tuple[bool, str]] = []
        step = len(columns)
        for idx in range(0, len(values), step):
            chunk = values[idx : idx + step]
            if len(chunk) < 2:
                return None
            checked = chunk[0].strip().upper() == "TRUE"
            label = chunk[1]
            rows.append((checked, label))
        return rows

    def _handle_checklist(self, argv: Sequence[str]) -> int:
        items = self._parse_checklist_items(argv)
        if items is None:
            return self._fallback(argv, "The checklist arguments could not be parsed.")

        maybe_height = self._extract_option(argv, "--height=")
        try:
            height = int(maybe_height) if maybe_height is not None else None
        except ValueError:
            height = None

        ok_label = self._extract_option(argv, "--ok-label=") or "OK"
        cancel_label = self._extract_option(argv, "--cancel-label=") or "Cancel"
        selected = dialogs.checklist_dialog(
            self._extract_option(argv, "--title=") or "Select items",
            self._extract_option(argv, "--text="),
            items,
            ok_label=ok_label,
            cancel_label=cancel_label,
            height=height,
        )
        if selected is None:
            self.logger.log(
                "Checklist dialog cancelled by user.", include_context=True, location="checklist"
            )
            return 1
        sys.stdout.write("|".join(selected) + "\n")
        self.logger.log(
            f"Checklist selections: {selected}", include_context=True, location="checklist"
        )
        return 0

    def _handle_progress(self, argv: Sequence[str]) -> int:
        result = dialogs.progress_dialog_from_stream(
            self._extract_option(argv, "--title=") or "Progress",
            self._extract_option(argv, "--text=") or "",
            sys.stdin,
        )
        if result != 0:
            self.logger.log(
                "Progress dialog cancelled by user before completion.",
                include_context=True,
                location="progress",
            )
            return result
        self.logger.log("Progress dialog completed", include_context=True, location="progress")
        return 0


class KDialogShim:
    """Lightweight kdialog-compatible shim that proxies to Kivy dialogs."""

    def __init__(self, real_kdialog: Optional[str] = None) -> None:
        self.real_kdialog = real_kdialog or self._discover_real_kdialog()
        self.logger = _logger()

    def _discover_real_kdialog(self) -> Optional[str]:
        shim_dir = os.environ.get("AP_BIZHELPER_SHIM_DIR", "")
        search_path = os.environ.get("PATH", "")
        if shim_dir:
            cleaned = os.pathsep.join(
                [p for p in search_path.split(os.pathsep) if p and Path(p) != Path(shim_dir)]
            )
        else:
            cleaned = search_path
        return shutil.which(DIALOG_SHIM_KDIALOG_FILENAME, path=cleaned) or None

    def _extract_value(self, argv: Sequence[str], flag: str) -> Optional[str]:
        if flag in argv:
            idx = argv.index(flag)
            if idx + 1 < len(argv):
                return argv[idx + 1]
        for arg in argv:
            if arg.startswith(flag + "="):
                return arg.split("=", 1)[1]
        return None

    def _auto_answer_emuhawk(self, argv: Sequence[str]) -> Optional[int]:
        title = self._extract_value(argv, "--title")
        text_candidates = [
            self._extract_value(argv, flag)
            for flag in (
                "--yesno",
                "--warningyesno",
                "--msgbox",
                "--error",
                "--sorry",
                "--inputbox",
                "--password",
            )
        ]
        text = next((value for value in text_candidates if value), None)

        title_matches = bool(title and "emuhawk" in title.casefold())
        text_has_hint = bool(text and any(hint in text.casefold() for hint in ("emuhawk", "bizhawk")))

        if not title_matches and not text_has_hint:
            return None

        runner = _locate_bizhawk_runner(self.logger)
        if runner is None:
            self.logger.log(
                "EmuHawk auto-answer detected but no runner could be located.",
                level="WARNING",
                include_context=True,
            )
            return None

        sys.stdout.write(str(runner) + "\n")
        self.logger.log(
            f"EmuHawk auto-answer provided runner: {runner}",
            include_context=True,
            location="auto-answer",
        )
        return 0

    def handle(self, argv: Sequence[str]) -> int:
        with self.logger.context("kdialog-handle"):
            self.logger.log(
                f"Handling kdialog shim request: {list(argv)}",
                include_context=True,
                location="kdialog",
            )
            if not argv:
                return self._fallback(argv, "No kdialog arguments were provided.")

            auto_answer = self._auto_answer_emuhawk(argv)
            if auto_answer is not None:
                return auto_answer

            if "--yesno" in argv or "--warningyesno" in argv:
                return self._handle_yesno(argv)
            if "--msgbox" in argv or "--sorry" in argv or "--error" in argv:
                return self._handle_message(argv)
            if "--getopenfilename" in argv:
                return self._handle_getopenfilename(argv)

            return self._fallback(argv, "The requested kdialog mode is not supported by the shim.")

    def _handle_yesno(self, argv: Sequence[str]) -> int:
        text = self._extract_value(argv, "--yesno") or self._extract_value(
            argv, "--warningyesno"
        )
        title = self._extract_value(argv, "--title") or "Question"
        choice = dialogs.question_dialog(
            title=title, text=text or "", ok_label="Yes", cancel_label="No"
        )
        self.logger.log(
            f"kdialog yes/no selection: {choice or 'cancelled'} (title={title!r})",
            include_context=True,
            location="kdialog-yesno",
        )
        return 0 if choice == "ok" else 1

    def _handle_message(self, argv: Sequence[str]) -> int:
        title = self._extract_value(argv, "--title") or "Message"
        message = (
            self._extract_value(argv, "--msgbox")
            or self._extract_value(argv, "--error")
            or self._extract_value(argv, "--sorry")
            or ""
        )
        if "--error" in argv:
            dialogs.error_dialog(message, title=title)
        else:
            dialogs.info_dialog(message, title=title)
        self.logger.log(
            f"kdialog message displayed with title={title!r}",
            include_context=True,
            location="kdialog-message",
        )
        return 0

    def _handle_getopenfilename(self, argv: Sequence[str]) -> int:
        try:
            start_index = argv.index("--getopenfilename") + 1
            start_dir = Path(argv[start_index]) if start_index < len(argv) else Path.cwd()
        except ValueError:
            start_dir = Path.cwd()
        file_filter = None
        try:
            filter_index = argv.index("--getopenfilename") + 2
            if filter_index < len(argv):
                file_filter = argv[filter_index]
        except ValueError:
            pass

        selection = _select_rom_aware_file_dialog(
            title=self._extract_value(argv, "--title") or "Select file",
            text=None,
            file_filter=file_filter,
            start_dir=start_dir,
            dialog_key="shim",
            logger=self.logger,
        )
        if selection:
            sys.stdout.write(str(selection) + "\n")
            self.logger.log(
                f"kdialog file selection: {selection}", include_context=True, location="kdialog-open"
            )
            return 0
        self.logger.log(
            "kdialog file selection cancelled", include_context=True, location="kdialog-open"
        )
        return 1

    def _fallback(self, argv: Sequence[str], reason: str) -> int:
        if self.real_kdialog:
            try:
                return subprocess.call([self.real_kdialog, *argv])
            except Exception:
                pass
        sys.stderr.write(reason + "\n")
        return 127


class PortalShim:
    """Minimal xdg-desktop-portal shim focused on FileChooser."""

    def __init__(self, real_portal: Optional[str] = None) -> None:
        self.real_portal = real_portal
        self.logger = _logger()

    def handle(self, argv: Sequence[str]) -> int:
        with self.logger.context("portal-handle"):
            self.logger.log(
                f"Handling portal shim request: {list(argv)}",
                include_context=True,
                location="portal",
            )
            if argv and argv[0] in {"--help", "-h"}:
                sys.stdout.write("ap-bizhelper portal shim (FileChooser only)\n")
                return 0

            if argv and argv[0] in {"--choose-file", "--choose-multiple"}:
                return self._handle_choose_file(argv)

            return self._fallback(argv)

    def _handle_choose_file(self, argv: Sequence[str]) -> int:
        start_dir = Path(argv[1]) if len(argv) > 1 else Path.cwd()
        selection = dialogs.select_file_dialog(
            title="Select file",
            dialog_key="shim",
            initial=start_dir,
            file_filter=None,
        )
        if selection:
            sys.stdout.write(str(selection) + "\n")
            self.logger.log(
                f"portal file selection: {selection}", include_context=True, location="portal"
            )
            return 0
        self.logger.log("portal file selection cancelled", include_context=True, location="portal")
        return 1

    def _fallback(self, argv: Sequence[str]) -> int:
        if self.real_portal:
            try:
                self.logger.log(
                    f"Delegating portal shim to real portal: {self.real_portal}",
                    include_context=True,
                    location="portal-fallback",
                )
                return subprocess.call([self.real_portal, *argv])
            except Exception:
                pass
        sys.stderr.write(
            "xdg-desktop-portal shim could not handle the request and no real portal was found.\n"
        )
        return 127




def shim_main() -> None:
    logger = _logger()
    with logger.context("shim-main"):
        logger.log(
            f"zenity shim entrypoint argv={sys.argv[1:]}", include_context=True, location="entry"
        )
        shim = ZenityShim(real_zenity=os.environ.get(_REAL_ZENITY_ENV) or None)
        sys.exit(shim.handle(sys.argv[1:]))


def kdialog_main() -> None:
    logger = _logger()
    with logger.context("kdialog-main"):
        logger.log(
            f"kdialog shim entrypoint argv={sys.argv[1:]}", include_context=True, location="entry"
        )
        shim = KDialogShim(real_kdialog=os.environ.get(_REAL_KDIALOG_ENV) or None)
        sys.exit(shim.handle(sys.argv[1:]))


def portal_file_chooser_main() -> None:
    logger = _logger()
    with logger.context("portal-main"):
        logger.log(
            f"portal shim entrypoint argv={sys.argv[1:]}", include_context=True, location="entry"
        )
        shim = PortalShim(real_portal=os.environ.get(_REAL_PORTAL_ENV) or None)
        sys.exit(shim.handle(sys.argv[1:]))


if __name__ == "__main__":
    shim_main()
