#!/usr/bin/env python3
import os
import sys
import glob
import json
import os
import shutil
import subprocess
import sys
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
    symlink cannot be created we return None so the caller can fail loudly.
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

    return rom_path, ap_lua_arg, emu_args


def _detect_connector_name(ap_lua_arg: str | None) -> str | None:
    """Return the connector filename if an AP Lua path was supplied."""

    if not ap_lua_arg:
        return None

    if ap_lua_arg.startswith("--lua="):
        lua_path = ap_lua_arg[len("--lua=") :]
    else:
        lua_path = ap_lua_arg

    name = Path(lua_path).name
    if name.startswith("connector_") and name.endswith(".lua"):
        return name

    return None


def decide_lua_arg(bizhawk_dir: Path, rom_path: str, ap_lua_arg: str | None) -> str:
    """Decide the final --lua=... argument or raise on failure.

    - For .sfc (SNES):
        * Must find BizHawkDir/lua/connector.lua (Windows SNI Lua).
    - For non-.sfc:
        * Must locate connector_bizhawk_generic.lua within the Archipelago AppImage
          mount, expose it via BizHawk-local symlink, and pass the relative path.
    - If we cannot satisfy the requirement, show dialog and exit.
    """
    ext = Path(rom_path).suffix.lower().lstrip(".")

    if ext == "sfc":
        # SNES + SNI (always prefer local SNI connector even if AP passed --lua)
        lua_fs_path = bizhawk_dir / "lua" / "connector.lua"
        if not lua_fs_path.is_file():
            error_dialog(
                "[ap-bizhelper] Expected SNI Lua at "
                f"{lua_fs_path} but it is missing.\n"
                "Cannot safely launch SNES ROM without SNI connector."
            )
            sys.exit(1)
        if ap_lua_arg:
            print(
                "[ap-bizhelper] Ignoring AP-supplied --lua for SNES ROM; "
                "using bundled SNI connector instead."
            )
        print("[ap-bizhelper] Using SNI Lua connector for SNES ROM: lua\\connector.lua")
        return "--lua=lua\\connector.lua"

    connector_linux = find_bizhawk_connector_linux()
    if connector_linux is None:
        error_dialog(
            "[ap-bizhelper] Could not locate Archipelago BizHawk connector Lua "
            "(connector_bizhawk_generic.lua).\n"
            "Ensure the Archipelago AppImage is mounted before launching BizHawk."
        )
        sys.exit(1)

    link = ensure_data_lua_symlink(bizhawk_dir, connector_linux)
    if link is None:
        error_dialog(
            "[ap-bizhelper] Unable to prepare BizHawk-local symlink to Archipelago "
            "Lua connector.\n"
            "Cannot safely launch non-SNES ROM without connector."
        )
        sys.exit(1)

    connector_name = _detect_connector_name(ap_lua_arg) or "connector_bizhawk_generic.lua"
    connector_path = link / connector_name
    if connector_name != "connector_bizhawk_generic.lua" and not connector_path.is_file():
        connector_name = "connector_bizhawk_generic.lua"
        connector_path = link / connector_name

    if not connector_path.is_file():
        error_dialog(
            "[ap-bizhelper] Expected Archipelago BizHawk connector Lua next to "
            f"{connector_linux.name} but none was found.\n"
            "Cannot safely launch non-SNES ROM without connector."
        )
        sys.exit(1)

    lua_ap_path = f"ap_data_lua\\{connector_name}"
    print(f"[ap-bizhelper] Using BizHawk AP Lua for non-SNES ROM: {lua_ap_path}")
    return f"--lua={lua_ap_path}"


def build_bizhawk_command(argv):
    """Transform incoming args into a Proton BizHawk command."""

    bizhawk_exe = ensure_bizhawk_exe()
    bizhawk_dir = bizhawk_exe.parent
    proton_bin, _, _ = configure_proton_env()

    rom_path, ap_lua_arg, emu_args = parse_args(argv)

    if rom_path is None:
        final_args = emu_args
        print("[ap-bizhelper] No ROM detected; launching BizHawk without AP connector.")
    else:
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
