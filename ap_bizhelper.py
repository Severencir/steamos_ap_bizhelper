#!/usr/bin/env python3

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, Optional, Set, Tuple

from ap_bizhelper_ap import (
    AP_APPIMAGE_DEFAULT,
    _has_zenity,
    _run_zenity,
    ensure_appimage,
    error_dialog,
    info_dialog,
)
from ap_bizhelper_bizhawk import ensure_bizhawk_and_proton
from ap_bizhelper_config import get_ext_behavior, load_settings, save_settings, set_ext_behavior
from ap_bizhelper_worlds import ensure_apworld_for_patch


def _select_patch_file() -> Path:
    if not _has_zenity():
        raise RuntimeError("zenity is required to choose an Archipelago patch file.")

    code, out = _run_zenity(
        [
            "--file-selection",
            "--title=Select Archipelago patch file",
            f"--filename={Path.home()}/",
        ]
    )
    if code != 0 or not out:
        raise RuntimeError("User cancelled patch selection.")

    patch = Path(out)
    if not patch.is_file():
        raise RuntimeError("Selected patch file does not exist.")

    return patch


def _needs_archipelago_download(settings: dict) -> bool:
    app_path_str = str(settings.get("AP_APPIMAGE", "") or "")
    app_path = Path(app_path_str) if app_path_str else None

    if app_path and app_path.is_file() and os.access(str(app_path), os.X_OK):
        return False

    if AP_APPIMAGE_DEFAULT.is_file() and os.access(str(AP_APPIMAGE_DEFAULT), os.X_OK):
        return False

    return True


def _needs_bizhawk_download(settings: dict) -> bool:
    exe_str = str(settings.get("BIZHAWK_EXE", "") or "")
    runner_str = str(settings.get("BIZHAWK_RUNNER", "") or "")
    proton_str = str(settings.get("PROTON_BIN", "") or "")

    exe = Path(exe_str) if exe_str else None
    runner = Path(runner_str) if runner_str else None
    proton_bin = Path(proton_str) if proton_str else None

    return not (
        exe and exe.is_file() and runner and runner.is_file() and proton_bin and proton_bin.is_file()
    )


def _prompt_setup_choices(
    *,
    allow_archipelago_skip: bool,
    show_archipelago: bool,
    show_bizhawk: bool,
) -> Tuple[bool, bool, bool]:
    if not any((show_archipelago, show_bizhawk)):
        return False, False, False

    if not _has_zenity():
        # Fall back to enabling available options when zenity is unavailable.
        shortcuts = show_archipelago or show_bizhawk
        return show_archipelago, show_bizhawk, shortcuts

    while True:
        args = [
            "--list",
            "--checklist",
            "--title=Download setup",
            "--text=Select which components to download and configure.",
            "--column=Install",
            "--column=Component",
        ]

        if show_archipelago:
            args.extend(["TRUE", "Archipelago"])
        if show_bizhawk:
            args.extend(["TRUE", "BizHawk (with Proton)"])
        if show_archipelago or show_bizhawk:
            args.extend(["TRUE", "Create Desktop shortcuts (Archipelago & BizHawk)"])

        args.extend(["--ok-label=Download", "--cancel-label=Cancel", "--height=300"])

        code, out = _run_zenity(args)

        if code != 0:
            raise RuntimeError("User cancelled setup selection.")

        selections = [s.strip() for s in out.split("|") if s.strip()]
        arch = "Archipelago" in selections
        bizhawk = "BizHawk (with Proton)" in selections
        shortcuts = "Create Desktop shortcuts (Archipelago & BizHawk)" in selections

        return arch, bizhawk, shortcuts


def _ensure_apworld_for_extension(ext: str) -> None:
    ext = ext.strip().lower()
    if not ext:
        return

    # Only care about "new" extensions (no behavior stored yet)
    behavior = get_ext_behavior(ext)
    if behavior:
        return

    if not _has_zenity():
        print(f"[ap-bizhelper] zenity not available; skipping APWorld prompt for .{ext}.")
        return

    worlds_dir = Path.home() / ".local/share/Archipelago/worlds"
    text = (
        f"This looks like a new Archipelago patch extension (.{ext}).\n\n"
        "If this game requires an external .apworld file and it isn't already installed, "
        f"you can select it now to copy into:\n{worlds_dir}\n\n"
        "Do you want to select a .apworld file for this extension now?"
    )

    code, _ = _run_zenity(
        [
            "--question",
            f"--title=APWorld for .{ext}",
            f"--text={text}",
            "--ok-label=Select .apworld",
            "--cancel-label=Skip",
        ]
    )
    if code != 0:
        print(f"[ap-bizhelper] User skipped APWorld selection for .{ext}.")
        return

    code, apworld = _run_zenity(
        [
            "--file-selection",
            f"--title=Select .apworld file for .{ext}",
            "--file-filter=*.apworld",
            f"--filename={Path.home()}/",
        ]
    )
    if code != 0 or not apworld:
        return

    apworld_path = Path(apworld)
    if apworld_path.is_file():
        try:
            worlds_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(apworld_path, worlds_dir / apworld_path.name)
            info_dialog(f"Copied {apworld_path.name} to:\n{worlds_dir}")
        except Exception as exc:  # pragma: no cover - filesystem edge cases
            error_dialog(f"Failed to copy {apworld_path.name}: {exc}")
    else:
        error_dialog("Selected .apworld file does not exist.")


def _list_bizhawk_pids() -> Set[int]:
    try:
        proc = subprocess.run(
            ["pgrep", "-f", "EmuHawk.exe"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except FileNotFoundError:
        return set()

    pids = set()
    for line in proc.stdout.splitlines():
        try:
            pids.add(int(line.strip()))
        except ValueError:
            continue
    return pids


def _find_matching_rom(patch: Path) -> Optional[Path]:
    if patch.suffix.lower() == ".sfc" and patch.is_file():
        return patch

    base = patch.with_suffix("")

    if base != patch and base.is_file():
        return base

    pattern = f"{base.name}.*"
    candidates = []
    for cand in patch.parent.glob(pattern):
        if cand == patch or cand == base:
            continue
        if cand.is_file():
            candidates.append(cand)

    candidates.sort()
    return candidates[0] if candidates else None


def _wait_for_rom(patch: Path, *, timeout: int = 60) -> Optional[Path]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rom = _find_matching_rom(patch)
        if rom:
            print(f"[ap-bizhelper] ROM detected: {rom}")
            return rom
        time.sleep(1)
    print("[ap-bizhelper] Timed out waiting for ROM; not launching BizHawk.")
    return None


def _launch_bizhawk(runner: Path, rom: Path) -> None:
    print(f"[ap-bizhelper] Launching BizHawk runner: {runner} {rom}")
    try:
        subprocess.Popen([str(runner), str(rom)])
    except Exception as exc:  # pragma: no cover - safety net for runtime environments
        error_dialog(f"Failed to launch BizHawk runner: {exc}")


def _detect_new_bizhawk(baseline: Iterable[int], *, timeout: int = 10) -> bool:
    baseline_set = set(baseline)
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = _list_bizhawk_pids()
        if current.difference(baseline_set):
            return True
        time.sleep(1)
    return False


def _handle_bizhawk_for_patch(patch: Path, runner: Optional[Path], baseline_pids: Iterable[int]) -> None:
    if patch.suffix.lower() != ".sfc":
        print("[ap-bizhelper] Non-.sfc patch detected; skipping BizHawk auto-launch.")
        return

    if runner is None or not runner.is_file():
        print("[ap-bizhelper] BizHawk runner not configured or not executable; skipping auto-launch.")
        return

    rom = _wait_for_rom(patch)
    if rom is None:
        return

    ext = patch.suffix.lower().lstrip(".")
    behavior = get_ext_behavior(ext)
    print(f"[ap-bizhelper] Patch: {patch}")
    print(f"[ap-bizhelper] Detected extension: .{ext}")
    print(f"[ap-bizhelper] Saved behavior for .{ext}: {behavior or '<none>'}")

    if behavior == "auto":
        print("[ap-bizhelper] Behavior 'auto': not launching BizHawk; assuming AP/user handles it.")
        return
    if behavior == "fallback":
        print("[ap-bizhelper] Behavior 'fallback': launching BizHawk via Proton.")
        _launch_bizhawk(runner, rom)
        return
    if behavior not in (None, ""):
        print(f"[ap-bizhelper] Unknown behavior '{behavior}' for .{ext}; doing nothing for safety.")
        return

    print("[ap-bizhelper] No behavior stored yet; waiting briefly to see if BizHawk appears on its own.")
    if _detect_new_bizhawk(baseline_pids):
        print(f"[ap-bizhelper] Detected new BizHawk instance; recording .{ext} as 'auto'.")
        set_ext_behavior(ext, "auto")
        return

    print(
        "[ap-bizhelper] No BizHawk detected after fallback timeout; "
        f"switching .{ext} to 'fallback' and launching runner."
    )
    set_ext_behavior(ext, "fallback")
    _launch_bizhawk(runner, rom)


def _run_prereqs(*, allow_archipelago_skip: bool = False) -> Tuple[Optional[Path], Optional[Path]]:
    settings = load_settings()
    need_arch = _needs_archipelago_download(settings)
    need_bizhawk = _needs_bizhawk_download(settings)

    if any((need_arch, need_bizhawk)):
        arch, bizhawk, shortcuts = _prompt_setup_choices(
            allow_archipelago_skip=allow_archipelago_skip,
            show_archipelago=need_arch,
            show_bizhawk=need_bizhawk,
        )
    else:
        arch = False
        bizhawk = False
        shortcuts = False

    download_messages: list[str] = []

    appimage: Optional[Path] = None
    runner: Optional[Path] = None

    if arch or not allow_archipelago_skip:
        appimage = ensure_appimage(
            download_selected=arch,
            create_shortcut=shortcuts,
            download_messages=download_messages,
        )

    bizhawk_result: Optional[Tuple[Path, Path, bool]] = None
    bizhawk_result = ensure_bizhawk_and_proton(
        download_selected=bizhawk,
        create_shortcut=shortcuts,
        download_messages=download_messages,
    )
    if bizhawk:
        if bizhawk_result is None:
            raise RuntimeError("BizHawk setup was cancelled or failed.")
        runner, _, _ = bizhawk_result
    elif bizhawk_result is not None:
        runner, _, _ = bizhawk_result

    if download_messages:
        message = "Completed downloads:\n- " + "\n- ".join(download_messages)
        info_dialog(message)

    return appimage, runner


def _run_full_flow() -> int:
    try:
        appimage, runner = _run_prereqs()
    except RuntimeError as exc:
        error_dialog(str(exc))
        return 1

    if appimage is None:
        error_dialog("Archipelago was not selected for download and is required to continue.")
        return 1

    baseline_pids = _list_bizhawk_pids()

    try:
        patch = _select_patch_file()
    except RuntimeError as exc:
        error_dialog(str(exc))
        return 1

    ensure_apworld_for_patch(patch)

    print(f"[ap-bizhelper] Launching Archipelago with patch: {patch}")
    try:
        subprocess.Popen([str(appimage), str(patch)])
    except Exception as exc:  # pragma: no cover - runtime launcher safety net
        error_dialog(f"Failed to launch Archipelago: {exc}")
        return 1

    _handle_bizhawk_for_patch(patch, runner, baseline_pids)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "ensure":
        try:
            _run_prereqs(allow_archipelago_skip=True)
        except RuntimeError:
            return 1
        return 0

    if len(argv) >= 2:
        print("Usage: ap_bizhelper.py [ensure]", file=sys.stderr)
        return 1

    return _run_full_flow()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
