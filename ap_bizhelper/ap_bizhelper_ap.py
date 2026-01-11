#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from . import dialogs
from .ap_bizhelper_config import (
    CONFIG_DIR,
    SETTINGS_FILE,
    get_path_setting,
    load_settings as _load_shared_settings,
    save_settings as _save_shared_settings,
)
from .constants import (
    AP_APPIMAGE_KEY,
    AP_DESKTOP_SHORTCUT_KEY,
    AP_LATEST_SEEN_VERSION_KEY,
    AP_SKIP_VERSION_KEY,
    AP_VERSION_KEY,
    AP_APPIMAGE_DEFAULT,
    DATA_DIR,
    DESKTOP_DIR_KEY,
    USER_AGENT,
    USER_AGENT_HEADER,
)
from .logging_utils import get_app_logger

APP_LOGGER = get_app_logger()

GITHUB_API_LATEST = "https://api.github.com/repos/ArchipelagoMW/Archipelago/releases/latest"


def _ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_settings() -> Dict[str, Any]:
    settings = _load_shared_settings()
    merged_settings = dialogs.merge_dialog_settings(settings)

    needs_save = not SETTINGS_FILE.exists()
    if not needs_save:
        for key in dialogs.DIALOG_DEFAULTS:
            if key not in settings:
                needs_save = True
                break

    if needs_save:
        _save_settings(merged_settings)

    return merged_settings


def _save_settings(settings: Dict[str, Any]) -> None:
    _ensure_dirs()
    merged_settings = dialogs.merge_dialog_settings(settings)
    _save_shared_settings(merged_settings)


def _ensure_qt_available() -> None:
    try:
        dialogs.ensure_qt_available()
    except Exception as exc:
        APP_LOGGER.log(
            f"PySide6 is required but could not be imported: {exc}",
            level="ERROR",
            include_context=True,
            mirror_console=True,
            stream="stderr",
            location="qt-deps",
        )
        raise


def _stage_appimage(settings: Dict[str, Any], app_path: Path) -> Path:
    if app_path == AP_APPIMAGE_DEFAULT:
        return app_path

    try:
        if AP_APPIMAGE_DEFAULT.exists() and AP_APPIMAGE_DEFAULT.samefile(app_path):
            return AP_APPIMAGE_DEFAULT
    except Exception:
        pass

    try:
        if AP_APPIMAGE_DEFAULT.resolve() == app_path.resolve():
            return AP_APPIMAGE_DEFAULT
    except Exception:
        pass

    try:
        AP_APPIMAGE_DEFAULT.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(app_path, AP_APPIMAGE_DEFAULT)
        AP_APPIMAGE_DEFAULT.chmod(AP_APPIMAGE_DEFAULT.stat().st_mode | 0o111)
    except Exception as exc:
        APP_LOGGER.log(
            f"Failed to stage Archipelago AppImage to {AP_APPIMAGE_DEFAULT}: {exc}",
            include_context=True,
            level="WARNING",
            mirror_console=True,
        )
        return app_path

    settings[AP_APPIMAGE_KEY] = str(AP_APPIMAGE_DEFAULT)
    _save_settings(settings)
    APP_LOGGER.log(
        f"Staged Archipelago AppImage to {AP_APPIMAGE_DEFAULT}",
        include_context=True,
        mirror_console=True,
    )
    return AP_APPIMAGE_DEFAULT


def _version_sort_key(version: str) -> tuple[tuple[int, object], ...]:
    cleaned = version.strip()
    if cleaned.lower().startswith("v"):
        cleaned = cleaned[1:]
    tokens = re.findall(r"\d+|[a-zA-Z]+", cleaned)
    if not tokens:
        return ((1, cleaned.lower()),)
    key: list[tuple[int, object]] = []
    for token in tokens:
        if token.isdigit():
            key.append((0, int(token)))
        else:
            key.append((1, token.lower()))
    return tuple(key)


def _is_newer_version(latest: str, seen: str) -> bool:
    latest = latest.strip()
    seen = seen.strip()
    if not latest:
        return False
    if not seen:
        return True
    if latest == seen:
        return False
    try:
        return _version_sort_key(latest) > _version_sort_key(seen)
    except Exception:
        return latest > seen


def _select_file_dialog(
    *,
    title: str,
    initial: Optional[Path] = None,
    file_filter: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
    dialog_key: str = "default",
) -> Optional[Path]:
    _ensure_qt_available()
    settings_obj = settings if settings is not None else _load_settings()
    start_dir = dialogs.preferred_start_dir(initial, settings_obj, dialog_key)

    try:
        selection = dialogs.file_dialog(
            title=title, start_dir=start_dir, file_filter=file_filter, settings=settings_obj
        )
    except Exception as exc:  # pragma: no cover - GUI/runtime issues
        APP_LOGGER.log(
            f"PySide6 file selection failed: {exc}",
            level="ERROR",
            include_context=True,
            mirror_console=True,
            stream="stderr",
            location="qt-file-dialog",
        )
        raise

    if selection:
        dialogs.remember_file_dialog_dir(settings_obj, selection, dialog_key)
        if settings is None:
            _save_settings(settings_obj)

    return selection


def info_dialog(message: str) -> None:
    dialogs.info_dialog(message, logger=APP_LOGGER)


def error_dialog(message: str) -> None:
    dialogs.error_dialog(message, logger=APP_LOGGER)


def choose_install_action(title: str, text: str, select_label: str = "Select") -> str:
    """
    Show a dialog offering Download / Select / Cancel.

    Returns "Download", "Select", or "Cancel". ``select_label`` customizes the
    text shown for the "Select" button.
    """
    choice = dialogs.question_dialog(
        title=title,
        text=text,
        ok_label="Download",
        cancel_label="Cancel",
        extra_label=select_label,
    )
    if choice == "extra":
        return "Select"
    if choice == "ok":
        return "Download"
    return "Cancel"


def select_appimage(
    initial: Optional[Path] = None, *, settings: Optional[Dict[str, Any]] = None
) -> Optional[Path]:
    selection = _select_file_dialog(
        title="Select Archipelago AppImage",
        initial=initial,
        settings=settings,
        dialog_key="appimage",
    )
    if selection is None:
        return None
    p = selection
    if not p.is_file():
        error_dialog("Selected file does not exist.")
        return None
    try:
        p.chmod(p.stat().st_mode | 0o111)
    except Exception:
        pass
    return p


def manual_select_appimage(settings: Optional[Dict[str, Any]] = None) -> Optional[Path]:
    """Prompt for an existing AppImage and persist the selection."""

    provided_settings = settings
    settings = settings if settings is not None else _load_settings()

    selection = select_appimage(Path(os.path.expanduser("~")), settings=settings)
    if selection is None:
        return None

    settings[AP_APPIMAGE_KEY] = str(selection)
    settings[AP_VERSION_KEY] = ""
    settings[AP_SKIP_VERSION_KEY] = ""
    _save_settings(settings)

    if provided_settings is not None and settings is not provided_settings:
        merged = {**provided_settings, **settings}
        provided_settings.clear()
        provided_settings.update(merged)

    return selection


def force_update_appimage(settings: Optional[Dict[str, Any]] = None) -> bool:
    """Force a download of the latest AppImage into the managed location."""

    provided_settings = settings
    settings = settings if settings is not None else _load_settings()

    try:
        url, latest_ver, latest_digest, latest_algo = _github_latest_appimage()
    except Exception as exc:
        error_dialog(f"Failed to query latest Archipelago release: {exc}")
        return False

    try:
        download_appimage(
            url,
            AP_APPIMAGE_DEFAULT,
            latest_ver,
            expected_digest=latest_digest,
            digest_algorithm=latest_algo,
        )
    except Exception as exc:
        error_dialog(f"Archipelago download failed or was cancelled: {exc}")
        return False

    settings[AP_APPIMAGE_KEY] = str(AP_APPIMAGE_DEFAULT)
    settings[AP_VERSION_KEY] = latest_ver
    settings[AP_SKIP_VERSION_KEY] = ""
    settings[AP_LATEST_SEEN_VERSION_KEY] = latest_ver
    _save_settings(settings)

    if provided_settings is not None and settings is not provided_settings:
        merged = {**provided_settings, **settings}
        provided_settings.clear()
        provided_settings.update(merged)

    info_dialog(f"Archipelago updated to {latest_ver}.")
    return True


def _prompt_select_existing_appimage(initial: Path, *, settings: Dict[str, Any]) -> Path:
    """Prompt the user to select an existing AppImage without offering download."""

    choice = dialogs.question_dialog(
        title="Archipelago setup",
        text="Archipelago was not selected for download.\n\nSelect an existing AppImage to continue?",
        ok_label="Select AppImage",
        cancel_label="Cancel",
    )
    if choice != "ok":
        raise RuntimeError("User cancelled Archipelago AppImage selection")

    chosen = select_appimage(initial, settings=settings)
    if not chosen:
        raise RuntimeError("User cancelled Archipelago AppImage selection")

    return chosen


def _normalize_asset_digest(raw_digest: str, *, default_algorithm: str = "sha256") -> Tuple[str, str]:
    digest = raw_digest.strip()
    if not digest:
        raise ValueError("empty digest")

    algorithm, value = default_algorithm, digest
    if ":" in digest:
        parts = digest.split(":", 1)
        if len(parts) == 2 and parts[0].strip():
            algorithm, value = parts[0].strip(), parts[1].strip()
    if not value:
        raise ValueError("empty digest value")
    return algorithm.lower(), value.lower()


def _github_latest_appimage() -> Tuple[str, str, str, str]:
    """
    Return (download_url, version_tag, digest, digest_algorithm) for the latest Archipelago Linux AppImage.

    Raises RuntimeError on failure.
    """
    import urllib.request
    import json as _json

    req = urllib.request.Request(GITHUB_API_LATEST, headers={USER_AGENT_HEADER: USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read().decode("utf-8")
    j = _json.loads(data)

    tag = j.get("tag_name") or ""
    assets = j.get("assets") or []
    pattern = re.compile(r"Archipelago_.*_linux-x86_64\.AppImage$")
    for asset in assets:
        name = asset.get("name") or ""
        if pattern.search(name):
            url = asset.get("browser_download_url")
            digest = asset.get("digest")
            if not url:
                continue
            if not digest:
                raise RuntimeError(f"AppImage asset missing digest: {name}")
            try:
                digest_algorithm, normalized_digest = _normalize_asset_digest(digest)
            except ValueError as exc:
                raise RuntimeError(f"Invalid digest for asset {name}: {exc}") from exc
            return url, tag, normalized_digest, digest_algorithm
    raise RuntimeError("Could not find Archipelago Linux AppImage asset in latest release.")


def download_with_progress(
    url: str,
    dest: Path,
    *,
    title: str,
    text: str,
    expected_hash: Optional[str] = None,
    hash_name: str = "sha256",
    require_hash: bool = False,
) -> None:
    """Download ``url`` to ``dest`` with a PySide6 progress dialog.

    When ``expected_hash`` is provided, the downloaded file is validated using
    the ``hash_name`` algorithm (``sha256`` by default). If the server provides
    a ``X-Checksum-Sha256`` header, that value is used as the expected hash
    when one is not explicitly supplied. When ``require_hash`` is True, missing
    digests will abort the download.
    """

    _ensure_dirs()
    _ensure_qt_available()

    req = urllib.request.Request(url, headers={USER_AGENT_HEADER: USER_AGENT})

    response_headers: dict[str, str] = {}
    temp_path: Optional[Path] = None

    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent, delete=False
        ) as tmp:
            temp_path = Path(tmp.name)
    except Exception as exc:
        raise RuntimeError(f"Failed to create temporary download file: {exc}") from exc

    def _cleanup_temp() -> None:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass

    def _download_stream() -> Iterable[str]:
        nonlocal response_headers
        try:
            with urllib.request.urlopen(req, timeout=300) as resp, temp_path.open("wb") as f:
                response_headers = {k.lower(): v for k, v in resp.headers.items()}

                normalized_hash_name = hash_name
                normalized_expected = expected_hash
                if normalized_expected:
                    try:
                        normalized_hash_name, normalized_expected = _normalize_asset_digest(
                            normalized_expected, default_algorithm=hash_name
                        )
                    except ValueError as exc:
                        raise RuntimeError(f"Invalid expected digest: {exc}") from exc

                header_hash = response_headers.get("x-checksum-sha256") or ""
                if not normalized_expected and header_hash:
                    try:
                        normalized_hash_name, normalized_expected = _normalize_asset_digest(
                            header_hash, default_algorithm="sha256"
                        )
                    except ValueError:
                        normalized_expected = ""

                try:
                    hash_ctx = hashlib.new(normalized_hash_name)
                except Exception as exc:
                    raise RuntimeError(
                        f"Unsupported hash algorithm: {normalized_hash_name}"
                    ) from exc

                total_str = resp.headers.get("Content-Length") or "0"
                try:
                    total = int(total_str)
                except ValueError:
                    total = 0
                downloaded = 0
                chunk_size = 65536
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    hash_ctx.update(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        percent = max(0, min(100, int(downloaded * 100 / total)))
                        yield str(percent)

                computed_hash = hash_ctx.hexdigest().lower()
                if normalized_expected:
                    if normalized_expected.lower() != computed_hash:
                        raise RuntimeError("Downloaded file failed hash verification")
                elif require_hash:
                    raise RuntimeError("Download did not provide an expected digest")
        except GeneratorExit:
            _cleanup_temp()
            raise
        except Exception:
            _cleanup_temp()
            raise

    result = dialogs.progress_dialog_from_stream(
        title=title,
        text=text,
        stream=_download_stream(),
        cancel_label="Cancel",
    )

    if result != 0:
        _cleanup_temp()
        raise RuntimeError("Download cancelled by user")

    try:
        temp_path.replace(dest)
    except Exception:
        _cleanup_temp()
        raise

    try:
        dest.chmod(dest.stat().st_mode | 0o111)
    except Exception:
        pass

def download_appimage(
    url: str,
    dest: Path,
    version: str,
    *,
    expected_digest: str,
    digest_algorithm: str,
    download_messages: Optional[list[str]] = None,
) -> None:
    """Download the AppImage to ``dest`` with a Qt progress dialog."""

    download_with_progress(
        url,
        dest,
        title="Archipelago download",
        text=f"Downloading Archipelago {version}...",
        expected_hash=expected_digest,
        hash_name=digest_algorithm,
        require_hash=True,
    )
    if download_messages is not None:
        download_messages.append(f"Downloaded Archipelago {version}")


def _desktop_shortcut_path(settings: Dict[str, Any], name: str) -> Path:
    desktop_dir = get_path_setting(settings, DESKTOP_DIR_KEY)
    return desktop_dir / f"{name}.desktop"


def _write_desktop_shortcut(path: Path, name: str, exec_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={name}\n"
        f"Exec={exec_path}\n"
        "Terminal=false\n"
    )
    with path.open("w", encoding="utf-8") as f:
        f.write(content)
    path.chmod(0o755)


def _create_desktop_shortcut(
    settings: Dict[str, Any],
    name: str,
    exec_path: Path,
    settings_key: str,
    *,
    enabled: bool,
) -> None:
    shortcut_path = _desktop_shortcut_path(settings, name)

    if not enabled:
        settings[settings_key] = "no"
        _save_settings(settings)
        return

    try:
        _write_desktop_shortcut(shortcut_path, name, exec_path)
        settings[settings_key] = "yes"
        _save_settings(settings)
    except Exception as exc:  # pragma: no cover - filesystem edge cases
        settings[settings_key] = "no"
        _save_settings(settings)
        error_dialog(f"Failed to create Desktop shortcut: {exc}")


def maybe_update_appimage(
    settings: Dict[str, Any], appimage: Path, *, download_messages: Optional[list[str]] = None
) -> Tuple[Path, bool]:
    """
    If we manage this AppImage (default path), check GitHub for a newer version.

    Respects AP_SKIP_VERSION. If an update is installed, updates AP_VERSION and
    returns the (possibly new) appimage path along with a flag indicating whether
    a download occurred.
    """
    # Only auto-update if using the default managed AppImage
    if appimage != AP_APPIMAGE_DEFAULT:
        return appimage, False

    try:
        url, latest_ver, latest_digest, latest_algo = _github_latest_appimage()
    except Exception:
        return appimage, False

    current_ver = str(settings.get(AP_VERSION_KEY, "") or "")
    skip_ver = str(settings.get(AP_SKIP_VERSION_KEY, "") or "")
    latest_seen = str(settings.get(AP_LATEST_SEEN_VERSION_KEY, "") or "")
    should_prompt = _is_newer_version(latest_ver, latest_seen)
    if latest_ver and latest_ver != latest_seen:
        settings[AP_LATEST_SEEN_VERSION_KEY] = latest_ver
        _save_settings(settings)

    if not current_ver:
        return appimage, False

    if current_ver == latest_ver or skip_ver == latest_ver:
        return appimage, False
    if not should_prompt:
        return appimage, False

    choice = dialogs.question_dialog(
        title="Archipelago update",
        text="An Archipelago update is available. Update now?",
        ok_label="Update now",
        cancel_label="Later",
        extra_label="Skip this version",
    )
    if choice == "cancel":
        return appimage, False
    if choice == "extra":
        settings[AP_SKIP_VERSION_KEY] = latest_ver
        settings[AP_LATEST_SEEN_VERSION_KEY] = latest_ver
        _save_settings(settings)
        return appimage, False

    # Update now
    try:
        download_appimage(
            url,
            AP_APPIMAGE_DEFAULT,
            latest_ver,
            expected_digest=latest_digest,
            digest_algorithm=latest_algo,
            download_messages=download_messages,
        )
    except Exception as e:
        error_dialog(f"Archipelago update failed: {e}")
        return appimage, False

    settings[AP_APPIMAGE_KEY] = str(AP_APPIMAGE_DEFAULT)
    settings[AP_VERSION_KEY] = latest_ver
    settings[AP_SKIP_VERSION_KEY] = ""
    settings[AP_LATEST_SEEN_VERSION_KEY] = latest_ver
    _save_settings(settings)
    if download_messages is not None:
        download_messages.append(f"Updated Archipelago to {latest_ver}")
    else:
        info_dialog(f"Archipelago updated to {latest_ver}.")
    return AP_APPIMAGE_DEFAULT, True


def ensure_appimage(
    *,
    download_selected: bool = True,
    create_shortcut: bool = False,
    download_messages: Optional[list[str]] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Path:
    """
    Ensure the Archipelago AppImage is configured and up to date.

    On success, returns the Path to the AppImage and persists any changes
    into the JSON settings file. On failure, raises RuntimeError.
    """
    _ensure_dirs()
    provided_settings = settings
    settings = settings if settings is not None else _load_settings()

    def _merge_and_save_settings() -> None:
        nonlocal settings

        if provided_settings is not None and settings is not provided_settings:
            merged = {**provided_settings, **settings}
            provided_settings.clear()
            provided_settings.update(merged)
            settings = provided_settings

        _save_settings(settings)

    downloaded = False

    # 1. Try stored path
    app_path_str = str(settings.get(AP_APPIMAGE_KEY, "") or "")
    app_path = Path(app_path_str) if app_path_str else None

    if app_path and app_path.is_file():
        # Make sure it's executable
        try:
            app_path.chmod(app_path.stat().st_mode | 0o111)
        except Exception:
            pass
    else:
        # 2. Try the default managed AppImage
        if AP_APPIMAGE_DEFAULT.is_file():
            app_path = AP_APPIMAGE_DEFAULT
        else:
            app_path = None

    needs_setup = app_path is None or not app_path.is_file() or not os.access(str(app_path), os.X_OK)

    # 3. If still missing, either download automatically (when selected) or prompt only for selection
    if needs_setup:
        if download_selected:
            try:
                url, ver, digest, digest_algo = _github_latest_appimage()
            except Exception as e:
                error_dialog(f"Failed to query latest Archipelago release: {e}")
                raise RuntimeError("Failed to query latest Archipelago release") from e
            settings[AP_LATEST_SEEN_VERSION_KEY] = ver
            try:
                download_appimage(
                    url,
                    AP_APPIMAGE_DEFAULT,
                    ver,
                    expected_digest=digest,
                    digest_algorithm=digest_algo,
                    download_messages=download_messages,
                )
            except Exception as e:
                error_dialog(f"Archipelago download failed or was cancelled: {e}")
                raise RuntimeError("Archipelago download failed") from e
            app_path = AP_APPIMAGE_DEFAULT
            settings[AP_APPIMAGE_KEY] = str(AP_APPIMAGE_DEFAULT)
            settings[AP_VERSION_KEY] = ver
            settings[AP_SKIP_VERSION_KEY] = ""
            settings[AP_LATEST_SEEN_VERSION_KEY] = ver
            _merge_and_save_settings()
            downloaded = True
        else:
            app_path = _prompt_select_existing_appimage(
                Path(os.path.expanduser("~")), settings=settings
            )
            settings[AP_APPIMAGE_KEY] = str(app_path)
            # No version information when manually selected.
            _merge_and_save_settings()

    if app_path is None or not app_path.is_file() or not os.access(str(app_path), os.X_OK):
        error_dialog("Archipelago AppImage was not configured correctly.")
        raise RuntimeError("Archipelago AppImage not configured")

    # 4. Auto-update if applicable
    app_path, updated = maybe_update_appimage(
        settings, app_path, download_messages=download_messages
    )
    downloaded = downloaded or updated

    # 5. Stage a local copy in the managed data directory.
    app_path = _stage_appimage(settings, app_path)

    # 6. Create a desktop shortcut only when a download occurred
    if downloaded:
        _create_desktop_shortcut(
            settings,
            "Archipelago",
            app_path,
            AP_DESKTOP_SHORTCUT_KEY,
            enabled=create_shortcut,
        )

    return app_path
