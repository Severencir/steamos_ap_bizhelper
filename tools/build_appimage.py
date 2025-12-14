from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import textwrap
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = PROJECT_ROOT / "dist"
APPDIR = DIST_DIR / "AppDir"
APP_NAME = "ap-bizhelper"
DEFAULT_TARGET_PYTHON = "python3.11"
SUPPORTED_PYTHON_MIN = (3, 10)
SUPPORTED_PYTHON_MAX = (3, 11)
APPIMAGE_TOOL_URL = (
    "https://github.com/AppImage/AppImageKit/releases/download/continuous/"
    "appimagetool-x86_64.AppImage"
)
APPIMAGE_TOOL_PATH = DIST_DIR / "appimagetool"
QTGAMEPAD_STUB = PROJECT_ROOT / "ap_bizhelper" / "qtgamepad_stub.py"
ICON_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAAJ0lEQVR4nO3BMQEAAADCoPVPbQ0PoAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAICtF7kAARQrxxgAAAAASUVORK5CYII="
)


def _get_python_version(python: Path) -> tuple[int, int, int]:
    output = subprocess.run(
        [str(python), "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    major, minor, micro = (int(part) for part in output.split("."))
    return major, minor, micro


def _resolve_target_python() -> Path:
    raw_path = os.environ.get("APPIMAGE_PYTHON", DEFAULT_TARGET_PYTHON)
    candidate = Path(raw_path)

    if not candidate.is_absolute():
        resolved = shutil.which(raw_path)
        if resolved is None:
            raise FileNotFoundError(
                f"Unable to locate target python '{raw_path}'. Set APPIMAGE_PYTHON to an explicit path."
            )
        candidate = Path(resolved)

    version = _get_python_version(candidate)
    if not (SUPPORTED_PYTHON_MIN <= version[:2] <= SUPPORTED_PYTHON_MAX):
        raise RuntimeError(
            "Unsupported python version for AppImage build: "
            f"{'.'.join(map(str, version))}. "
            f"Expected {SUPPORTED_PYTHON_MIN[0]}.{SUPPORTED_PYTHON_MIN[1]} "
            f"through {SUPPORTED_PYTHON_MAX[0]}.{SUPPORTED_PYTHON_MAX[1]}."
        )

    return candidate


def _build_wheel(python: Path) -> Path:
    DIST_DIR.mkdir(exist_ok=True)
    subprocess.run(
        [str(python), "-m", "build", "--wheel", "--outdir", str(DIST_DIR)],
        check=True,
        cwd=PROJECT_ROOT,
    )
    wheels = sorted(DIST_DIR.glob("ap_bizhelper-*.whl"))
    if not wheels:
        raise FileNotFoundError("Wheel not found after build")
    return wheels[-1]


def _create_appdir_venv(python: Path) -> Path:
    if APPDIR.exists():
        shutil.rmtree(APPDIR)
    APPDIR.mkdir(parents=True)
    env_dir = APPDIR / "usr"
    subprocess.run([str(python), "-m", "venv", str(env_dir)], check=True)
    return env_dir / "bin" / "python"


def _install_wheel(python: Path, wheel: Path) -> None:
    subprocess.run([python, "-m", "pip", "install", "--upgrade", "pip"], check=True)
    subprocess.run([python, "-m", "pip", "install", str(wheel)], check=True)


def _verify_qtgamepad_plugins(appdir: Path) -> None:
    site_packages = _site_packages(appdir)
    plugin_candidates = []

    for site in site_packages:
        plugin_root = site / "PySide6" / "Qt" / "plugins"
        if plugin_root.exists():
            plugin_candidates.extend(plugin_root.rglob("libqtgamepad*.so"))

    python = appdir / "usr" / "bin" / "python"
    import_check = subprocess.run(
        [
            python,
            "-c",
            "import importlib; import PySide6; importlib.import_module('PySide6.QtGamepad');"
            "print('QtGamepad import succeeded')",
        ],
        capture_output=True,
        text=True,
    )
    if import_check.returncode != 0:
        raise RuntimeError(
            "QtGamepad import failed inside AppImage env:\n"
            f"stdout: {import_check.stdout}\nstderr: {import_check.stderr}"
        )

    if plugin_candidates:
        found_paths = "\n".join(str(p.relative_to(appdir)) for p in sorted(plugin_candidates))
        print(f"Bundled QtGamepad plugins:\n{found_paths}")
    else:
        print("QtGamepad Qt plugin not found; relying on stubbed bindings.")


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


def _site_packages(appdir: Path) -> list[Path]:
    return sorted(appdir.glob("usr/lib/python*/site-packages"))


def _inject_qtgamepad_stub(appdir: Path) -> None:
    if not QTGAMEPAD_STUB.exists():
        raise FileNotFoundError("QtGamepad stub source missing from project")

    for site in _site_packages(appdir):
        target_dir = site / "PySide6"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / "QtGamepad.py"
        if target_path.exists():
            continue
        target_path.write_text(
            QTGAMEPAD_STUB.read_text()
            + "\n# Injected by ap-bizhelper build to provide QtGamepad bindings.\n"
        )


def _build_appimage(appdir: Path) -> Path:
    tool = _download_appimagetool()
    appimage_path = DIST_DIR / f"{APP_NAME}.AppImage"
    subprocess.run(
        [str(tool), "--appimage-extract-and-run", str(appdir), str(appimage_path)],
        check=True,
    )
    return appimage_path


def build_appimage() -> Path:
    target_python = _resolve_target_python()
    wheel = _build_wheel(target_python)
    python = _create_appdir_venv(target_python)
    _install_wheel(python, wheel)
    _inject_qtgamepad_stub(APPDIR)
    _verify_qtgamepad_plugins(APPDIR)
    _write_apprun(APPDIR)
    _write_desktop(APPDIR)
    _write_icon(APPDIR)
    return _build_appimage(APPDIR)


if __name__ == "__main__":  # pragma: no cover
    artifact = build_appimage()
    print(f"Created AppImage: {artifact}")
