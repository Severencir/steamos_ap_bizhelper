"""Shared zenity-backed UI helpers and download utilities.

These helpers mirror the behaviour of the legacy Bash script while being
reusable across Python modules. All helpers are best-effort: when zenity is
unavailable they degrade to stdout/stderr logging instead of failing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Optional


def has_zenity() -> bool:
    return shutil.which("zenity") is not None


def info_dialog(message: str) -> None:
    if has_zenity():
        try:
            subprocess.run(["zenity", "--info", f"--text={message}"], check=False)
            return
        except Exception:
            pass
    sys.stderr.write(message + "\n")


def error_dialog(message: str) -> None:
    if has_zenity():
        try:
            subprocess.run(["zenity", "--error", f"--text={message}"], check=False)
            return
        except Exception:
            pass
    sys.stderr.write("ERROR: " + message + "\n")


def choose_install_action(title: str, text: str) -> str:
    """Return "Download", "Select" or "Cancel" using a zenity question dialog."""

    if not has_zenity():
        return "Cancel"

    proc = subprocess.run(
        [
            "zenity",
            "--question",
            f"--title={title}",
            f"--text={text}",
            "--ok-label=Download",
            "--cancel-label=Cancel",
            "--extra-button=Select",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if proc.returncode == 0:
        if proc.stdout.strip() == "Select":
            return "Select"
        return "Download"

    return "Cancel"


def file_selection(title: str, *, initial: Optional[Path] = None) -> Optional[Path]:
    """Return a Path chosen via zenity or None on cancel/zenity absence."""

    if not has_zenity():
        return None

    args = ["zenity", "--file-selection", f"--title={title}"]
    if initial:
        args.append(f"--filename={initial}")

    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return None

    path = Path(proc.stdout.strip())
    if not path.exists():
        error_dialog("Selected file does not exist.")
        return None
    return path


def download_with_progress(url: str, dest: Path, *, title: str, text: str) -> None:
    """
    Download a file using wget/curl, showing a cancelable zenity progress bar
    when available. Raises RuntimeError on failure.
    """

    downloader = shutil.which("wget") or shutil.which("curl")
    if downloader is None:
        raise RuntimeError("Neither wget nor curl is available for downloads.")

    if dest.exists():
        try:
            dest.unlink()
        except Exception:
            pass

    if not has_zenity():
        cmd = [downloader, "-O", str(dest), url] if downloader.endswith("wget") else [downloader, "-L", "-o", str(dest), url]
        proc = subprocess.run(cmd, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"Download failed with status {proc.returncode}")
        return

    fifo = Path(os.path.join(tempfile.gettempdir(), f"ap_bizhelper_fifo_{os.getpid()}"))
    try:
        fifo.unlink(missing_ok=True)  # type: ignore[arg-type]
    except Exception:
        pass
    fifo_path = str(fifo)

    os.mkfifo(fifo_path)

    writer = subprocess.Popen(
        [
            "bash",
            "-c",
            "p=1; while true; do echo $p; echo '#" + text + "'; sleep 0.3; p=$((p+2)); if [ $p -gt 95 ]; then p=1; fi; done",
        ],
        stdout=open(fifo_path, "w"),
        stderr=subprocess.DEVNULL,
        text=True,
    )

    zenity_proc = subprocess.Popen(
        [
            "zenity",
            "--progress",
            f"--title={title}",
            "--percentage=0",
            "--auto-close",
        ],
        stdin=open(fifo_path, "r"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    cmd = [downloader, "-O", str(dest), url] if downloader.endswith("wget") else [downloader, "-L", "-o", str(dest), url]
    download_proc = subprocess.Popen(cmd)

    status = 0
    try:
        while True:
            if download_proc.poll() is not None:
                status = download_proc.returncode or 0
                break
            if zenity_proc.poll() is not None:
                download_proc.terminate()
                status = 1
                break
            try:
                download_proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                continue
    finally:
        try:
            writer.terminate()
        except Exception:
            pass
        try:
            with open(fifo_path, "w") as f:
                f.write("100\n#Finished\n")
        except Exception:
            pass
        try:
            zenity_proc.terminate()
        except Exception:
            pass
        try:
            fifo.unlink()
        except Exception:
            pass

    if status != 0:
        raise RuntimeError("Download failed or was cancelled.")


def pgrep(pattern: str) -> Iterable[str]:
    """Return a list of matching PIDs (strings) for the given pattern."""

    try:
        proc = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            return []
        return [p for p in proc.stdout.strip().splitlines() if p]
    except Exception:
        return []

