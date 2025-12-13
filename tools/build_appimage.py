from __future__ import annotations

import base64
import shutil
import subprocess
import sys
import textwrap
import urllib.request
import venv
from pathlib import Path

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


def _build_wheel() -> Path:
    DIST_DIR.mkdir(exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(DIST_DIR)],
        check=True,
        cwd=PROJECT_ROOT,
    )
    wheels = sorted(DIST_DIR.glob("ap_bizhelper-*.whl"))
    if not wheels:
        raise FileNotFoundError("Wheel not found after build")
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


def _write_apprun(appdir: Path) -> None:
    apprun_path = appdir / "AppRun"
    content = textwrap.dedent(
        """
        #!/bin/sh
        set -e
        HERE="$(dirname "$(readlink -f "$0")")"
        export PATH="$HERE/usr/bin:$PATH"
        QT_PLUGIN_PATH=""
        QML2_IMPORT_PATH=""
        for site in "$HERE"/usr/lib/python*/site-packages; do
            if [ -d "$site/PySide6/Qt/plugins" ]; then
                QT_PLUGIN_PATH="$site/PySide6/Qt/plugins${QT_PLUGIN_PATH:+:$QT_PLUGIN_PATH}"
            fi
            if [ -d "$site/PySide6_Addons/Qt/plugins" ]; then
                QT_PLUGIN_PATH="$site/PySide6_Addons/Qt/plugins${QT_PLUGIN_PATH:+:$QT_PLUGIN_PATH}"
            fi
            if [ -d "$site/PySide6/qml" ]; then
                QML2_IMPORT_PATH="$site/PySide6/qml${QML2_IMPORT_PATH:+:$QML2_IMPORT_PATH}"
            fi
            if [ -d "$site/PySide6_Addons/qml" ]; then
                QML2_IMPORT_PATH="$site/PySide6_Addons/qml${QML2_IMPORT_PATH:+:$QML2_IMPORT_PATH}"
            fi
        done
        export QT_PLUGIN_PATH
        export QML2_IMPORT_PATH
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
    _write_apprun(APPDIR)
    _write_desktop(APPDIR)
    _write_icon(APPDIR)
    return _build_appimage(APPDIR)


if __name__ == "__main__":  # pragma: no cover
    artifact = build_appimage()
    print(f"Created AppImage: {artifact}")
