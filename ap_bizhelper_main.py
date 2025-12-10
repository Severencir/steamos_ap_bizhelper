"""Python entrypoint that mirrors the legacy ap-bizhelper.sh behaviour."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ap_bizhelper_ap import ensure_appimage
from ap_bizhelper_bizhawk import ensure_bizhawk_and_proton
from ap_bizhelper_lua import ensure_bizhawk_skip_admin_warning, ensure_sfc_lua_path, ensure_sni_prepared
from ap_bizhelper_ui import error_dialog, file_selection, has_zenity, info_dialog, pgrep

CONFIG_DIR = Path(os.path.expanduser("~/.config/ap_bizhelper_test"))
SETTINGS_FILE = CONFIG_DIR / "settings.json"
EXT_BEHAVIOR_FILE = CONFIG_DIR / "ext_behavior.json"


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def get_ext_behavior(ext: str) -> str:
    behaviors = _load_json(EXT_BEHAVIOR_FILE)
    return str(behaviors.get(ext.lower(), ""))


def set_ext_behavior(ext: str, value: str) -> None:
    behaviors = _load_json(EXT_BEHAVIOR_FILE)
    behaviors[ext.lower()] = value
    _save_json(EXT_BEHAVIOR_FILE, behaviors)


def select_patch_file() -> Path:
    if not has_zenity():
        raise SystemExit("zenity is required to select a patch file")

    selected = file_selection("Select Archipelago patch file", initial=Path.home())
    if selected is None:
        raise SystemExit(0)
    return selected


def find_candidate_rom(patch: Path, *, timeout: int = 60) -> Path | None:
    stem = patch.stem
    patch_ext = patch.suffix.lower()
    directory = patch.parent
    waited = 0
    while waited < timeout:
        candidates = sorted(
            [p for p in directory.glob(f"{stem}.*") if p.suffix.lower() != patch_ext and p.is_file()]
        )
        if candidates:
            return candidates[0]
        time.sleep(1)
        waited += 1
    return None


def is_new_bizhawk_running(baseline: list[str]) -> bool:
    current = list(pgrep("EmuHawk.exe"))
    if not current:
        return False
    for pid in current:
        if pid not in baseline:
            return True
    return False


def launch_bizhawk(runner: Path, rom: Path, bizhawk_exe: Path) -> None:
    if rom.suffix.lower() == ".sfc":
        ensure_sfc_lua_path(bizhawk_exe)
    ensure_bizhawk_skip_admin_warning(bizhawk_exe)
    os.spawnl(os.P_NOWAIT, str(runner), str(runner), str(rom))


def handle_bizhawk_for_patch(patch: Path, runner: Path, bizhawk_exe: Path, baseline_pids: list[str]) -> None:
    rom = find_candidate_rom(patch)
    if rom is None:
        info_dialog("[ap-bizhelper] Timed out waiting for ROM; not launching BizHawk.")
        return

    ext = patch.suffix.lower().lstrip(".")
    behavior = get_ext_behavior(ext)

    if behavior == "auto":
        info_dialog("[ap-bizhelper] Behavior 'auto': not launching BizHawk; assuming AP/user handles it.")
        return
    if behavior == "fallback":
        info_dialog("[ap-bizhelper] Behavior 'fallback': launching BizHawk via Proton.")
        launch_bizhawk(runner, rom, bizhawk_exe)
        return
    if behavior not in ("", None):
        info_dialog(f"[ap-bizhelper] Unknown behavior '{behavior}'; skipping auto-launch for safety.")
        return

    # Learning mode
    waited = 0
    timeout = 10
    while waited < timeout:
        if is_new_bizhawk_running(baseline_pids):
            set_ext_behavior(ext, "auto")
            info_dialog(f"[ap-bizhelper] Detected new BizHawk instance; recording .{ext} as 'auto'.")
            return
        time.sleep(1)
        waited += 1

    info_dialog(
        f"[ap-bizhelper] No BizHawk detected for .{ext} after {timeout}s; switching this extension to 'fallback' and launching BizHawk."
    )
    set_ext_behavior(ext, "fallback")
    launch_bizhawk(runner, rom, bizhawk_exe)


def main() -> None:
    appimage = ensure_appimage()
    runner = ensure_bizhawk_and_proton()
    if runner is None:
        error_dialog("BizHawk runner not configured; exiting.")
        raise SystemExit(1)

    settings = _load_json(SETTINGS_FILE)
    exe_str = settings.get("BIZHAWK_EXE") or ""
    if not exe_str:
        error_dialog("BIZHAWK_EXE missing from settings after setup.")
        raise SystemExit(1)
    bizhawk_exe = Path(exe_str)

    ensure_sni_prepared()

    baseline = list(pgrep("EmuHawk.exe"))

    patch = select_patch_file()
    info_dialog(f"[ap-bizhelper] Launching Archipelago with patch: {patch}")
    os.spawnl(os.P_NOWAIT, str(appimage), str(appimage), str(patch))

    handle_bizhawk_for_patch(patch, runner, bizhawk_exe, baseline)


if __name__ == "__main__":  # pragma: no cover
    main()

