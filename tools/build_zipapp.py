from __future__ import annotations

import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import zipapp

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = PROJECT_ROOT / "dist"


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


def _create_zipapp(source_dir: Path, target: Path) -> None:
    zipapp.create_archive(
        source_dir,
        target=target,
        interpreter="/usr/bin/env python3",
        main="ap_bizhelper.__main__:console_main",
    )


def build_zipapp() -> Path:
    wheel = _build_wheel()
    pyz_path = DIST_DIR / "ap-bizhelper.pyz"

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        with zipfile.ZipFile(wheel, "r") as zf:
            zf.extractall(tmp_path)
        _create_zipapp(tmp_path, pyz_path)

    return pyz_path


if __name__ == "__main__":  # pragma: no cover
    artifact = build_zipapp()
    print(f"Created zipapp: {artifact}")
