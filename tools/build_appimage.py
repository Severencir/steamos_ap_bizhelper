from __future__ import annotations

import base64
import shutil
import subprocess
import sys
import textwrap
import urllib.request
import venv
from pathlib import Path
from typing import Iterable, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = PROJECT_ROOT / "dist"
APPDIR = DIST_DIR / "AppDir"
APP_NAME = "ap-bizhelper"
APPIMAGE_TOOL_URL = (
    "https://github.com/AppImage/AppImageKit/releases/download/continuous/"
    "appimagetool-x86_64.AppImage"
)
APPIMAGE_TOOL_PATH = DIST_DIR / "appimagetool"
ICON_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAAJ0lEQVR4nO3BMQEAAADCoPVPbQ0PoAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAICtF7kAARQrxxgAAAAASUVORK5CYII="
)
LIB_SEARCH_ROOTS = [
    Path("/usr/lib"),
    Path("/usr/lib/x86_64-linux-gnu"),
    Path("/usr/local/lib"),
    Path("/lib"),
    Path("/lib/x86_64-linux-gnu"),
]


def _build_wheel() -> Path:
    DIST_DIR.mkdir(exist_ok=True)
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "wheel", ".", "--wheel-dir", str(DIST_DIR)],
            check=True,
            cwd=PROJECT_ROOT,
        )
    except subprocess.CalledProcessError as exc:
        setup_path = PROJECT_ROOT / "setup.py"
        if setup_path.exists():
            subprocess.run(
                [
                    sys.executable,
                    str(setup_path),
                    "bdist_wheel",
                    "--dist-dir",
                    str(DIST_DIR),
                ],
                check=True,
                cwd=PROJECT_ROOT,
            )
        else:
            raise RuntimeError(
                "Wheel build failed. The supported primary method is "
                "`python -m pip wheel . --wheel-dir dist`."
            ) from exc
    wheels = sorted(DIST_DIR.glob("ap_bizhelper-*.whl"))
    if not wheels:
        raise FileNotFoundError(
            "Wheel not found after build. The supported primary method is "
            "`python -m pip wheel . --wheel-dir dist`."
        )
    return wheels[-1]


def _create_appdir_venv() -> Path:
    if APPDIR.exists():
        shutil.rmtree(APPDIR)
    APPDIR.mkdir(parents=True)
    env_dir = APPDIR / "usr"
    venv.EnvBuilder(with_pip=True, symlinks=False, upgrade_deps=False).create(env_dir)
    return env_dir / "bin" / "python"


def _install_wheel(python: Path, wheel: Path) -> None:
    subprocess.run([python, "-m", "pip", "install", "--upgrade", "pip"], check=True)
    subprocess.run([python, "-m", "pip", "install", str(wheel)], check=True)


def _find_library_path(names: Iterable[str]) -> Optional[Path]:
    for name in names:
        for root in LIB_SEARCH_ROOTS:
            direct = root / name
            if direct.exists():
                return direct
            for candidate in root.glob(name):
                if candidate.exists():
                    return candidate
    try:
        import ctypes.util

        for name in names:
            hinted = ctypes.util.find_library(name) or ctypes.util.find_library(
                str(name).replace("lib", "").split(".so")[0]
            )
            if hinted:
                hinted_path = Path(hinted)
                if hinted_path.exists():
                    return hinted_path
                for root in LIB_SEARCH_ROOTS:
                    candidate = root / hinted
                    if candidate.exists():
                        return candidate
    except Exception:
        pass
    return None


def _bundle_runtime_libs(appdir: Path) -> None:
    lib_dir = appdir / "usr" / "lib"
    lib_dir.mkdir(parents=True, exist_ok=True)

    required_libs = {
        "SDL2": ["libSDL2-2.0.so.0"],
        "hidapi-hidraw": ["libhidapi-hidraw.so.0"],
        "hidapi-libusb": ["libhidapi-libusb.so.0"],
    }

    missing: list[str] = []
    for label, names in required_libs.items():
        path = _find_library_path(names)
        if path is None:
            missing.append(label)
            continue
        shutil.copy2(path, lib_dir / path.name)

    if missing:
        raise FileNotFoundError(
            "Missing native libraries: "
            + ", ".join(missing)
            + ". Please install libsdl2 and hidapi packages before building the AppImage."
        )


def _write_apprun(appdir: Path) -> None:
    apprun_path = appdir / "AppRun"
    content = textwrap.dedent(
        """
        #!/bin/sh
        set -e
        HERE="$(dirname "$(readlink -f "$0")")"
        export PATH="$HERE/usr/bin:$PATH"
        export APPDIR="$HERE"
        export LD_LIBRARY_PATH="$HERE/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
        exec "$HERE/usr/bin/python" -m ap_bizhelper "$@"
        """
    ).strip()
    apprun_path.write_text(content + "\n")
    apprun_path.chmod(0o755)


def _write_desktop(appdir: Path) -> None:
    desktop_path = appdir / f"{APP_NAME}.desktop"
    desktop_path.write_text(
        textwrap.dedent(
            f"""
            [Desktop Entry]
            Type=Application
            Name=AP BizHelper
            Exec=AppRun
            Icon={APP_NAME}
            Terminal=false
            Categories=Game;
            """
        ).strip()
        + "\n"
    )


def _write_icon(appdir: Path) -> None:
    icon_path = appdir / f"{APP_NAME}.png"
    icon_path.write_bytes(base64.b64decode(ICON_BASE64))


def _download_appimagetool() -> Path:
    if APPIMAGE_TOOL_PATH.exists():
        return APPIMAGE_TOOL_PATH

    DIST_DIR.mkdir(exist_ok=True)
    with urllib.request.urlopen(APPIMAGE_TOOL_URL) as resp:
        APPIMAGE_TOOL_PATH.write_bytes(resp.read())
    APPIMAGE_TOOL_PATH.chmod(0o755)
    return APPIMAGE_TOOL_PATH


def _build_appimage(appdir: Path) -> Path:
    tool = _download_appimagetool()
    appimage_path = DIST_DIR / f"{APP_NAME}.AppImage"
    subprocess.run(
        [str(tool), "--appimage-extract-and-run", str(appdir), str(appimage_path)],
        check=True,
    )
    return appimage_path


def build_appimage() -> Path:
    wheel = _build_wheel()
    python = _create_appdir_venv()
    _install_wheel(python, wheel)
    _bundle_runtime_libs(APPDIR)
    _write_apprun(APPDIR)
    _write_desktop(APPDIR)
    _write_icon(APPDIR)
    return _build_appimage(APPDIR)


if __name__ == "__main__":  # pragma: no cover
    artifact = build_appimage()
    print(f"Created AppImage: {artifact}")
