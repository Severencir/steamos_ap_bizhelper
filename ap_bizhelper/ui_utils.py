from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import importlib.metadata
import json
import shlex
from pathlib import Path
import shutil
import subprocess
from typing import Callable, Iterable, Optional

from .ap_bizhelper_ap import AP_APPIMAGE_DEFAULT, force_update_appimage, manual_select_appimage
from .ap_bizhelper_bizhawk import (
    ensure_runtime_root,
    force_update_bizhawk,
    manual_select_bizhawk,
)
from .ap_bizhelper_config import (
    APWORLD_CACHE_FILE,
    CONFIG_DIR as LAUNCHER_CONFIG_DIR,
    get_path_setting,
    load_apworld_cache,
    load_settings,
    save_settings,
)
from .ap_bizhelper_worlds import WORLD_DIR, force_update_apworlds, manual_select_apworld
from .constants import (
    AP_APPIMAGE_KEY,
    AP_LATEST_SEEN_VERSION_KEY,
    AP_SKIP_VERSION_KEY,
    AP_VERSION_KEY,
    ARCHIPELAGO_CONFIG_DIR,
    ARCHIPELAGO_DATA_DIR,
    BACKUPS_DIR,
    BIZHELPER_APPIMAGE_KEY,
    BIZHAWK_EXE_KEY,
    BIZHAWK_INSTALL_DIR_KEY,
    BIZHAWK_LATEST_SEEN_KEY,
    BIZHAWK_RUNNER_KEY,
    BIZHAWK_SKIP_VERSION_KEY,
    BIZHAWK_VERSION_KEY,
    BIZHAWK_RUNTIME_ROOT_KEY,
    BIZHAWK_RUNTIME_DOWNLOAD_KEY,
    DATA_DIR as LAUNCHER_DATA_DIR,
    DESKTOP_DIR_KEY,
    DOWNLOADS_DIR_KEY,
    GAME_SAVES_DIR,
    SFC_LUA_PATH_KEY,
    STEAM_APPID_KEY,
)
from .dialogs import (
    DIALOG_DEFAULTS,
    DialogButtonSpec,
    checklist_dialog,
    copy_to_clipboard,
    error_dialog,
    gui_available,
    info_dialog,
    list_action_dialog,
    question_dialog,
    run_custom_dialog,
    select_file_dialog,
)
from .logging_utils import get_app_logger


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

    ap_path = str(settings.get(AP_APPIMAGE_KEY, "") or "")
    ap_appimage = Path(ap_path) if ap_path else None

    bizhawk_path = str(settings.get(BIZHAWK_EXE_KEY, "") or "")
    bizhawk_exe = Path(bizhawk_path) if bizhawk_path else None
    bizhawk_install = get_path_setting(settings, BIZHAWK_INSTALL_DIR_KEY)

    runtime_root = get_path_setting(settings, BIZHAWK_RUNTIME_ROOT_KEY)
    runtime_status = "missing"
    try:
        from .ap_bizhelper_bizhawk import validate_runtime_root

        validate_runtime_root(runtime_root)
        runtime_status = "staged"
    except Exception:
        runtime_status = "missing"

    runtime_source = "download" if settings.get(BIZHAWK_RUNTIME_DOWNLOAD_KEY, True) else "manual"

    return [
        _ComponentRow(
            name="AP AppImage",
            installed_version=_dash_if_empty(str(settings.get(AP_VERSION_KEY, "") or "")),
            latest_seen=_dash_if_empty(str(settings.get(AP_LATEST_SEEN_VERSION_KEY, "") or "")),
            skip_version=_dash_if_empty(str(settings.get(AP_SKIP_VERSION_KEY, "") or "")),
            source=_source_from_path(ap_appimage, AP_APPIMAGE_DEFAULT),
            force_update=lambda: force_update_appimage(settings),
            manual_select=lambda: bool(manual_select_appimage(settings)),
        ),
        _ComponentRow(
            name="BizHawk",
            installed_version=_dash_if_empty(str(settings.get(BIZHAWK_VERSION_KEY, "") or "")),
            latest_seen=_dash_if_empty(str(settings.get(BIZHAWK_LATEST_SEEN_KEY, "") or "")),
            skip_version=_dash_if_empty(str(settings.get(BIZHAWK_SKIP_VERSION_KEY, "") or "")),
            source=_source_from_path(bizhawk_exe, bizhawk_install),
            force_update=lambda: force_update_bizhawk(settings),
            manual_select=lambda: bool(manual_select_bizhawk(settings)),
        ),
        _ComponentRow(
            name="BizHawk runtime",
            installed_version=runtime_status,
            latest_seen="—",
            skip_version="—",
            source=runtime_source,
            force_update=lambda: bool(
                ensure_runtime_root(settings, download_enabled=True, prompt_on_missing=True)
            ),
            manual_select=lambda: bool(
                ensure_runtime_root(settings, download_enabled=False, prompt_on_missing=True)
            ),
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


def _downloads_dir(settings: dict) -> Path:
    return get_path_setting(settings, DOWNLOADS_DIR_KEY)


def _desktop_dir(settings: dict) -> Path:
    return get_path_setting(settings, DESKTOP_DIR_KEY)


def _bizhawk_saveram_dir(settings: dict) -> Path:
    return get_path_setting(settings, "BIZHAWK_SAVERAM_DIR")


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

    bizhawk_path = str(settings.get(BIZHAWK_EXE_KEY, "") or "")
    if bizhawk_path:
        candidate = Path(bizhawk_path)
        if candidate.is_file():
            candidate = candidate.parent
        _add(candidate)

    install_root = get_path_setting(settings, BIZHAWK_INSTALL_DIR_KEY)
    if install_root:
        _add(install_root)

    return installs


def _build_managed_dir_rows() -> list[_ManagedDirRow]:
    settings = load_settings()
    rows: list[_ManagedDirRow] = [
        _ManagedDirRow("AP config", AP_CONFIG_DIR),
        _ManagedDirRow("AP local", AP_DATA_DIR),
        _ManagedDirRow("Launcher data", LAUNCHER_DATA_DIR),
        _ManagedDirRow("APWorlds", WORLD_DIR),
        _ManagedDirRow("BizHawk SaveRAM", _bizhawk_saveram_dir(settings)),
        _ManagedDirRow(
            "BizHawk runtime", get_path_setting(settings, BIZHAWK_RUNTIME_ROOT_KEY)
        ),
    ]

    for install_dir in _bizhawk_install_dirs(settings):
        rows.append(_ManagedDirRow("BizHawk install", install_dir))

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

    _path_line("AP AppImage", str(settings.get(AP_APPIMAGE_KEY, "") or ""))
    _path_line("BizHawk EXE", str(settings.get(BIZHAWK_EXE_KEY, "") or ""))
    _path_line("BizHawk runner", str(settings.get(BIZHAWK_RUNNER_KEY, "") or ""))
    _path_line("BizHawk runtime", str(settings.get(BIZHAWK_RUNTIME_ROOT_KEY, "") or ""))
    _path_line("SFC Lua path", str(settings.get(SFC_LUA_PATH_KEY, "") or ""))
    _path_line("APWorld cache", str(APWORLD_CACHE_FILE))
    _path_line("Cached APWorlds", str(len(cache.get("playable_worlds", {}))))

    return "\n".join(lines)


def show_apworlds_dialog(parent: Optional[object] = None) -> None:
    """Display managed APWorlds with actions to refresh them."""

    if not gui_available():
        cache = load_apworld_cache()
        playable_cache = cache.get("playable_worlds", {})
        if not playable_cache:
            info_dialog("No APWorlds are currently managed.")
            return
        lines = [
            "Managed APWorlds:",
            *[
                f"- {name}: version={entry.get('version', '—')} latest={entry.get('latest_seen_version', '—')} source={entry.get('source', '—')}"
                for name, entry in sorted(playable_cache.items(), key=lambda item: str(item[0]).casefold())
            ],
        ]
        info_dialog("\n".join(lines), title="ap-bizhelper APWorlds")
        return

    while True:
        cache = load_apworld_cache()
        playable_cache = cache.get("playable_worlds", {})
        if not playable_cache:
            info_dialog("No APWorlds are currently managed.")
            return
        items = []
        mapping: dict[str, str] = {}
        for idx, world_name in enumerate(sorted(playable_cache.keys(), key=str.casefold), start=1):
            entry = playable_cache.get(world_name, {})
            installed_version = _dash_if_empty(str(entry.get("version", "") or ""))
            latest_seen = _dash_if_empty(str(entry.get("latest_seen_version", "") or ""))
            source = _dash_if_empty(str(entry.get("source", "") or ""))
            label = f"{idx}. {world_name} | {installed_version} | {latest_seen} | {source}"
            items.append(label)
            mapping[label] = world_name

        selection, action = list_action_dialog(
            title="ap-bizhelper APWorlds",
            text="Managed APWorlds",
            items=items,
            actions=[
                DialogButtonSpec("Force update", role="positive", is_default=True),
                DialogButtonSpec("Manual select", role="special"),
            ],
            cancel_label="Close",
        )
        if action is None or action.role == "negative":
            return
        if not selection:
            info_dialog("Select an APWorld from the list first.")
            continue
        chosen = mapping.get(selection)
        if not chosen:
            info_dialog("Select an APWorld from the list first.")
            continue
        if action.label == "Force update":
            force_update_apworlds(chosen)
        elif action.label == "Manual select":
            manual_select_apworld(chosen)


AP_CONFIG_DIR = ARCHIPELAGO_CONFIG_DIR
AP_DATA_DIR = ARCHIPELAGO_DATA_DIR
EXPORTS_DIR = LAUNCHER_DATA_DIR / "exports"
SETTINGS_EXPORT_VERSION = 1
SETTINGS_EXPORT_PREFIX = "ap-bizhelper-settings"
LOCAL_ACTIONS_CREATED_KEY = "LOCAL_ACTIONS_CREATED"
LOCAL_ACTION_SCRIPTS = {
    "ap-bizhelper-utils.sh": ("utils",),
    "ap-bizhelper-uninstall.sh": ("uninstall",),
}

UNINSTALL_OPTION_MANAGED_DIRS = "Uninstall managed directories"
UNINSTALL_OPTION_APPIMAGE = "Uninstall AppImage"
UNINSTALL_OPTION_BACKUPS = "Uninstall backups"
UNINSTALL_OPTION_SAVES = "Uninstall game saves"
UNINSTALL_OPTIONS = (
    (True, UNINSTALL_OPTION_MANAGED_DIRS),
    (True, UNINSTALL_OPTION_APPIMAGE),
    (False, UNINSTALL_OPTION_BACKUPS),
    (False, UNINSTALL_OPTION_SAVES),
)
UNINSTALL_DEFAULT_SELECTIONS = frozenset(
    {
        UNINSTALL_OPTION_MANAGED_DIRS,
        UNINSTALL_OPTION_APPIMAGE,
    }
)
UNINSTALL_ALL_SELECTIONS = frozenset(
    {
        UNINSTALL_OPTION_MANAGED_DIRS,
        UNINSTALL_OPTION_APPIMAGE,
        UNINSTALL_OPTION_BACKUPS,
        UNINSTALL_OPTION_SAVES,
    }
)

_RESET_PRESERVE_KEYS = (STEAM_APPID_KEY,)
_IMPORT_PRESERVE_KEYS = (
    AP_APPIMAGE_KEY,
    BIZHAWK_EXE_KEY,
    BIZHAWK_RUNNER_KEY,
    BIZHAWK_RUNTIME_ROOT_KEY,
)


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
    appimage_path: Path, preserved: list[str], errors: list[str], downloads_dir: Path
) -> Optional[Path]:
    try:
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


def _stored_appimage_path(settings: dict) -> Optional[Path]:
    stored_appimage = str(settings.get(BIZHELPER_APPIMAGE_KEY) or "")
    return Path(stored_appimage) if stored_appimage else None


def _uninstall_app(
    *,
    stored_appimage_path: Optional[Path] = None,
    selected: Optional[Iterable[str]] = None,
) -> None:
    settings = load_settings()
    if selected is None:
        selected = checklist_dialog(
            "Uninstall ap-bizhelper",
            "Select what to remove. Desktop shortcuts are removed with managed directories.",
            list(UNINSTALL_OPTIONS),
            ok_label="Uninstall",
            cancel_label="Cancel",
            height=500,
        )
        if selected is None:
            return

    selected_set = set(selected)
    remove_managed_dirs = UNINSTALL_OPTION_MANAGED_DIRS in selected_set
    remove_backups = UNINSTALL_OPTION_BACKUPS in selected_set
    remove_saves = UNINSTALL_OPTION_SAVES in selected_set
    remove_appimage = UNINSTALL_OPTION_APPIMAGE in selected_set
    if stored_appimage_path is None:
        stored_appimage_path = _stored_appimage_path(settings)
    appimage_path = stored_appimage_path

    deleted: list[str] = []
    preserved: list[str] = []
    errors: list[str] = []

    preserved_appimage: Optional[Path] = None
    if appimage_path and not remove_appimage and appimage_path.exists():
        if remove_managed_dirs and _is_under_dir(appimage_path, LAUNCHER_DATA_DIR):
            preserved_appimage = _relocate_appimage(
                appimage_path, preserved, errors, _downloads_dir(settings)
            )
        else:
            preserved.append(str(appimage_path))

    if remove_appimage and appimage_path:
        _safe_remove_path(appimage_path, deleted, errors)

    if remove_managed_dirs:
        desktop_dir = _desktop_dir(settings)
        ap_desktop_shortcut = desktop_dir / "Archipelago.desktop"
        bizhawk_shortcut = desktop_dir / "BizHawk.sh"
        bizhawk_legacy_shortcuts = [
            desktop_dir / "BizHawk-Proton.sh",
            desktop_dir / "BizHawk-Proton.desktop",
        ]
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
        _safe_remove_path(ap_desktop_shortcut, deleted, errors)
        _safe_remove_path(bizhawk_shortcut, deleted, errors)
        for legacy_shortcut in bizhawk_legacy_shortcuts:
            _safe_remove_path(legacy_shortcut, deleted, errors)

    if remove_backups:
        _safe_remove_path(BACKUPS_DIR, deleted, errors)
    if remove_saves:
        _safe_remove_path(GAME_SAVES_DIR, deleted, errors)
        _safe_remove_path(_bizhawk_saveram_dir(settings), deleted, errors)

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


def _import_settings(parent: Optional[object] = None) -> bool:
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

    response = question_dialog(
        title="Import settings?",
        text=(
            "Import settings from the selected file?\n"
            "This will replace your current settings (managed paths stay as-is)."
        ),
        ok_label="Import",
        cancel_label="Cancel",
    )
    if response != "ok":
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


def show_managed_dirs_dialog(parent: Optional[object] = None) -> None:
    """Display a dialog listing managed directories with quick open actions."""

    rows = _build_managed_dir_rows()
    items = []
    mapping: dict[str, _ManagedDirRow] = {}
    for idx, row in enumerate(rows, start=1):
        path_text = _managed_dir_display(row.path)
        label = f\"{idx}. {row.role}: {path_text}\"
        items.append(label)
        mapping[label] = row

    if not gui_available():
        info_dialog(\"\\n\".join(items), title=\"Managed directories\")
        return

    while True:
        selection, action = list_action_dialog(
            title=\"Managed directories\",
            text=\"Select a directory to open.\",
            items=items,
            actions=[DialogButtonSpec(\"Open\", role=\"positive\", is_default=True)],
            cancel_label=\"Close\",
        )
        if action is None or action.role == \"negative\":
            return
        if not selection:
            info_dialog(\"Select a directory from the list first.\")
            continue
        row = mapping.get(selection)
        if not row or not row.path or not _managed_dir_exists(row.path):
            info_dialog(\"That path is not available on this device.\")
            continue
        _open_path_in_manager(row.path)


def show_utils_dialog(parent: Optional[object] = None) -> None:
    """Display a utilities dialog with component versions and update actions."""

    if not gui_available():
        info_dialog(_format_status_text(), title="ap-bizhelper utilities")
        return

    def _build(session, modules):  # type: ignore[no-untyped-def]
        settings = load_settings()
        padding_value = int(settings.get("KIVY_DIALOG_PADDING_DP", DIALOG_DEFAULTS["KIVY_DIALOG_PADDING_DP"]))
        spacing_value = int(settings.get("KIVY_DIALOG_SPACING_DP", DIALOG_DEFAULTS["KIVY_DIALOG_SPACING_DP"]))
        button_height_value = int(settings.get("KIVY_BUTTON_HEIGHT_DP", DIALOG_DEFAULTS["KIVY_BUTTON_HEIGHT_DP"]))
        row_height_value = int(settings.get("KIVY_LIST_ROW_HEIGHT_DP", DIALOG_DEFAULTS["KIVY_LIST_ROW_HEIGHT_DP"]))
        padding = modules.dp(padding_value)
        spacing = modules.dp(spacing_value)
        root = modules.BoxLayout(orientation="vertical", padding=padding, spacing=spacing)

        header = modules.Label(
            text=f"ap-bizhelper version: {_app_version()}",
            size_hint_y=None,
            height=modules.dp(row_height_value),
            bold=True,
        )
        root.add_widget(header)

        scroll = modules.ScrollView(size_hint=(1, 1))
        grid = modules.GridLayout(cols=6, size_hint_y=None, spacing=spacing)
        grid.bind(minimum_height=grid.setter("height"))

        def _add_header_cell(text: str) -> None:
            label = modules.Label(text=text, bold=True, size_hint_y=None, height=modules.dp(row_height_value))
            grid.add_widget(label)

        for label in (
            "Component",
            "Installed version",
            "Latest seen",
            "Skip version",
            "Source",
            "Actions",
        ):
            _add_header_cell(label)

        def _run_action(action: Callable[[], bool]) -> None:
            action()
            _refresh_table()

        def _refresh_table() -> None:
            grid.clear_widgets()
            for label in (
                "Component",
                "Installed version",
                "Latest seen",
                "Skip version",
                "Source",
                "Actions",
            ):
                _add_header_cell(label)

            rows = _build_component_rows()
            for row in rows:
                for value in (
                    row.name,
                    row.installed_version,
                    row.latest_seen,
                    row.skip_version,
                    row.source,
                ):
                    cell = modules.Label(text=value, size_hint_y=None, height=modules.dp(row_height_value))
                    grid.add_widget(cell)

                action_box = modules.BoxLayout(orientation="horizontal", spacing=spacing)
                force_button = modules.FocusableButton(text="Force update", size_hint_x=None, width=modules.dp(140))
                manual_button = modules.FocusableButton(text="Manual select", size_hint_x=None, width=modules.dp(140))
                session.focus_manager.register(force_button)
                session.focus_manager.register(manual_button)
                force_button.bind(on_release=lambda _btn, cb=row.force_update: _run_action(cb))
                manual_button.bind(on_release=lambda _btn, cb=row.manual_select: _run_action(cb))
                action_box.add_widget(force_button)
                action_box.add_widget(manual_button)
                grid.add_widget(action_box)

        _refresh_table()
        scroll.add_widget(grid)
        root.add_widget(scroll)

        def _show_update_app_placeholder() -> None:
            info_dialog(
                "The Update App feature is not implemented yet. "
                "This button is a placeholder for future update logic.",
                title="Update App (Placeholder)",
            )

        def _show_rollback_placeholder() -> None:
            info_dialog(
                "Rollback is planned but requires the snapshot system. "
                "Once snapshots are available, this action will restore the latest snapshot.",
                title="Rollback (Planned)",
            )

        def _confirm_reset_settings() -> None:
            response = question_dialog(
                title="Reset settings?",
                text="Reset settings to defaults? This will clear saved paths and preferences.",
                ok_label="Reset",
                cancel_label="Cancel",
            )
            if response != "ok":
                return
            _reset_settings()
            _refresh_table()

        def _copy_status() -> None:
            if not copy_to_clipboard(_format_status_text(), settings=settings):
                info_dialog("Clipboard is not available on this system.")

        button_rows = [
            [
                ("Copy status", _copy_status),
                ("Managed dirs", lambda: show_managed_dirs_dialog()),
                ("Update App", _show_update_app_placeholder),
                ("APWorlds…", lambda: show_apworlds_dialog()),
            ],
            [
                ("Import settings", lambda: _refresh_table() if _import_settings() else None),
                ("Export settings", _export_settings),
                ("Open exports", lambda: _open_path_in_manager(EXPORTS_DIR) if _ensure_exports_dir() else None),
            ],
            [
                ("Rollback", _show_rollback_placeholder),
                ("Reset settings", _confirm_reset_settings),
                ("Uninstall", show_uninstall_dialog),
            ],
        ]

        for row_buttons in button_rows:
            row_layout = modules.BoxLayout(
                orientation="horizontal",
                spacing=spacing,
                size_hint_y=None,
                height=modules.dp(button_height_value),
            )
            for label, handler in row_buttons:
                btn = modules.FocusableButton(text=label)
                session.focus_manager.register(btn)
                btn.bind(on_release=lambda _btn, h=handler: h())
                row_layout.add_widget(btn)
            root.add_widget(row_layout)

        close_row = modules.BoxLayout(
            orientation="horizontal",
            spacing=spacing,
            size_hint_y=None,
            height=modules.dp(button_height_value),
        )
        close_button = modules.FocusableButton(text="Close", size_hint_x=None, width=modules.dp(160))
        session.focus_manager.register(close_button, default=True)
        close_button.bind(on_release=lambda *_args: session.close(None))
        close_row.add_widget(close_button)
        root.add_widget(close_row)
        return root

    run_custom_dialog(title="ap-bizhelper utilities", build=_build)


def show_uninstall_dialog() -> None:
    settings = load_settings()
    _uninstall_app(stored_appimage_path=_stored_appimage_path(settings))


def uninstall_core() -> None:
    settings = load_settings()
    _uninstall_app(
        stored_appimage_path=_stored_appimage_path(settings),
        selected=UNINSTALL_DEFAULT_SELECTIONS,
    )


def uninstall_all() -> None:
    settings = load_settings()
    _uninstall_app(
        stored_appimage_path=_stored_appimage_path(settings),
        selected=UNINSTALL_ALL_SELECTIONS,
    )


def _resolve_bizhelper_appimage(settings: dict, *, action: str) -> Optional[Path]:
    appimage_value = str(settings.get(BIZHELPER_APPIMAGE_KEY) or "")
    if not appimage_value:
        error_dialog(
            "The ap-bizhelper AppImage path is missing from settings.\n\n"
            f"Unable to {action}."
        )
        return None

    appimage_path = Path(appimage_value)
    if not appimage_path.is_file():
        error_dialog(
            "The ap-bizhelper AppImage could not be found.\n\n"
            f"Path: {appimage_path}\n\nUnable to {action}."
        )
        return None

    return appimage_path


def _remove_local_action_scripts() -> None:
    for filename in LOCAL_ACTION_SCRIPTS:
        target = LAUNCHER_DATA_DIR / filename
        try:
            if target.exists() or target.is_symlink():
                target.unlink()
        except Exception:
            continue


def _write_local_action_script(path: Path, appimage_path: Path, args: tuple[str, ...]) -> None:
    quoted_args = " ".join(shlex.quote(arg) for arg in args)
    quoted_appimage = shlex.quote(str(appimage_path))
    content = (
        "#!/usr/bin/env bash\n"
        f"APPIMAGE_PATH={quoted_appimage}\n"
        "if [ ! -f \"$APPIMAGE_PATH\" ]; then\n"
        "  if command -v zenity >/dev/null 2>&1; then\n"
        "    zenity --error --text=\"ap-bizhelper AppImage not found:\\n$APPIMAGE_PATH\"\n"
        "  elif command -v kdialog >/dev/null 2>&1; then\n"
        "    kdialog --error \"ap-bizhelper AppImage not found:\\n$APPIMAGE_PATH\"\n"
        "  else\n"
        "    echo \"ap-bizhelper AppImage not found: $APPIMAGE_PATH\" >&2\n"
        "  fi\n"
        "  exit 1\n"
        "fi\n"
        f"exec \"$APPIMAGE_PATH\" --nosteam {quoted_args}\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def ensure_local_action_scripts(settings: dict) -> None:
    appimage_path = _resolve_bizhelper_appimage(settings, action="create local action scripts")
    if not appimage_path:
        _remove_local_action_scripts()
        settings.pop(LOCAL_ACTIONS_CREATED_KEY, None)
        save_settings(settings)
        return

    logger = get_app_logger()
    created = []
    for filename, args in LOCAL_ACTION_SCRIPTS.items():
        target = LAUNCHER_DATA_DIR / filename
        try:
            _write_local_action_script(target, appimage_path, args)
            created.append(str(target))
        except Exception as exc:
            logger.log(
                f"Failed to create local action script {target}: {exc}",
                level="WARNING",
                location="local-actions",
                include_context=True,
                mirror_console=True,
            )

    if len(created) == len(LOCAL_ACTION_SCRIPTS):
        settings[LOCAL_ACTIONS_CREATED_KEY] = True
        save_settings(settings)
