#!/usr/bin/env python3

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ap_bizhelper_ap import ensure_appimage
from ap_bizhelper_bizhawk import ensure_bizhawk_and_proton
from ap_bizhelper_config import get_ext_behavior, load_settings, save_settings, set_ext_behavior
from ap_bizhelper_sni import download_sni_if_needed


def _has_zenity() -> bool:
    return shutil.which("zenity") is not None


def _run_zenity(args: list[str]) -> tuple[int, str]:
    if not _has_zenity():
        return 127, ""
    try:
        proc = subprocess.Popen(
            ["zenity", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out, _ = proc.communicate()
        return proc.returncode, out.strip()
    except FileNotFoundError:
        return 127, ""


def _ensure_sni(settings: dict) -> None:
    exe_str = str(settings.get("BIZHAWK_EXE", "") or "")
    if not exe_str:
        return
    exe_path = Path(exe_str)
    if not exe_path.is_file():
        return
    download_sni_if_needed(exe_path.parent)


def _select_patch_file() -> Path:
    if _has_zenity():
        code, out = _run_zenity(["--file-selection", "--title=Select Archipelago patch file", f"--filename={Path.home()}/"])
        if code != 0 or not out:
            raise RuntimeError("User cancelled patch selection")
        candidate = Path(out)
    else:
        raise RuntimeError("zenity is required to select a patch file")

    if not candidate.is_file():
        raise RuntimeError("Selected patch file does not exist")
    return candidate


def _ensure_apworld_for_extension(ext_lc: str) -> None:
    behavior = get_ext_behavior(ext_lc)
    if behavior:
        return
    if not _has_zenity():
        print(f"[ap-bizhelper] zenity not available; skipping APWorld prompt for .{ext_lc}.")
        return

    worlds_dir = Path.home() / ".local" / "share" / "Archipelago" / "worlds"
    question = (
        f"This looks like a new Archipelago patch extension (.{ext_lc}).\n\n"
        "If this game requires an external .apworld file and it isn't already installed, you can select it now to copy into:\n"
        f"{worlds_dir}\n\nDo you want to select a .apworld file for this extension now?"
    )
    code, _ = _run_zenity(
        [
            "--question",
            f"--title=APWorld for .{ext_lc}",
            f"--text={question}",
            "--ok-label=Select .apworld",
            "--cancel-label=Skip",
        ]
    )
    if code != 0:
        return

    code, apworld_path = _run_zenity(
        [
            "--file-selection",
            f"--title=Select .apworld file for .{ext_lc}",
            "--file-filter=*.apworld",
            f"--filename={Path.home()}/",
        ]
    )
    if code != 0 or not apworld_path:
        return

    src = Path(apworld_path)
    if not src.is_file():
        return

    try:
        worlds_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, worlds_dir / src.name)
        print(f"[ap-bizhelper] Copied {src} to {worlds_dir}")
    except Exception as exc:  # pragma: no cover - best effort UX
        print(f"[ap-bizhelper] Failed to copy .apworld: {exc}")


def _run_archipelago(appimage: Path, patch: Path) -> None:
    if not appimage.is_file() or not os.access(str(appimage), os.X_OK):
        raise RuntimeError("Archipelago AppImage is not configured correctly")
    subprocess.Popen([str(appimage), str(patch)])


def _current_bizhawk_pids() -> set[str]:
    try:
        out = subprocess.check_output(["pgrep", "-f", "EmuHawk.exe"], text=True)
    except subprocess.CalledProcessError:
        return set()
    return {pid for pid in out.strip().splitlines() if pid}


def _is_new_bizhawk_running(baseline: set[str]) -> bool:
    current = _current_bizhawk_pids()
    return any(pid not in baseline for pid in current)


def _find_rom_for_patch(patch: Path) -> Path | None:
    stem = patch.stem
    for candidate in patch.parent.glob(f"{stem}.*"):
        if candidate.resolve() == patch.resolve():
            continue
        if candidate.is_file():
            return candidate
    return None


def _wait_for_rom(patch: Path, timeout: int = 60) -> Path | None:
    for _ in range(timeout):
        rom = _find_rom_for_patch(patch)
        if rom is not None:
            print(f"[ap-bizhelper] ROM detected: {rom}")
            return rom
        time.sleep(1)
    print("[ap-bizhelper] Timed out waiting for ROM; not launching BizHawk.")
    return None


def _launch_bizhawk_for_rom(rom: Path, settings: dict) -> None:
    runner_str = str(settings.get("BIZHAWK_RUNNER", "") or "")
    if not runner_str:
        print("[ap-bizhelper] BizHawk runner not configured; skipping auto-launch.")
        return
    runner = Path(runner_str)
    if not runner.is_file():
        print("[ap-bizhelper] BizHawk runner path is missing; skipping auto-launch.")
        return

    for key in ["BIZHAWK_EXE", "PROTON_BIN", "PROTON_PREFIX", "SFC_LUA_PATH"]:
        value = settings.get(key)
        if value:
            os.environ[key] = str(value)

    print(f"[ap-bizhelper] Launching BizHawk runner for ROM: {rom}")
    subprocess.Popen([str(runner), str(rom)])


def _handle_bizhawk_for_patch(patch: Path, baseline_pids: set[str], settings: dict) -> None:
    rom = _wait_for_rom(patch)
    if rom is None:
        return

    ext_lc = patch.suffix.lstrip(".").lower()
    behavior = get_ext_behavior(ext_lc)
    print(f"[ap-bizhelper] Saved behavior for .{ext_lc}: {behavior or '<none>'}")

    if behavior == "auto":
        print("[ap-bizhelper] Behavior 'auto': not launching BizHawk; assuming AP/user handles it.")
        return
    if behavior == "fallback":
        print("[ap-bizhelper] Behavior 'fallback': launching BizHawk via Proton.")
        _launch_bizhawk_for_rom(rom, settings)
        return
    if behavior not in (None, ""):
        print(f"[ap-bizhelper] Unknown behavior '{behavior}' for .{ext_lc}; doing nothing for safety.")
        return

    print(f"[ap-bizhelper] No behavior stored for .{ext_lc} yet; entering learning mode.")
    waited = 0
    timeout = 10
    while waited < timeout:
        if _is_new_bizhawk_running(baseline_pids):
            print(f"[ap-bizhelper] Detected new BizHawk instance; recording .{ext_lc} as 'auto'.")
            set_ext_behavior(ext_lc, "auto")
            return
        time.sleep(1)
        waited += 1

    print(
        f"[ap-bizhelper] No BizHawk detected for .{ext_lc} after {timeout}s; switching this extension to 'fallback' and launching BizHawk."
    )
    set_ext_behavior(ext_lc, "fallback")
    _launch_bizhawk_for_rom(rom, settings)


def _ensure_all() -> tuple[Path, dict] | tuple[None, None]:
    try:
        appimage = ensure_appimage()
    except RuntimeError:
        return None, None

    bizhawk_result = ensure_bizhawk_and_proton()
    if bizhawk_result is None:
        return None, None

    settings = load_settings()
    _ensure_sni(settings)
    save_settings(settings)
    return appimage, settings


def _run_full_flow() -> int:
    appimage, settings = _ensure_all()
    if appimage is None or settings is None:
        return 1

    baseline = _current_bizhawk_pids()

    try:
        patch = _select_patch_file()
    except RuntimeError as exc:
        print(f"[ap-bizhelper] {exc}")
        return 1

    ext_lc = patch.suffix.lstrip(".").lower()
    _ensure_apworld_for_extension(ext_lc)

    try:
        _run_archipelago(appimage, patch)
    except RuntimeError as exc:
        print(f"[ap-bizhelper] {exc}")
        return 1

    _handle_bizhawk_for_patch(patch, baseline, settings)
    return 0


def _print_usage() -> None:
    print("Usage: ap_bizhelper.py [ensure]")
    print("  ensure : only perform Archipelago/BizHawk/SNI setup")
    print("  (no args): run full flow matching the legacy launcher")


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        if argv[1] == "ensure":
            appimage, _ = _ensure_all()
            return 0 if appimage is not None else 1
        _print_usage()
        return 1

    return _run_full_flow()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
