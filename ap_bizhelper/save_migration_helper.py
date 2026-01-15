#!/usr/bin/env python3
from __future__ import annotations

import os
import signal
import shutil
import sys
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from ap_bizhelper.logging_utils import create_component_logger

CONFLICTS_DIRNAME = ".conflicts"
MIGRATION_LOG_SUBDIR = "saveram"
SAVE_RAM_DIRNAME = "SaveRAM"
TIME_WINDOW_SECONDS = 300
PID_TERM_WAIT_SECONDS = 2.0
PID_KILL_WAIT_SECONDS = 2.0
PID_POLL_INTERVAL_SECONDS = 0.1

HELPER_LOGGER = create_component_logger("save-migration", subdir=MIGRATION_LOG_SUBDIR)


def _prepend_sys_path(path: Path) -> bool:
    if not path.is_dir():
        return False
    path_str = path.as_posix()
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
        return True
    return False


def _normalize_env_paths(value: str) -> list[str]:
    return [path for path in value.split(os.pathsep) if path]


def _build_staged_env_path(
    key: str,
    staged: list[Path],
    blocked_exact: set[str],
    blocked_prefixes: tuple[str, ...],
) -> dict[str, object]:
    staged_values = [path.as_posix() for path in staged if path.is_dir()]
    existing_values = _normalize_env_paths(os.environ.get(key, ""))
    removed_values: list[str] = []
    filtered_existing: list[str] = []
    for path in existing_values:
        if path in blocked_exact or path.startswith(blocked_prefixes):
            removed_values.append(path)
        else:
            filtered_existing.append(path)
    seen: set[str] = set()
    merged_values: list[str] = []
    for path in staged_values + filtered_existing:
        if path in seen:
            continue
        merged_values.append(path)
        seen.add(path)
    os.environ[key] = os.pathsep.join(merged_values)
    return {
        "key": key,
        "value": os.environ.get(key, ""),
        "removed": removed_values,
        "staged": staged_values,
    }


def _stage_pyside6_paths() -> dict[str, object]:
    from ap_bizhelper.constants import (
        BIZHAWK_HELPERS_APPIMAGE_DIRNAME,
        BIZHAWK_HELPERS_LIB_DIRNAME,
    )

    helpers_root = Path(__file__).resolve().parent
    helpers_lib = helpers_root / BIZHAWK_HELPERS_LIB_DIRNAME
    helpers_appimage = helpers_lib / BIZHAWK_HELPERS_APPIMAGE_DIRNAME
    added_sys_paths: list[str] = []
    if _prepend_sys_path(helpers_lib):
        added_sys_paths.append(helpers_lib.as_posix())
    for site_packages in helpers_appimage.glob("usr/lib/python*/site-packages"):
        if _prepend_sys_path(site_packages):
            added_sys_paths.append(site_packages.as_posix())
    ld_library_info = _build_staged_env_path(
        "LD_LIBRARY_PATH",
        [helpers_appimage / "usr/lib"],
        blocked_exact={"/usr/lib", "/usr/lib64", "/lib", "/lib64"},
        blocked_prefixes=(
            "/usr/lib/qt",
            "/usr/lib64/qt",
            "/lib/qt",
            "/lib64/qt",
        ),
    )
    qt_plugin_info = _build_staged_env_path(
        "QT_PLUGIN_PATH",
        [
            helpers_appimage / "usr/lib/qt6/plugins",
            helpers_appimage / "usr/lib/qt/plugins",
        ],
        blocked_exact=set(),
        blocked_prefixes=(
            "/usr/lib/qt",
            "/usr/lib64/qt",
            "/lib/qt",
            "/lib64/qt",
        ),
    )

    HELPER_LOGGER.log(
        (
            "Staged PySide6 helper paths from "
            f"{helpers_appimage.resolve().as_posix()}."
        ),
        include_context=True,
        location="pyside6-stage",
    )
    HELPER_LOGGER.log(
        f"Inserted sys.path entries: {added_sys_paths}",
        include_context=True,
        location="pyside6-stage",
    )
    HELPER_LOGGER.log(
        (
            "Environment after staging: "
            f"LD_LIBRARY_PATH={ld_library_info['value']} "
            f"QT_PLUGIN_PATH={qt_plugin_info['value']}"
        ),
        include_context=True,
        location="pyside6-stage",
    )
    HELPER_LOGGER.log(
        (
            "System Qt paths removed or overridden: "
            f"LD_LIBRARY_PATH={ld_library_info['removed']} "
            f"QT_PLUGIN_PATH={qt_plugin_info['removed']}"
        ),
        include_context=True,
        location="pyside6-stage",
    )
    return {
        "helpers_root": helpers_root.as_posix(),
        "helpers_lib": helpers_lib.as_posix(),
        "helpers_appimage": helpers_appimage.as_posix(),
        "sys_paths": added_sys_paths,
        "ld_library_path": ld_library_info["value"],
        "qt_plugin_path": qt_plugin_info["value"],
    }


def _ensure_pyside6_available(stage_info: dict[str, object]) -> None:
    try:
        import PySide6
        from PySide6 import QtCore
    except Exception as exc:
        raise RuntimeError(
            "Failed to import PySide6 after staging helper paths. "
            f"Staging info: {stage_info}"
        ) from exc

    HELPER_LOGGER.log(
        f"PySide6 loaded from {getattr(PySide6, '__file__', 'unknown')}.",
        include_context=True,
        location="pyside6-stage",
    )
    HELPER_LOGGER.log(
        f"QtCore.qVersion() reported {QtCore.qVersion()}.",
        include_context=True,
        location="pyside6-stage",
    )


_STAGE_INFO = _stage_pyside6_paths()
_ensure_pyside6_available(_STAGE_INFO)

from ap_bizhelper.ap_bizhelper_config import get_path_setting, load_settings
from ap_bizhelper.constants import (
    BIZHAWK_EXE_KEY,
    BIZHAWK_INSTALL_DIR_KEY,
    BIZHAWK_LAST_LAUNCH_ARGS_KEY,
    BIZHAWK_LAST_PID_KEY,
    BIZHAWK_RUNNER_KEY,
    BIZHAWK_SAVERAM_DIR_KEY,
)
from ap_bizhelper.dialogs import (
    ensure_qt_app,
    ensure_qt_available,
    error_dialog,
    question_dialog,
)


def _bizhawk_root(settings: dict) -> Optional[Path]:
    install_root = str(settings.get(BIZHAWK_INSTALL_DIR_KEY, "") or "")
    if install_root:
        candidate = Path(install_root)
        if candidate.is_dir():
            return candidate

    exe_str = str(settings.get(BIZHAWK_EXE_KEY, "") or "")
    if exe_str:
        exe_path = Path(exe_str)
        if exe_path.is_file():
            return exe_path.parent
    return None


def _save_root(settings: dict) -> Path:
    return get_path_setting(settings, BIZHAWK_SAVERAM_DIR_KEY)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _wait_for_pid_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(PID_POLL_INTERVAL_SECONDS)
    return not _pid_alive(pid)


def _terminate_pid(pid: int) -> None:
    if not _pid_alive(pid):
        return
    HELPER_LOGGER.log(
        f"Sending SIGTERM to EmuHawk pid={pid}.",
        include_context=True,
        location="bizhawk-close",
    )
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return

    if _wait_for_pid_exit(pid, PID_TERM_WAIT_SECONDS):
        HELPER_LOGGER.log(
            f"EmuHawk pid={pid} terminated after SIGTERM.",
            include_context=True,
            location="bizhawk-close",
        )
        return

    HELPER_LOGGER.log(
        f"EmuHawk pid={pid} still alive; sending SIGKILL.",
        include_context=True,
        location="bizhawk-close",
    )
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError as exc:
        raise RuntimeError(f"Failed to SIGKILL EmuHawk pid={pid}: {exc}") from exc

    if _wait_for_pid_exit(pid, PID_KILL_WAIT_SECONDS):
        HELPER_LOGGER.log(
            f"EmuHawk pid={pid} terminated after SIGKILL.",
            include_context=True,
            location="bizhawk-close",
        )
        return

    raise RuntimeError(f"EmuHawk pid={pid} refused to exit after SIGKILL.")


def _scan_bizhawk_pids(bizhawk_root: Path) -> set[int]:
    pids: set[int] = set()
    try:
        output = subprocess.check_output(
            ["pgrep", "-f", str(bizhawk_root / "EmuHawkMono.sh")],
            text=True,
        )
    except Exception:
        return pids
    for line in output.splitlines():
        try:
            pids.add(int(line.strip()))
        except ValueError:
            continue
    return pids


def _ensure_bizhawk_closed(
    settings: dict, bizhawk_root: Path, target_pid: Optional[int]
) -> None:
    if target_pid:
        _terminate_pid(target_pid)

    while True:
        pids: set[int] = set()
        last_pid = str(settings.get(BIZHAWK_LAST_PID_KEY, "") or "")
        if last_pid.isdigit():
            pid = int(last_pid)
            if _pid_alive(pid):
                pids.add(pid)
        if target_pid and _pid_alive(target_pid):
            pids.add(target_pid)
        pids.update(_scan_bizhawk_pids(bizhawk_root))
        if not pids:
            return

        HELPER_LOGGER.log(
            f"EmuHawk running; migration blocked. PIDs={sorted(pids)}",
            include_context=True,
            location="bizhawk-close",
        )

        ensure_qt_available()
        ensure_qt_app()
        choice = question_dialog(
            title="EmuHawk running",
            text="EmuHawk is running. Migration cannot proceed while EmuHawk is open.",
            ok_label="Try again",
            cancel_label="Cancel",
        )

        if choice == "ok":
            HELPER_LOGGER.log(
                "User chose to try BizHawk wait again.",
                include_context=True,
                location="bizhawk-close",
            )
            continue

        HELPER_LOGGER.log(
            "User cancelled SaveRAM migration because EmuHawk is still running.",
            include_context=True,
            location="bizhawk-close",
        )
        raise RuntimeError("SaveRAM migration cancelled while EmuHawk was running.")


def _conflict_root(base: Path, rel_path: Path) -> Path:
    return base / rel_path.parent


def _backup_conflict(conflict_root: Path, label: str, source: Path) -> None:
    conflict_root.mkdir(parents=True, exist_ok=True)
    target = conflict_root / f"{source.name}.{label}"
    shutil.copy2(source, target)
    HELPER_LOGGER.log(
        f"Backed up {source} to {target}",
        include_context=True,
        location="conflict",
    )


def _choose_conflict(
    *,
    canonical_path: Path,
    local_path: Path,
    older_path: Path,
    newer_path: Path,
) -> Path:
    older_mtime = older_path.stat().st_mtime
    newer_mtime = newer_path.stat().st_mtime
    if abs(newer_mtime - older_mtime) <= TIME_WINDOW_SECONDS:
        HELPER_LOGGER.log(
            f"5-minute rule applied; choosing older file {older_path}",
            include_context=True,
            location="conflict",
        )
        return older_path

    ensure_qt_available()
    ensure_qt_app()

    prompt = (
        "SaveRAM conflict detected.\n\n"
        f"Canonical: {canonical_path}\n"
        f"Local: {local_path}\n\n"
        f"Older: {older_path}\n"
        f"Newer: {newer_path}\n\n"
        "Choose which file should become the canonical save.\n"
        "The non-selected file will remain backed up."
    )

    choice = question_dialog(
        title="SaveRAM conflict",
        text=prompt,
        ok_label="Use older",
        cancel_label="Cancel",
        extra_label="Use newer",
    )

    if choice == "ok":
        HELPER_LOGGER.log(
            f"User chose older file {older_path}",
            include_context=True,
            location="conflict",
        )
        return older_path
    if choice == "extra":
        HELPER_LOGGER.log(
            f"User chose newer file {newer_path}",
            include_context=True,
            location="conflict",
        )
        return newer_path

    raise RuntimeError("User cancelled SaveRAM conflict resolution.")


def _merge_file(
    *,
    source: Path,
    dest: Path,
    conflict_root: Path,
    canonical_path: Path,
) -> None:
    if dest.exists() and dest.is_dir():
        raise RuntimeError(f"Conflict destination is a directory: {dest}")
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(dest))
        HELPER_LOGGER.log(
            f"Moved new file into canonical SaveRAM: {dest}",
            include_context=True,
            location="merge",
        )
        return

    _backup_conflict(conflict_root, "canonical", dest)
    _backup_conflict(conflict_root, "local", source)

    src_mtime = source.stat().st_mtime
    dest_mtime = dest.stat().st_mtime
    older = source if src_mtime <= dest_mtime else dest
    newer = dest if older is source else source

    chosen = _choose_conflict(
        canonical_path=canonical_path,
        local_path=source,
        older_path=older,
        newer_path=newer,
    )

    if chosen is source:
        shutil.copy2(source, dest)
        HELPER_LOGGER.log(
            f"Conflict resolved: chose local file for {dest}",
            include_context=True,
            location="merge",
        )
    else:
        HELPER_LOGGER.log(
            f"Conflict resolved: kept canonical file for {dest}",
            include_context=True,
            location="merge",
        )


def _migrate_saveram_dir(source_dir: Path, canonical_dir: Path) -> None:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    conflict_base = canonical_dir / CONFLICTS_DIRNAME / timestamp
    for item in source_dir.rglob("*"):
        if item.is_dir():
            continue
        rel = item.relative_to(source_dir)
        dest = canonical_dir / rel
        conflict_root = _conflict_root(conflict_base, rel)
        _merge_file(
            source=item,
            dest=dest,
            conflict_root=conflict_root,
            canonical_path=dest,
        )


def _ensure_symlink(target: Path, link: Path) -> None:
    if link.exists() or link.is_symlink():
        try:
            if link.is_dir() and not link.is_symlink():
                shutil.rmtree(link)
            else:
                link.unlink()
        except Exception as exc:
            raise RuntimeError(f"Failed to remove existing SaveRAM path: {exc}")
    os.symlink(target, link)


def _migrate_system_dir(system_dir_name: str, *, settings: dict) -> None:
    bizhawk_root = _bizhawk_root(settings)
    if not bizhawk_root:
        raise RuntimeError("BizHawk root directory not configured.")

    save_root = _save_root(settings)
    canonical_dir = save_root / system_dir_name
    bizhawk_system_dir = bizhawk_root / system_dir_name
    save_ram_path = bizhawk_system_dir / SAVE_RAM_DIRNAME

    canonical_dir.mkdir(parents=True, exist_ok=True)
    bizhawk_system_dir.mkdir(parents=True, exist_ok=True)

    if save_ram_path.is_symlink():
        try:
            resolved = save_ram_path.resolve()
        except FileNotFoundError:
            resolved = None
        if resolved and resolved == canonical_dir:
            HELPER_LOGGER.log(
                f"SaveRAM symlink already valid for {system_dir_name}.",
                include_context=True,
                location="migration",
            )
            return

        HELPER_LOGGER.log(
            f"Repairing SaveRAM symlink for {system_dir_name}.",
            include_context=True,
            location="migration",
        )
        _ensure_symlink(canonical_dir, save_ram_path)
        return

    if not save_ram_path.exists():
        HELPER_LOGGER.log(
            f"SaveRAM missing for {system_dir_name}; creating symlink.",
            include_context=True,
            location="migration",
        )
        _ensure_symlink(canonical_dir, save_ram_path)
        return

    if save_ram_path.is_dir():
        HELPER_LOGGER.log(
            f"Migrating local SaveRAM directory for {system_dir_name}.",
            include_context=True,
            location="migration",
        )
        _migrate_saveram_dir(save_ram_path, canonical_dir)
        shutil.rmtree(save_ram_path)
        _ensure_symlink(canonical_dir, save_ram_path)
        return

    raise RuntimeError(f"Unexpected SaveRAM path type: {save_ram_path}")


def _derive_system_dirs(save_root: Path, bizhawk_root: Path) -> list[str]:
    candidates = set()
    if save_root.is_dir():
        for entry in save_root.iterdir():
            if entry.is_dir():
                candidates.add(entry.name)
    if bizhawk_root.is_dir():
        for entry in bizhawk_root.iterdir():
            if not entry.is_dir():
                continue
            save_ram = entry / SAVE_RAM_DIRNAME
            if save_ram.exists() or save_ram.is_symlink():
                candidates.add(entry.name)
    return sorted(candidates)


def _relaunch_bizhawk(settings: dict) -> None:
    runner = Path(str(settings.get(BIZHAWK_RUNNER_KEY, "") or ""))
    args = settings.get(BIZHAWK_LAST_LAUNCH_ARGS_KEY, [])
    if not runner.is_file():
        raise RuntimeError("BizHawk runner not configured; cannot relaunch.")
    if not isinstance(args, list) or not args:
        raise RuntimeError("No cached BizHawk launch args; cannot relaunch.")

    HELPER_LOGGER.log(
        f"Relaunching BizHawk via {runner} {args}",
        include_context=True,
        location="relaunch",
    )
    subprocess.Popen([str(runner), *[str(arg) for arg in args]])


def main(argv: list[str]) -> int:
    with HELPER_LOGGER.context("main"):
        HELPER_LOGGER.log(
            "Save migration helper starting.",
            include_context=True,
            location="startup",
        )
        settings = load_settings()
        system_dir = argv[1] if len(argv) > 1 else None
        target_pid: Optional[int] = None
        if len(argv) > 2:
            try:
                candidate = int(argv[2])
                if candidate <= 0:
                    raise ValueError("PID must be positive")
                target_pid = candidate
            except ValueError as exc:
                raise RuntimeError(f"Invalid EmuHawk pid argument: {argv[2]}") from exc
            HELPER_LOGGER.log(
                f"Received EmuHawk pid argument: {target_pid}",
                include_context=True,
                location="startup",
            )
        if system_dir:
            HELPER_LOGGER.log(
                f"Running targeted migration for system dir: {system_dir}",
                include_context=True,
                location="startup",
            )
        else:
            HELPER_LOGGER.log(
                "Running migration scan for all system directories.",
                include_context=True,
                location="startup",
            )

        try:
            if system_dir:
                bizhawk_root = _bizhawk_root(settings)
                if not bizhawk_root:
                    raise RuntimeError("BizHawk root directory not configured.")
                _ensure_bizhawk_closed(settings, bizhawk_root, target_pid)
                _migrate_system_dir(system_dir, settings=settings)
                _relaunch_bizhawk(settings)
            else:
                bizhawk_root = _bizhawk_root(settings)
                if not bizhawk_root:
                    raise RuntimeError("BizHawk root directory not configured.")
                save_root = _save_root(settings)
                system_dirs = _derive_system_dirs(save_root, bizhawk_root)
                if not system_dirs:
                    HELPER_LOGGER.log(
                        "No system directories found for SaveRAM repair.",
                        include_context=True,
                        location="migration",
                    )
                    return 0
                HELPER_LOGGER.log(
                    f"Discovered system directories for SaveRAM repair: {system_dirs}",
                    include_context=True,
                    location="migration",
                )
                for name in system_dirs:
                    _migrate_system_dir(name, settings=settings)
        except Exception as exc:
            message = f"SaveRAM migration failed: {exc}"
            HELPER_LOGGER.log(message, level="ERROR", include_context=True)
            error_dialog(message)
            return 1

        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
