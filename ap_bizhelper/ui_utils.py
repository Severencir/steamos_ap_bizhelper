from __future__ import annotations

from dataclasses import dataclass
import importlib.metadata
from pathlib import Path
from typing import Callable, Optional

from .ap_bizhelper_ap import AP_APPIMAGE_DEFAULT, force_update_appimage, manual_select_appimage
from .ap_bizhelper_bizhawk import (
    BIZHAWK_WIN_DIR,
    force_update_bizhawk,
    force_update_connectors,
    manual_select_bizhawk,
    manual_select_connectors,
)
from .ap_bizhelper_config import load_apworld_cache, load_settings
from .ap_bizhelper_worlds import force_update_apworlds, manual_select_apworld
from .dialogs import enable_dialog_gamepad, ensure_qt_app, ensure_qt_available


@dataclass
class _ComponentRow:
    name: str
    installed_version: str
    latest_seen: str
    skip_version: str
    source: str
    force_update: Callable[[], bool]
    manual_select: Callable[[], bool]


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


def show_utils_dialog(parent: Optional["QtWidgets.QWidget"] = None) -> None:
    """Display a utilities dialog with component versions and update actions."""

    ensure_qt_available()
    from PySide6 import QtCore, QtWidgets

    ensure_qt_app()
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle("ap-bizhelper utilities")
    dialog.setMinimumWidth(820)

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

    button_row = QtWidgets.QHBoxLayout()
    button_row.addStretch()
    update_app_button = QtWidgets.QPushButton("Update App")
    close_button = QtWidgets.QPushButton("Close")
    button_row.addWidget(update_app_button)
    button_row.addWidget(close_button)
    layout.addLayout(button_row)

    def _show_update_app_placeholder() -> None:
        QtWidgets.QMessageBox.information(
            dialog,
            "Update App (Placeholder)",
            "The Update App feature is not implemented yet. "
            "This button is a placeholder for future update logic.",
        )

    update_app_button.clicked.connect(_show_update_app_placeholder)
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

    _refresh_table()
    enable_dialog_gamepad(dialog, affirmative=close_button, negative=close_button, default=close_button)
    dialog.exec()
