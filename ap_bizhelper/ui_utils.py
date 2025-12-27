from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Callable, Optional

from .ap_bizhelper_ap import (
    AP_APPIMAGE_DEFAULT,
    DATA_DIR as LAUNCHER_DATA_DIR,
    DESKTOP_DIR,
    force_update_appimage,
    manual_select_appimage,
)
from .ap_bizhelper_bizhawk import (
    BIZHAWK_WIN_DIR,
    force_update_bizhawk,
    force_update_connectors,
    manual_select_bizhawk,
    manual_select_connectors,
)
from .ap_bizhelper_config import (
    APWORLD_CACHE_FILE,
    CONFIG_DIR as LAUNCHER_CONFIG_DIR,
    load_apworld_cache,
    load_settings,
    save_settings,
)
from .ap_bizhelper_worlds import WORLD_DIR, force_update_apworlds, manual_select_apworld
from .dialogs import (
    DIALOG_DEFAULTS,
    checklist_dialog,
    enable_dialog_gamepad,
    ensure_qt_app,
    ensure_qt_available,
    error_dialog,
    info_dialog,
    select_file_dialog,
)


@dataclass
class _ComponentRow:
    name: str
    installed_version: str
    latest_seen: str
    skip_version: str
    source: str
    force_update: Callable[[], bool]
    manual_select: Callable[[], bool]


@dataclass
class _ManagedDirRow:
    role: str
    path: Optional[Path]


def _app_version() -> str:
    try:
        return importlib.metadata.version("ap-bizhelper")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _dash_if_empty(value: str) -> str:
    return value if value else "—"


def _source_from_path(path: Optional[Path], managed_path: Path) -> str:
    if not path:
        return "unknown"
    try:
        resolved = path.resolve()
        managed = managed_path.resolve()
        try:
            resolved.relative_to(managed)
            return "download"
        except ValueError:
            if resolved == managed:
                return "download"
    except Exception:
        pass
    return "manual"


def _apworld_source(playable_cache: dict[str, object]) -> str:
    sources = set()
    for entry in playable_cache.values():
        source = str(entry.get("source", "") or "")
        if not source:
            continue
        if source == "manual":
            sources.add("manual")
        else:
            sources.add("download")
    if not sources:
        return "unknown"
    if len(sources) == 1:
        return next(iter(sources))
    return "mixed"


def _apworld_latest_seen(playable_cache: dict[str, object]) -> str:
    seen = {str(entry.get("latest_seen_version", "") or "") for entry in playable_cache.values()}
    seen.discard("")
    if not seen:
        return "—"
    if len(seen) == 1:
        return next(iter(seen))
    return "multiple"


def _build_component_rows() -> list[_ComponentRow]:
    settings = load_settings()
    cache = load_apworld_cache()
    playable_cache = cache.get("playable_worlds", {})

    ap_path = str(settings.get("AP_APPIMAGE", "") or "")
    ap_appimage = Path(ap_path) if ap_path else None

    bizhawk_path = str(settings.get("BIZHAWK_EXE", "") or "")
    bizhawk_exe = Path(bizhawk_path) if bizhawk_path else None

    connectors_ap_version = str(settings.get("BIZHAWK_AP_CONNECTOR_VERSION", "") or "")
    connectors_sni_version = str(settings.get("BIZHAWK_SNI_VERSION", "") or "")
    connectors_latest_seen = str(
        settings.get("BIZHAWK_AP_CONNECTOR_LATEST_SEEN_VERSION", "") or ""
    )

    connectors_source = "unknown"
    if connectors_ap_version or connectors_sni_version:
        connectors_source = "download"
        if "manual" in (connectors_ap_version, connectors_sni_version):
            connectors_source = "manual"

    apworld_count = len(playable_cache)
    apworld_installed = f"{apworld_count} cached" if apworld_count else "—"

    return [
        _ComponentRow(
            name="AP AppImage",
            installed_version=_dash_if_empty(str(settings.get("AP_VERSION", "") or "")),
            latest_seen=_dash_if_empty(str(settings.get("AP_LATEST_SEEN_VERSION", "") or "")),
            skip_version=_dash_if_empty(str(settings.get("AP_SKIP_VERSION", "") or "")),
            source=_source_from_path(ap_appimage, AP_APPIMAGE_DEFAULT),
            force_update=lambda: force_update_appimage(settings),
            manual_select=lambda: bool(manual_select_appimage(settings)),
        ),
        _ComponentRow(
            name="BizHawk",
            installed_version=_dash_if_empty(str(settings.get("BIZHAWK_VERSION", "") or "")),
            latest_seen=_dash_if_empty(str(settings.get("BIZHAWK_LATEST_SEEN_VERSION", "") or "")),
            skip_version=_dash_if_empty(str(settings.get("BIZHAWK_SKIP_VERSION", "") or "")),
            source=_source_from_path(bizhawk_exe, BIZHAWK_WIN_DIR),
            force_update=lambda: force_update_bizhawk(settings),
            manual_select=lambda: bool(manual_select_bizhawk(settings)),
        ),
        _ComponentRow(
            name="Connectors",
            installed_version=_dash_if_empty(
                f"AP: {connectors_ap_version or '—'} / SNI: {connectors_sni_version or '—'}"
            ),
            latest_seen=_dash_if_empty(f"AP: {connectors_latest_seen or '—'} / SNI: —"),
            skip_version="—",
            source=connectors_source,
            force_update=lambda: force_update_connectors(settings),
            manual_select=lambda: manual_select_connectors(settings),
        ),
        _ComponentRow(
            name="APWorlds",
            installed_version=apworld_installed,
            latest_seen=_apworld_latest_seen(playable_cache),
            skip_version="—",
            source=_apworld_source(playable_cache),
            force_update=force_update_apworlds,
            manual_select=manual_select_apworld,
        ),
    ]


def _managed_dir_display(path: Optional[Path]) -> str:
    if not path:
        return "—"
    return str(path)


def _managed_dir_exists(path: Optional[Path]) -> bool:
    if not path:
        return False
    return path.exists()


def _bizhawk_install_dirs(settings: dict) -> list[Path]:
    installs: list[Path] = []

    def _add(path: Optional[Path]) -> None:
        if not path:
            return
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        if resolved not in installs:
            installs.append(resolved)

    _add(BIZHAWK_WIN_DIR)

    bizhawk_path = str(settings.get("BIZHAWK_EXE", "") or "")
    if bizhawk_path:
        candidate = Path(bizhawk_path)
        if candidate.is_file():
            candidate = candidate.parent
        _add(candidate)

    return installs


def _build_managed_dir_rows() -> list[_ManagedDirRow]:
    settings = load_settings()
    rows: list[_ManagedDirRow] = [
        _ManagedDirRow("AP config", AP_CONFIG_DIR),
        _ManagedDirRow("AP local", AP_DATA_DIR),
        _ManagedDirRow("Launcher data", LAUNCHER_DATA_DIR),
        _ManagedDirRow("APWorlds", WORLD_DIR),
    ]

    for install_dir in _bizhawk_install_dirs(settings):
        rows.append(_ManagedDirRow("BizHawk install", install_dir))
        rows.append(_ManagedDirRow("Connectors", install_dir / "connectors"))

    return rows


def _open_path_in_manager(path: Path) -> None:
    opener = shutil.which("xdg-open")
    if not opener:
        error_dialog("xdg-open is not available to open folders.")
        return
    try:
        subprocess.Popen([opener, str(path)])
    except Exception as exc:
        error_dialog(f"Failed to open {path}:\n{exc}")


def _format_status_text() -> str:
    settings = load_settings()
    cache = load_apworld_cache()
    rows = _build_component_rows()

    lines = ["ap-bizhelper status", f"App version: {_app_version()}", ""]
    lines.append("Components:")
    for row in rows:
        lines.append(f"- {row.name}")
        lines.append(f"  Installed version: {row.installed_version}")
        lines.append(f"  Latest seen: {row.latest_seen}")
        lines.append(f"  Skip version: {row.skip_version}")
        lines.append(f"  Source: {row.source}")
    lines.append("")
    lines.append("Paths:")

    def _path_line(label: str, value: Optional[str]) -> None:
        lines.append(f"- {label}: {_dash_if_empty(value or '')}")

    _path_line("AP AppImage", str(settings.get("AP_APPIMAGE", "") or ""))
    _path_line("BizHawk EXE", str(settings.get("BIZHAWK_EXE", "") or ""))
    _path_line("Proton bin", str(settings.get("PROTON_BIN", "") or ""))
    _path_line("BizHawk runner", str(settings.get("BIZHAWK_RUNNER", "") or ""))
    _path_line("SFC Lua path", str(settings.get("SFC_LUA_PATH", "") or ""))
    _path_line("APWorld cache", str(APWORLD_CACHE_FILE))
    _path_line("Cached APWorlds", str(len(cache.get("playable_worlds", {}))))

    return "\n".join(lines)


AP_CONFIG_DIR = Path(os.path.expanduser("~/.config/Archipelago"))
AP_DATA_DIR = Path(os.path.expanduser("~/.local/share/Archipelago"))
BACKUPS_DIR = Path(os.path.expanduser("~/.local/share/ap-bizhelper/backups"))
EXPORTS_DIR = LAUNCHER_DATA_DIR / "exports"
GAME_SAVES_DIR = Path(os.path.expanduser("~/.local/share/ap-bizhelper/saves"))
AP_DESKTOP_SHORTCUT = DESKTOP_DIR / "Archipelago.desktop"
BIZHAWK_SHORTCUT = DESKTOP_DIR / "BizHawk-Proton.sh"
BIZHAWK_LEGACY_SHORTCUT = DESKTOP_DIR / "BizHawk-Proton.desktop"
SETTINGS_EXPORT_VERSION = 1
SETTINGS_EXPORT_PREFIX = "ap-bizhelper-settings"

_RESET_PRESERVE_KEYS = ("STEAM_APPID",)
_IMPORT_PRESERVE_KEYS = ("AP_APPIMAGE", "BIZHAWK_EXE", "BIZHAWK_RUNNER", "PROTON_BIN")


def _ensure_exports_dir() -> Optional[Path]:
    try:
        EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        error_dialog(f"Failed to create export directory:\n{EXPORTS_DIR}\n\n{exc}")
        return None
    return EXPORTS_DIR


def _write_settings_export(settings: dict, export_path: Path) -> None:
    payload = {
        "format_version": SETTINGS_EXPORT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "app_version": _app_version(),
        "settings": settings,
    }
    export_path.parent.mkdir(parents=True, exist_ok=True)
    with export_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _read_settings_export(export_path: Path) -> Optional[dict]:
    try:
        with export_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as exc:
        error_dialog(f"Failed to read settings export:\n{export_path}\n\n{exc}")
        return None

    if not isinstance(payload, dict):
        error_dialog("Settings export format is invalid (expected a JSON object).")
        return None

    version = payload.get("format_version")
    if version != SETTINGS_EXPORT_VERSION:
        error_dialog(
            "Settings export format version is unsupported.\n\n"
            f"Expected {SETTINGS_EXPORT_VERSION}, got {version!r}."
        )
        return None

    settings = payload.get("settings")
    if not isinstance(settings, dict):
        error_dialog("Settings export is missing the settings payload.")
        return None

    return settings


def _is_under_dir(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def _safe_remove_path(path: Path, deleted: list[str], errors: list[str]) -> None:
    try:
        if not path.exists() and not path.is_symlink():
            return
        resolved = path.resolve()
        if resolved in {Path("/"), Path.home()}:
            errors.append(f"Refusing to remove unsafe path: {path}")
            return
        if path.is_symlink() or path.is_file():
            path.unlink()
            deleted.append(str(path))
            return
        if path.is_dir():
            shutil.rmtree(path)
            deleted.append(str(path))
    except Exception as exc:
        errors.append(f"Failed to remove {path}: {exc}")


def _relocate_appimage(
    appimage_path: Path, preserved: list[str], errors: list[str]
) -> Optional[Path]:
    try:
        downloads_dir = Path(os.path.expanduser("~/Downloads"))
        downloads_dir.mkdir(parents=True, exist_ok=True)
        target = downloads_dir / appimage_path.name
        if target.exists():
            stem = appimage_path.stem
            suffix = appimage_path.suffix
            counter = 1
            while True:
                candidate = downloads_dir / f"{stem}-{counter}{suffix}"
                if not candidate.exists():
                    target = candidate
                    break
                counter += 1
        shutil.move(str(appimage_path), str(target))
        preserved.append(str(target))
        return target
    except Exception as exc:
        errors.append(f"Failed to preserve AppImage {appimage_path}: {exc}")
        return None


def _uninstall_app(
    dialog: "QtWidgets.QDialog", stored_appimage_path: Optional[Path] = None
) -> None:
    settings = load_settings()
    selected = checklist_dialog(
        "Uninstall ap-bizhelper",
        "Select optional data to remove. Local/config folders and desktop shortcuts will be removed.",
        [
            (False, "Uninstall backups"),
            (False, "Uninstall game saves"),
            (False, "Uninstall AppImage"),
        ],
        ok_label="Uninstall",
        cancel_label="Cancel",
        height=260,
    )
    if selected is None:
        return

    remove_backups = "Uninstall backups" in selected
    remove_saves = "Uninstall game saves" in selected
    remove_appimage = "Uninstall AppImage" in selected
    if stored_appimage_path is None:
        settings_appimage = str(settings.get("AP_APPIMAGE") or "")
        if settings_appimage:
            stored_appimage_path = Path(settings_appimage)
    appimage_path = stored_appimage_path

    deleted: list[str] = []
    preserved: list[str] = []
    errors: list[str] = []

    preserved_appimage: Optional[Path] = None
    if appimage_path and not remove_appimage and appimage_path.exists():
        if _is_under_dir(appimage_path, LAUNCHER_DATA_DIR):
            preserved_appimage = _relocate_appimage(appimage_path, preserved, errors)
        else:
            preserved.append(str(appimage_path))

    if remove_appimage and appimage_path:
        _safe_remove_path(appimage_path, deleted, errors)

    _safe_remove_path(AP_CONFIG_DIR, deleted, errors)
    _safe_remove_path(AP_DATA_DIR, deleted, errors)
    _safe_remove_path(LAUNCHER_CONFIG_DIR, deleted, errors)
    if preserved_appimage is None:
        _safe_remove_path(LAUNCHER_DATA_DIR, deleted, errors)
    else:
        for child in LAUNCHER_DATA_DIR.glob("*"):
            if child == preserved_appimage:
                continue
            _safe_remove_path(child, deleted, errors)

    _safe_remove_path(AP_DESKTOP_SHORTCUT, deleted, errors)
    _safe_remove_path(BIZHAWK_SHORTCUT, deleted, errors)
    _safe_remove_path(BIZHAWK_LEGACY_SHORTCUT, deleted, errors)

    if remove_backups:
        _safe_remove_path(BACKUPS_DIR, deleted, errors)
    if remove_saves:
        _safe_remove_path(GAME_SAVES_DIR, deleted, errors)

    if errors:
        error_dialog("Uninstall completed with errors:\n" + "\n".join(errors))
        return

    summary = "Uninstall complete."
    if deleted:
        summary = summary + "\n\nRemoved:\n" + "\n".join(f"- {item}" for item in deleted)
    if preserved:
        summary = summary + "\n\nPreserved:\n" + "\n".join(f"- {item}" for item in preserved)
    info_dialog(summary)


def _reset_settings() -> None:
    settings = load_settings()
    preserved = {key: settings.get(key) for key in _RESET_PRESERVE_KEYS if settings.get(key)}
    save_settings({**DIALOG_DEFAULTS, **preserved})
    info_dialog("Settings reset to defaults.")


def _export_settings() -> None:
    export_dir = _ensure_exports_dir()
    if not export_dir:
        return
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    export_path = export_dir / f"{SETTINGS_EXPORT_PREFIX}-{timestamp}.json"
    settings = load_settings()
    try:
        _write_settings_export(settings, export_path)
    except Exception as exc:
        error_dialog(f"Failed to export settings:\n{export_path}\n\n{exc}")
        return
    info_dialog(f"Settings exported to:\n{export_path}")


def _import_settings(parent: Optional["QtWidgets.QWidget"] = None) -> bool:
    export_dir = _ensure_exports_dir()
    if not export_dir:
        return False
    selection = select_file_dialog(
        title="Import settings",
        dialog_key="settings-import",
        initial=export_dir,
        file_filter="ap-bizhelper settings (*.json);;All files (*)",
    )
    if not selection:
        return False

    ensure_qt_available()
    from PySide6 import QtWidgets

    response = QtWidgets.QMessageBox.warning(
        parent,
        "Import settings?",
        "Import settings from the selected file?\n"
        "This will replace your current settings (managed paths stay as-is).",
        QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        QtWidgets.QMessageBox.No,
    )
    if response != QtWidgets.QMessageBox.Yes:
        return False

    imported_settings = _read_settings_export(selection)
    if imported_settings is None:
        return False

    current_settings = load_settings()
    preserved = {
        key: current_settings.get(key)
        for key in _IMPORT_PRESERVE_KEYS
        if key in current_settings
    }
    merged = {**imported_settings, **preserved}
    save_settings(merged)
    info_dialog("Settings imported.")
    return True


def show_managed_dirs_dialog(parent: Optional["QtWidgets.QWidget"] = None) -> None:
    """Display a dialog listing managed directories with quick open actions."""

    ensure_qt_available()
    from PySide6 import QtCore, QtWidgets

    ensure_qt_app()
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle("Managed directories")
    dialog.setMinimumWidth(720)

    layout = QtWidgets.QVBoxLayout(dialog)
    table = QtWidgets.QTableWidget()
    table.setColumnCount(2)
    table.setHorizontalHeaderLabels(["Role", "Path"])
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
    table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
    table.setFocusPolicy(QtCore.Qt.NoFocus)
    table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
    table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
    layout.addWidget(table)

    rows = _build_managed_dir_rows()
    table.setRowCount(len(rows))

    for row_index, row in enumerate(rows):
        role_item = QtWidgets.QTableWidgetItem(row.role)
        role_item.setFlags(role_item.flags() & ~QtCore.Qt.ItemIsEditable)
        table.setItem(row_index, 0, role_item)

        path_text = _managed_dir_display(row.path)
        path_widget = QtWidgets.QWidget()
        path_layout = QtWidgets.QHBoxLayout(path_widget)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.setSpacing(8)

        label = QtWidgets.QLabel(path_text)
        label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        path_layout.addWidget(label)

        open_button = QtWidgets.QPushButton("Open")
        if row.path and _managed_dir_exists(row.path):
            open_button.clicked.connect(lambda _=False, p=row.path: _open_path_in_manager(p))
        else:
            open_button.setEnabled(False)
            open_button.setToolTip("Path not available.")
        path_layout.addWidget(open_button)
        path_layout.addStretch()

        table.setCellWidget(row_index, 1, path_widget)

    button_row = QtWidgets.QHBoxLayout()
    button_row.addStretch()
    close_button = QtWidgets.QPushButton("Close")
    close_button.clicked.connect(dialog.reject)
    button_row.addWidget(close_button)
    layout.addLayout(button_row)

    enable_dialog_gamepad(dialog, affirmative=close_button, negative=close_button, default=close_button)
    dialog.exec()


def show_utils_dialog(parent: Optional["QtWidgets.QWidget"] = None) -> None:
    """Display a utilities dialog with component versions and update actions."""

    ensure_qt_available()
    from PySide6 import QtCore, QtWidgets

    ensure_qt_app()
    settings = load_settings()
    stored_appimage = str(settings.get("AP_APPIMAGE") or "")
    stored_appimage_path = Path(stored_appimage) if stored_appimage else None
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle("ap-bizhelper utilities")

    layout = QtWidgets.QVBoxLayout(dialog)
    header = QtWidgets.QLabel(f"ap-bizhelper version: {_app_version()}")
    layout.addWidget(header)

    table = QtWidgets.QTableWidget()
    table.setColumnCount(6)
    table.setHorizontalHeaderLabels(
        ["Component", "Installed version", "Latest seen", "Skip version", "Source", "Actions"]
    )
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
    table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
    table.setFocusPolicy(QtCore.Qt.NoFocus)
    table.horizontalHeader().setStretchLastSection(True)
    table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
    table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
    table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
    table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)
    table.horizontalHeader().setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeToContents)
    layout.addWidget(table)

    copy_status_button = QtWidgets.QPushButton("Copy status")
    managed_dirs_button = QtWidgets.QPushButton("Managed dirs")
    export_settings_button = QtWidgets.QPushButton("Export settings")
    open_exports_button = QtWidgets.QPushButton("Open exports")
    import_settings_button = QtWidgets.QPushButton("Import settings")
    update_app_button = QtWidgets.QPushButton("Update App")
    rollback_button = QtWidgets.QPushButton("Rollback")
    reset_settings_button = QtWidgets.QPushButton("Reset settings")
    uninstall_button = QtWidgets.QPushButton("Uninstall")
    close_button = QtWidgets.QPushButton("Close")

    button_row_top = QtWidgets.QHBoxLayout()
    button_row_top.addWidget(copy_status_button)
    button_row_top.addWidget(managed_dirs_button)
    button_row_top.addWidget(update_app_button)
    button_row_top.addStretch()
    layout.addLayout(button_row_top)

    button_row_middle = QtWidgets.QHBoxLayout()
    button_row_middle.addWidget(import_settings_button)
    button_row_middle.addWidget(export_settings_button)
    button_row_middle.addWidget(open_exports_button)
    button_row_middle.addStretch()
    layout.addLayout(button_row_middle)

    button_row_bottom = QtWidgets.QHBoxLayout()
    button_row_bottom.addWidget(rollback_button)
    button_row_bottom.addWidget(reset_settings_button)
    button_row_bottom.addWidget(uninstall_button)
    button_row_bottom.addStretch()
    layout.addLayout(button_row_bottom)

    button_row_close = QtWidgets.QHBoxLayout()
    button_row_close.addStretch()
    button_row_close.addWidget(close_button)
    layout.addLayout(button_row_close)

    def _show_update_app_placeholder() -> None:
        QtWidgets.QMessageBox.information(
            dialog,
            "Update App (Placeholder)",
            "The Update App feature is not implemented yet. "
            "This button is a placeholder for future update logic.",
        )

    update_app_button.clicked.connect(_show_update_app_placeholder)
    def _show_rollback_placeholder() -> None:
        QtWidgets.QMessageBox.information(
            dialog,
            "Rollback (Planned)",
            "Rollback is planned but requires the snapshot system. "
            "Once snapshots are available, this action will restore the latest snapshot.",
        )

    rollback_button.clicked.connect(_show_rollback_placeholder)
    uninstall_button.clicked.connect(
        lambda: _uninstall_app(dialog, stored_appimage_path=stored_appimage_path)
    )
    copy_status_button.clicked.connect(
        lambda: QtWidgets.QApplication.clipboard().setText(_format_status_text())
    )
    managed_dirs_button.clicked.connect(lambda: show_managed_dirs_dialog(dialog))
    export_settings_button.clicked.connect(_export_settings)
    open_exports_button.clicked.connect(
        lambda: _open_path_in_manager(EXPORTS_DIR)
        if _ensure_exports_dir()
        else None
    )
    import_settings_button.clicked.connect(
        lambda: _refresh_table() if _import_settings(dialog) else None
    )
    reset_settings_button.clicked.connect(lambda: _confirm_reset_settings(dialog))
    close_button.clicked.connect(dialog.reject)

    def _refresh_table() -> None:
        rows = _build_component_rows()
        table.setRowCount(len(rows))

        for row_index, row in enumerate(rows):
            values = [
                row.name,
                row.installed_version,
                row.latest_seen,
                row.skip_version,
                row.source,
            ]
            for col_index, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                item.setFlags(item.flags() & ~QtCore.Qt.ItemIsEditable)
                table.setItem(row_index, col_index, item)

            action_widget = QtWidgets.QWidget()
            action_layout = QtWidgets.QHBoxLayout(action_widget)
            action_layout.setContentsMargins(0, 0, 0, 0)
            action_layout.setSpacing(6)

            force_button = QtWidgets.QPushButton("Force update")
            manual_button = QtWidgets.QPushButton("Manual select")
            action_layout.addWidget(force_button)
            action_layout.addWidget(manual_button)
            action_layout.addStretch()

            force_button.clicked.connect(lambda _=False, cb=row.force_update: _run_action(cb))
            manual_button.clicked.connect(lambda _=False, cb=row.manual_select: _run_action(cb))

            table.setCellWidget(row_index, 5, action_widget)

    def _run_action(action: Callable[[], bool]) -> None:
        action()
        _refresh_table()

    def _confirm_reset_settings(parent: Optional["QtWidgets.QWidget"] = None) -> None:
        response = QtWidgets.QMessageBox.warning(
            parent or dialog,
            "Reset settings?",
            "Reset settings to defaults? This will clear saved paths and preferences.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if response != QtWidgets.QMessageBox.Yes:
            return
        _reset_settings()
        _refresh_table()

    _refresh_table()
    table.resizeColumnsToContents()
    dialog.adjustSize()
    size_hint = dialog.sizeHint()
    target_width = int(1400)
    dialog.setMinimumWidth(target_width)
    dialog.resize(target_width, size_hint.height())
    enable_dialog_gamepad(dialog, affirmative=close_button, negative=close_button, default=close_button)
    dialog.exec()
