#!/usr/bin/env python3
import os
import sys
import glob
import json
import shutil
import subprocess
from pathlib import Path
CONFIG_DIR = Path(os.path.expanduser("~/.config/ap_bizhelper_test"))
SETTINGS_FILE = CONFIG_DIR / "settings.json"


def _load_settings():
    if not SETTINGS_FILE.exists():
        return {}
    try:
        with SETTINGS_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def error_dialog(msg: str) -> None:
    """Show an error via zenity if available, else stderr."""
    if shutil.which("zenity"):
        try:
            subprocess.run(
                ["zenity", "--error", f"--text={msg}"],
                check=False,
            )
        except Exception:
            pass
    else:
        sys.stderr.write(f"ERROR: {msg}\n")
_SETTINGS_CACHE = None


def get_env_or_config(var: str):
    """Read config from the environment or stored settings."""
    value = os.environ.get(var)
    if value:
        return value

    global _SETTINGS_CACHE
    if _SETTINGS_CACHE is None:
        _SETTINGS_CACHE = _load_settings()

    value = _SETTINGS_CACHE.get(var)
    return str(value) if value else None


def ensure_bizhawk_exe() -> Path:
    exe = get_env_or_config("BIZHAWK_EXE")
    if not exe or not Path(exe).is_file():
        error_dialog("[ap-bizhelper] BIZHAWK_EXE is not set or not a file; cannot launch BizHawk.")
        sys.exit(1)
    return Path(exe)


def configure_proton_env():
    home = Path.home()
    proton_bin = get_env_or_config("PROTON_BIN") or "proton"
    proton_prefix = get_env_or_config("PROTON_PREFIX") or str(
        home / ".local" / "share" / "ap_bizhelper_test" / "proton_prefix"
    )
    steam_root = get_env_or_config("STEAM_ROOT") or str(
        home / ".local" / "share" / "Steam"
    )

    os.environ["STEAM_COMPAT_DATA_PATH"] = proton_prefix
    os.environ["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = steam_root

    return proton_bin, proton_prefix, steam_root


def find_archip_mount_dir():
    """Return the most recent /tmp/.mount_Archip* directory, if any."""
    candidates = glob.glob("/tmp/.mount_Archip*")
    if not candidates:
        return None
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return Path(candidates[0])


def find_bizhawk_connector_linux():
    """Locate connector_bizhawk_generic.lua inside the AP AppImage mount.

    Example path:
      /tmp/.mount_ArchipXXXXXX/opt/Archipelago/data/lua/connector_bizhawk_generic.lua
    """
    mount_dir = find_archip_mount_dir()
    if mount_dir is None:
        return None

    candidate = mount_dir / "opt" / "Archipelago" / "data" / "lua" / "connector_bizhawk_generic.lua"
    if candidate.is_file():
        return candidate
    return None


def ensure_data_lua_symlink(bizhawk_dir: Path, connector_linux: Path) -> Path | None:
    """Create a stable symlink into the Archipelago data/lua directory.

    We prefer to reference connector_bizhawk_generic.lua via a relative path inside
    the BizHawk directory so the Windows-side launcher receives a clean path. If the
    symlink cannot be created we return None and the caller can fall back to the
    absolute Z: mapping.
    """

    target_dir = connector_linux.parent
    link_path = bizhawk_dir / "ap_data_lua"

    # Remove broken/incorrect symlink to avoid stale references.
    if link_path.is_symlink():
        if not link_path.exists() or link_path.resolve() != target_dir:
            try:
                link_path.unlink()
            except Exception:
                return None
    elif link_path.exists():
        # If a regular directory/file is in the way, do not clobber it.
        return None

    if not link_path.exists():
        try:
            link_path.symlink_to(target_dir)
        except Exception:
            return None

    return link_path


def linux_path_to_proton_win_z(p: Path) -> str:
    """Convert /foo/bar to Z:\\foo\\bar for Proton."""
    as_posix = str(p)
    as_posix = as_posix.rstrip("/")
    return "Z:" + as_posix.replace("/", "\\")


def parse_args(argv):
    """Return (rom_path, ap_lua_arg, emu_args_no_lua).

    ap_lua_arg is the *original* --lua=... (if any), stripped from final args.
    """
    rom_path = None
    ap_lua_arg = None
    emu_args = []

    i = 0
    n = len(argv)
    while i < n:
        arg = argv[i]
        i += 1

        if arg.startswith("--lua="):
            ap_lua_arg = arg
        elif arg == "--lua":
            if i < n:
                ap_lua_arg = f"--lua={argv[i]}"
                i += 1
        else:
            if rom_path is None and not arg.startswith("-"):
                rom_path = arg
            else:
                emu_args.append(arg)

    # Recovery: if we didn't capture ROM earlier, try first non-option in emu_args
    if rom_path is None:
        for idx, a in enumerate(emu_args):
            if not a.startswith("-"):
                rom_path = a
                emu_args = emu_args[:idx] + emu_args[idx + 1 :]
                break

    if rom_path is None:
        error_dialog("[ap-bizhelper] No ROM path detected in arguments.")
        sys.exit(1)

    return rom_path, ap_lua_arg, emu_args


def decide_lua_arg(bizhawk_dir: Path, rom_path: str, ap_lua_arg: str | None) -> str:
    """Decide the final --lua=... argument or raise on failure.

    - For .sfc (SNES):
        * Must find BizHawkDir/lua/connector.lua (Windows SNI Lua).
    - For non-.sfc:
        * Prefer AP's --lua= path if provided.
        * Otherwise locate connector_bizhawk_generic.lua in AP mount,
          convert to Z:\\ path, and use that.
    - If we cannot satisfy the requirement, show dialog and exit.
    """
    ext = Path(rom_path).suffix.lower().lstrip(".")

    if ext == "sfc":
        # SNES + SNI
        lua_fs_path = bizhawk_dir / "lua" / "connector.lua"
        if not lua_fs_path.is_file():
            error_dialog(
                "[ap-bizhelper] Expected SNI Lua at "
                f"{lua_fs_path} but it is missing.\n"
                "Cannot safely launch SNES ROM without SNI connector."
            )
            sys.exit(1)
        print("[ap-bizhelper] Using SNI Lua connector for SNES ROM: lua\\connector.lua")
        return "--lua=lua\\connector.lua"

    # Non-SNES: must attach BizHawk AP connector
    lua_ap_path = None

    if ap_lua_arg:
        # ap_lua_arg looks like "--lua=Something"
        lua_ap_path = ap_lua_arg[len("--lua="):]

    if not lua_ap_path:
        connector_linux = find_bizhawk_connector_linux()
        if connector_linux is not None:
            link = ensure_data_lua_symlink(bizhawk_dir, connector_linux)
            if link is not None:
                lua_ap_path = "ap_data_lua\\connector_bizhawk_generic.lua"
            else:
                lua_ap_path = linux_path_to_proton_win_z(connector_linux)

    if not lua_ap_path:
        error_dialog(
            "[ap-bizhelper] Could not locate Archipelago BizHawk connector Lua "
            "(connector_bizhawk_generic.lua).\n"
            "Cannot safely launch non-SNES ROM without connector."
        )
        sys.exit(1)

    print(f"[ap-bizhelper] Using BizHawk AP Lua for non-SNES ROM: {lua_ap_path}")
    return f"--lua={lua_ap_path}"


def build_bizhawk_command(argv):
    """Transform incoming args into a Proton BizHawk command."""

    bizhawk_exe = ensure_bizhawk_exe()
    bizhawk_dir = bizhawk_exe.parent
    proton_bin, _, _ = configure_proton_env()

    rom_path, ap_lua_arg, emu_args = parse_args(argv)

    lua_arg = decide_lua_arg(bizhawk_dir, rom_path, ap_lua_arg)

    final_args = [rom_path, lua_arg] + emu_args

    print("[ap-bizhelper] Running BizHawk via Proton:")
    print(f"  BIZHAWK_EXE: {bizhawk_exe}")
    print(f"  ROM:         {rom_path}")
    print(f"  Lua:         {lua_arg}")

    bizhawk_exe_rel = bizhawk_exe.name

    return proton_bin, bizhawk_dir, [proton_bin, "run", bizhawk_exe_rel, *final_args]


def main(argv):
    proton_bin, bizhawk_dir, cmd = build_bizhawk_command(argv)
    os.chdir(bizhawk_dir)
    os.execvp(proton_bin, cmd)


if __name__ == "__main__":
    main(sys.argv[1:])
