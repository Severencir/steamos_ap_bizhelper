#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from . import dialogs
from .ap_bizhelper_config import (
    CONFIG_DIR,
    SETTINGS_FILE,
    load_settings as _load_shared_settings,
    save_settings as _save_shared_settings,
)
from .constants import DATA_DIR, DESKTOP_DIR, DOWNLOADS_DIR, USER_AGENT, USER_AGENT_HEADER
from .logging_utils import get_app_logger

# Paths mirror the bash script and the config helper.
AP_APPIMAGE_DEFAULT = DATA_DIR / "Archipelago.AppImage"

APP_LOGGER = get_app_logger()

GITHUB_API_LATEST = "https://api.github.com/repos/ArchipelagoMW/Archipelago/releases/latest"

_QT_APP: Optional["QtWidgets.QApplication"] = None
_QT_BASE_FONT: Optional["QtGui.QFont"] = None
_QT_FONT_SCALE = 1
_QT_MIN_POINT_SIZE = 12
_QT_FILE_NAME_FONT_SCALE = 1.4
_QT_FILE_DIALOG_WIDTH = 1280
_QT_FILE_DIALOG_HEIGHT = 800
_QT_FILE_DIALOG_MAXIMIZE = True
_QT_FILE_DIALOG_NAME_WIDTH = 850
_QT_FILE_DIALOG_TYPE_WIDTH = 200
_QT_FILE_DIALOG_SIZE_WIDTH = 200
_QT_FILE_DIALOG_DATE_WIDTH = 0
_QT_FILE_DIALOG_SIDEBAR_WIDTH = 200
_QT_FILE_DIALOG_COLUMN_SCALE = 1.8
_QT_FILE_DIALOG_DEFAULT_SHRINK = 0.95
_QT_IMPORT_ERROR: Optional[BaseException] = None
_DEFAULT_SETTINGS = dialogs.DIALOG_DEFAULTS


def _ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_settings() -> Dict[str, Any]:
    settings = _load_shared_settings()
    merged_settings = {**_DEFAULT_SETTINGS, **settings}

    needs_save = not SETTINGS_FILE.exists()
    if not needs_save:
        for key in _DEFAULT_SETTINGS:
            if key not in settings:
                needs_save = True
                break

    if needs_save:
        _save_settings(merged_settings)

    return merged_settings


def _save_settings(settings: Dict[str, Any]) -> None:
    _ensure_dirs()
    merged_settings = {**_DEFAULT_SETTINGS, **settings}
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


def _coerce_font_setting(
    settings: Dict[str, Any], key: str, default: float, *, minimum: Optional[float] = None
) -> float:
    value = settings.get(key, default)
    try:
        numeric_value = float(value)
    except Exception:
        return default
    if minimum is not None:
        numeric_value = max(numeric_value, minimum)
    return numeric_value


def _coerce_int_setting(
    settings: Dict[str, Any], key: str, default: int, *, minimum: Optional[int] = None
) -> int:
    value = settings.get(key, default)
    try:
        numeric_value = int(value)
    except Exception:
        return default
    if minimum is not None:
        numeric_value = max(numeric_value, minimum)
    return numeric_value


def _coerce_bool_setting(settings: Dict[str, Any], key: str, default: bool) -> bool:
    value = settings.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


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


def _detect_global_scale() -> float:
    _ensure_qt_available()
    from PySide6 import QtGui

    try:
        screen = QtGui.QGuiApplication.primaryScreen()
    except Exception:
        return 1.0

    if screen is None:
        return 1.0

    dpi_scale = 1.0
    try:
        logical_dpi = float(screen.logicalDotsPerInch())
        if logical_dpi > 0:
            dpi_scale = logical_dpi / 96.0
    except Exception:
        dpi_scale = 1.0

    try:
        pixel_ratio = float(screen.devicePixelRatio())
        if pixel_ratio > 0:
            dpi_scale = max(dpi_scale, pixel_ratio)
    except Exception:
        pass

    return max(dpi_scale, 0.1)


def _ensure_qt_app(settings: Optional[Dict[str, Any]] = None) -> "QtWidgets.QApplication":
    return dialogs.ensure_qt_app(settings)


def _enable_dialog_gamepad(
    dialog: "QtWidgets.QDialog | QtWidgets.QMessageBox",
    *,
    affirmative: Optional["QtWidgets.QAbstractButton"] = None,
    negative: Optional["QtWidgets.QAbstractButton"] = None,
    special: Optional["QtWidgets.QAbstractButton"] = None,
    default: Optional["QtWidgets.QAbstractButton"] = None,
) -> Optional["object"]:
    """Attach controller navigation to ``dialog`` if available."""

    try:
        from . import gamepad_input

        layer = gamepad_input.install_gamepad_navigation(
            dialog,
            actions={
                "affirmative": affirmative,
                "negative": negative,
                "special": special,
                "default": default,
            },
        )
        if layer is not None:
            dialog.finished.connect(layer.shutdown)  # type: ignore[attr-defined]
        return layer
    except Exception as exc:  # pragma: no cover - runtime guard
        APP_LOGGER.log(
            f"Failed to enable gamepad navigation: {exc}",
            level="WARNING",
            include_context=True,
            location="gamepad",
        )
    return None


def _dialog_dir_map(settings: Dict[str, Any]) -> Dict[str, str]:
    stored = settings.get("LAST_FILE_DIALOG_DIRS", {})
    if isinstance(stored, dict):
        return stored
    return {}


def _preferred_start_dir(initial: Optional[Path], settings: Dict[str, Any], dialog_key: str) -> Path:
    last_dir_setting = str(settings.get("LAST_FILE_DIALOG_DIR", "") or "")
    per_dialog_dir = str(_dialog_dir_map(settings).get(dialog_key, "") or "")

    candidates = [
        initial if initial and initial.expanduser() != Path.home() else None,
        Path(per_dialog_dir) if per_dialog_dir else None,
        Path(last_dir_setting) if last_dir_setting else None,
        DOWNLOADS_DIR if DOWNLOADS_DIR.exists() else None,
        initial if initial else None,
        Path.home(),
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        candidate_path = candidate.expanduser()
        if candidate_path.is_file():
            candidate_path = candidate_path.parent
        if candidate_path.exists():
            return candidate_path
    return Path.home()


def _remember_file_dialog_dir(settings: Dict[str, Any], selection: Path, dialog_key: str) -> None:
    parent = selection.parent if selection.is_file() else selection
    dialog_dirs = _dialog_dir_map(settings)
    dialog_dirs[dialog_key] = str(parent)
    settings["LAST_FILE_DIALOG_DIRS"] = dialog_dirs
    settings["LAST_FILE_DIALOG_DIR"] = str(parent)


def _sidebar_urls() -> list["QtCore.QUrl"]:
    from PySide6 import QtCore

    common_dirs = [
        Path.home(),
        DOWNLOADS_DIR,
        Path(os.path.expanduser("~/Documents")),
        DESKTOP_DIR,
        Path(os.path.expanduser("~/Music")),
        Path(os.path.expanduser("~/Pictures")),
        Path(os.path.expanduser("~/Videos")),
    ]
    return [
        QtCore.QUrl.fromLocalFile(str(path)) for path in common_dirs if path.expanduser().exists()
    ]


def _widen_file_dialog_sidebar(
    dialog: "QtWidgets.QFileDialog", settings_obj: Dict[str, Any]
) -> None:
    from PySide6 import QtWidgets

    sidebar = dialog.findChild(QtWidgets.QListView, "sidebar")
    width = _coerce_int_setting(
        settings_obj, "QT_FILE_DIALOG_SIDEBAR_WIDTH", _QT_FILE_DIALOG_SIDEBAR_WIDTH, minimum=0
    )
    if sidebar is None or width <= 0:
        return

    splitter: Optional[QtWidgets.QSplitter] = None
    parent = sidebar.parent()
    while parent is not None:
        if isinstance(parent, QtWidgets.QSplitter):
            splitter = parent
            break
        parent = parent.parent()
    if splitter is None:
        splitter = dialog.findChild(QtWidgets.QSplitter)

    if splitter is None:
        try:
            sidebar.setFixedWidth(width)
            sidebar.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Expanding)
        except Exception:
            pass
        return

    sidebar_index = splitter.indexOf(sidebar)
    if sidebar_index < 0:
        sidebar_index = 0

    try:
        sizes = splitter.sizes()
    except Exception:
        sizes = []
    total_width = splitter.size().width()
    total_width = max(total_width, sum(sizes))
    if total_width <= 0:
        total_width = width * max(2, splitter.count())

    remaining = max(total_width - width, width)
    other_count = max(splitter.count() - 1, 1)
    per_other = max(int(remaining / other_count), 1)

    new_sizes = [per_other for _ in range(splitter.count())]
    if sidebar_index < len(new_sizes):
        new_sizes[sidebar_index] = width

    try:
        splitter.setSizes(new_sizes)
    except Exception:
        pass

    try:
        sidebar.setFixedWidth(width)
        sidebar.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Expanding)
    except Exception:
        pass

    for idx in range(splitter.count()):
        try:
            splitter.setStretchFactor(idx, 1 if idx != sidebar_index else 0)
        except Exception:
            continue


def _configure_file_view_columns(
    dialog: "QtWidgets.QFileDialog", settings_obj: Dict[str, Any]
) -> None:
    from PySide6 import QtCore, QtWidgets

    tree_view = dialog.findChild(QtWidgets.QTreeView, "treeView")
    if tree_view is None:
        return

    model = tree_view.model()
    header = tree_view.header()
    if model is None or header is None:
        return

    column_count = model.columnCount()
    if column_count <= 0:
        return

    label_to_index: dict[str, int] = {}
    for idx in range(column_count):
        try:
            label = str(
                model.headerData(idx, QtCore.Qt.Horizontal, QtCore.Qt.DisplayRole) or ""
            ).strip()
        except Exception:
            label = ""
        if label:
            label_to_index[label.lower()] = idx

    desired_columns = [
        ("name", "QT_FILE_DIALOG_NAME_WIDTH"),
        ("size", "QT_FILE_DIALOG_SIZE_WIDTH"),
        ("type", "QT_FILE_DIALOG_TYPE_WIDTH"),
        ("date modified", "QT_FILE_DIALOG_DATE_WIDTH"),
    ]
    fallback_indices = {"name": 0, "size": 1, "type": 2, "date modified": 3}

    updated_settings = False
    for label, setting_key in desired_columns:
        index = label_to_index.get(label, fallback_indices.get(label))
        if index is None or index >= column_count:
            continue

        try:
            base_width = tree_view.sizeHintForColumn(index)
        except Exception:
            base_width = 0
        if base_width <= 0:
            try:
                base_width = header.sectionSizeHint(index)
            except Exception:
                base_width = 0
        if base_width <= 0:
            try:
                base_width = header.defaultSectionSize()
            except Exception:
                base_width = 0

        configured_width = _coerce_int_setting(
            settings_obj, setting_key, 0, minimum=0
        )
        target_width = configured_width or int(
            base_width * _QT_FILE_DIALOG_COLUMN_SCALE * _QT_FILE_DIALOG_DEFAULT_SHRINK
        )
        if target_width > 0:
            try:
                header.setSectionResizeMode(index, QtWidgets.QHeaderView.Interactive)
            except Exception:
                pass
            header.resizeSection(index, target_width)
            try:
                header.setSectionResizeMode(index, QtWidgets.QHeaderView.Interactive)
            except Exception:
                pass
            if configured_width <= 0:
                settings_obj[setting_key] = target_width
                updated_settings = True

    if updated_settings:
        _save_settings(settings_obj)


def _focus_file_view(dialog: "QtWidgets.QFileDialog") -> None:
    from PySide6 import QtCore, QtWidgets

    tree_view = dialog.findChild(QtWidgets.QTreeView, "treeView")
    if tree_view is None:
        return

    selection_model = tree_view.selectionModel()
    model = tree_view.model()
    if model is None or selection_model is None:
        return

    try:
        current_index = tree_view.currentIndex()
        if not current_index.isValid() and model.rowCount() > 0:
            first_index = model.index(0, 0)
            if first_index.isValid():
                tree_view.setCurrentIndex(first_index)
                selection_model.select(
                    first_index,
                    QtCore.QItemSelectionModel.Select
                    | QtCore.QItemSelectionModel.Rows,
                )
    except Exception:
        pass

    try:
        tree_view.setFocus(QtCore.Qt.FocusReason.ActiveWindowFocusReason)
    except Exception:
        pass


def _qt_file_dialog(
    *,
    title: str,
    start_dir: Path,
    file_filter: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Optional[Path]:
    from PySide6 import QtCore, QtGui, QtWidgets

    settings_obj = {**_DEFAULT_SETTINGS, **(settings or {})}
    _ensure_qt_app(settings_obj)
    global_scale = _detect_global_scale()
    filter_text = file_filter or "All Files (*)"
    dialog = QtWidgets.QFileDialog()
    dialog.setWindowTitle(title)
    dialog.setAcceptMode(QtWidgets.QFileDialog.AcceptOpen)
    dialog.setFileMode(QtWidgets.QFileDialog.ExistingFile)
    dialog.setDirectory(str(start_dir))
    dialog.setNameFilter(filter_text)
    dialog.setViewMode(QtWidgets.QFileDialog.Detail)
    dialog.setOption(QtWidgets.QFileDialog.DontUseNativeDialog, True)
    dialog.setOption(QtWidgets.QFileDialog.ReadOnly, False)
    width = _coerce_int_setting(
        settings_obj, "QT_FILE_DIALOG_WIDTH", _QT_FILE_DIALOG_WIDTH, minimum=0
    )
    height = _coerce_int_setting(
        settings_obj, "QT_FILE_DIALOG_HEIGHT", _QT_FILE_DIALOG_HEIGHT, minimum=0
    )
    if width > 0 and height > 0:
        dialog.resize(width, height)
    if hasattr(QtGui.QGuiApplication, "setNavigationMode") and hasattr(
        QtCore.Qt, "NavigationModeKeypadDirectional"
    ):
        QtGui.QGuiApplication.setNavigationMode(
            QtCore.Qt.NavigationModeKeypadDirectional
        )

    def _scale_file_name_font(widget: "QtWidgets.QWidget") -> None:
        base_font = widget.font()
        scaled_font = QtGui.QFont(base_font)
        name_font_scale = _coerce_font_setting(
            settings_obj, "QT_FILE_NAME_FONT_SCALE", _QT_FILE_NAME_FONT_SCALE, minimum=0.1
        )
        effective_name_font_scale = name_font_scale / global_scale
        if base_font.pointSize() > 0:
            scaled_font.setPointSize(
                int(
                    base_font.pointSize()
                    * effective_name_font_scale
                )
            )
        elif base_font.pixelSize() > 0:
            scaled_font.setPixelSize(
                int(
                    base_font.pixelSize()
                    * effective_name_font_scale
                )
            )
        widget.setFont(scaled_font)

    for view_name in ("listView", "treeView"):
        file_view = dialog.findChild(QtWidgets.QWidget, view_name)
        if file_view is not None:
            _scale_file_name_font(file_view)

    sidebar_urls = _sidebar_urls()
    if sidebar_urls:
        dialog.setSidebarUrls(sidebar_urls)
    _widen_file_dialog_sidebar(dialog, settings_obj)
    if _coerce_bool_setting(
        settings_obj, "QT_FILE_DIALOG_MAXIMIZE", _QT_FILE_DIALOG_MAXIMIZE
    ):
        dialog.setWindowState(dialog.windowState() | QtCore.Qt.WindowMaximized)
    _configure_file_view_columns(dialog, settings_obj)
    _focus_file_view(dialog)
    try:
        QtCore.QTimer.singleShot(0, lambda: _focus_file_view(dialog))
    except Exception:
        pass
    dialog.activateWindow()
    dialog.raise_()
    dialog.setFocus(QtCore.Qt.FocusReason.ActiveWindowFocusReason)
    gamepad_layer = _enable_dialog_gamepad(dialog)
    if dialog.exec() == QtWidgets.QDialog.Accepted:
        selected_files = dialog.selectedFiles()
        if selected_files:
            if gamepad_layer is not None:
                gamepad_layer.shutdown()
            return Path(selected_files[0])
    if gamepad_layer is not None:
        gamepad_layer.shutdown()
    return None


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
    start_dir = _preferred_start_dir(initial, settings_obj, dialog_key)

    try:
        selection = _qt_file_dialog(
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
        _remember_file_dialog_dir(settings_obj, selection, dialog_key)
        if settings is None:
            _save_settings(settings_obj)

    return selection


def info_dialog(message: str) -> None:
    dialogs.info_dialog(message, logger=APP_LOGGER)


def error_dialog(message: str) -> None:
    dialogs.error_dialog(message, logger=APP_LOGGER)


# Centralize dialog helpers through the shared module.
_qt_question_dialog = dialogs.question_dialog
_qt_file_dialog = dialogs.file_dialog
_enable_dialog_gamepad = dialogs.enable_dialog_gamepad
_preferred_start_dir = dialogs.preferred_start_dir
_remember_file_dialog_dir = dialogs.remember_file_dialog_dir

def choose_install_action(title: str, text: str, select_label: str = "Select") -> str:
    """
    Show a dialog offering Download / Select / Cancel.

    Returns "Download", "Select", or "Cancel". ``select_label`` customizes the
    text shown for the "Select" button.
    """
    choice = _qt_question_dialog(
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

    settings["AP_APPIMAGE"] = str(selection)
    settings["AP_VERSION"] = ""
    settings["AP_SKIP_VERSION"] = ""
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

    settings["AP_APPIMAGE"] = str(AP_APPIMAGE_DEFAULT)
    settings["AP_VERSION"] = latest_ver
    settings["AP_SKIP_VERSION"] = ""
    settings["AP_LATEST_SEEN_VERSION"] = latest_ver
    _save_settings(settings)

    if provided_settings is not None and settings is not provided_settings:
        merged = {**provided_settings, **settings}
        provided_settings.clear()
        provided_settings.update(merged)

    info_dialog(f"Archipelago updated to {latest_ver}.")
    return True


def _prompt_select_existing_appimage(initial: Path, *, settings: Dict[str, Any]) -> Path:
    """Prompt the user to select an existing AppImage without offering download."""

    choice = _qt_question_dialog(
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


def _desktop_shortcut_path(name: str) -> Path:
    return DESKTOP_DIR / f"{name}.desktop"


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
    shortcut_path = _desktop_shortcut_path(name)

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

    current_ver = str(settings.get("AP_VERSION", "") or "")
    skip_ver = str(settings.get("AP_SKIP_VERSION", "") or "")
    latest_seen = str(settings.get("AP_LATEST_SEEN_VERSION", "") or "")
    should_prompt = _is_newer_version(latest_ver, latest_seen)
    if latest_ver and latest_ver != latest_seen:
        settings["AP_LATEST_SEEN_VERSION"] = latest_ver
        _save_settings(settings)

    if not current_ver:
        return appimage, False

    if current_ver == latest_ver or skip_ver == latest_ver:
        return appimage, False
    if not should_prompt:
        return appimage, False

    choice = _qt_question_dialog(
        title="Archipelago update",
        text="An Archipelago update is available. Update now?",
        ok_label="Update now",
        cancel_label="Later",
        extra_label="Skip this version",
    )
    if choice == "cancel":
        return appimage, False
    if choice == "extra":
        settings["AP_SKIP_VERSION"] = latest_ver
        settings["AP_LATEST_SEEN_VERSION"] = latest_ver
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

    settings["AP_APPIMAGE"] = str(AP_APPIMAGE_DEFAULT)
    settings["AP_VERSION"] = latest_ver
    settings["AP_SKIP_VERSION"] = ""
    settings["AP_LATEST_SEEN_VERSION"] = latest_ver
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
    app_path_str = str(settings.get("AP_APPIMAGE", "") or "")
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
            settings["AP_LATEST_SEEN_VERSION"] = ver
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
            settings["AP_APPIMAGE"] = str(AP_APPIMAGE_DEFAULT)
            settings["AP_VERSION"] = ver
            settings["AP_SKIP_VERSION"] = ""
            settings["AP_LATEST_SEEN_VERSION"] = ver
            _merge_and_save_settings()
            downloaded = True
        else:
            app_path = _prompt_select_existing_appimage(
                Path(os.path.expanduser("~")), settings=settings
            )
            settings["AP_APPIMAGE"] = str(app_path)
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

    # 5. Create a desktop shortcut only when a download occurred
    if downloaded:
        _create_desktop_shortcut(
            settings,
            "Archipelago",
            app_path,
            "AP_DESKTOP_SHORTCUT",
            enabled=create_shortcut,
        )

    return app_path
