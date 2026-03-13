"""
relaTVity_winfsp.py

Helpers to detect, download, and install WinFsp on Windows.

Provides:
- DEFAULT_WINFSP_URL
- is_winfsp_installed() -> bool
- download_winfsp(dest_path: Path, download_url: Optional[str] = None) -> bool
- launch_winfsp_installer_interactive(msi_path: Path, log_path: Path) -> bool
- download_and_run_winfsp_interactive(download_url: Optional[str] = None, status_cb=None) -> bool
- ensure_winfsp_installed(status_cb=None, download_url: Optional[str] = None) -> bool

This module is written to be robust in environments with proxies/AV/policy restrictions:
- Tries requests with a browser User-Agent first.
- Falls back to PowerShell Invoke-WebRequest if requests fails.
- Launches the MSI interactively with elevation using PowerShell Start-Process -Verb RunAs.
- Writes verbose MSI logs to %TEMP%\winfsp-install.log for diagnosis.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_WINFSP_URL = "https://github.com/winfsp/winfsp/releases/download/v2.1/winfsp-2.1.25156.msi"


def is_winfsp_installed() -> bool:
    """
    Quick check whether WinFsp appears installed.
    Returns True if winfspctl.exe exists in Program Files.
    """
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WinFsp" / "bin" / "winfspctl.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "WinFsp" / "bin" / "winfspctl.exe",
    ]
    for p in candidates:
        try:
            if p.exists():
                logger.debug("Found WinFsp binary at %s", p)
                return True
        except Exception:
            continue
    # As a fallback, try running winfspctl from PATH
    try:
        res = subprocess.run(["winfspctl", "--version"], capture_output=True, text=True, shell=False, timeout=5)
        if res.returncode == 0:
            logger.debug("winfspctl returned version output")
            return True
    except Exception:
        pass
    logger.debug("WinFsp not detected")
    return False


def download_winfsp(dest_path: Path, download_url: Optional[str] = None, status_cb=None, timeout: int = 60) -> bool:
    """
    Download WinFsp MSI to dest_path.
    Tries requests with a browser-like User-Agent first, then falls back to PowerShell Invoke-WebRequest.
    Returns True on success.
    """
    download_url = download_url or DEFAULT_WINFSP_URL
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    if status_cb:
        status_cb("Downloading WinFsp", "info")
    # Try requests first
    try:
        import requests  # local import so module can be imported on non-Windows systems without requests
    except Exception:
        requests = None

    if requests:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        try:
            logger.info("Attempting download via requests: %s -> %s", download_url, dest_path)
            with requests.get(download_url, stream=True, timeout=timeout, headers=headers) as r:
                r.raise_for_status()
                with open(dest_path, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            fh.write(chunk)
            if dest_path.exists() and dest_path.stat().st_size > 0:
                logger.info("Downloaded WinFsp via requests to %s", dest_path)
                return True
            logger.warning("requests download produced empty file at %s", dest_path)
        except Exception:
            logger.exception("requests download failed for %s", download_url)

    # Fallback to PowerShell Invoke-WebRequest
    try:
        logger.info("Falling back to PowerShell Invoke-WebRequest for %s", download_url)
        if status_cb:
            status_cb("Downloading WinFsp (PowerShell fallback)", "info")
        ps_cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            f"Invoke-WebRequest -Uri \"{download_url}\" -OutFile \"{str(dest_path)}\" -UseBasicParsing"
        ]
        subprocess.run(ps_cmd, check=True, timeout=300)
        if dest_path.exists() and dest_path.stat().st_size > 0:
            logger.info("Downloaded WinFsp via PowerShell to %s", dest_path)
            return True
        logger.warning("PowerShell download completed but file missing or empty at %s", dest_path)
    except Exception:
        logger.exception("PowerShell fallback download failed for %s", download_url)

    if status_cb:
        status_cb("Failed to download WinFsp", "warning")
    return False


def launch_winfsp_installer_interactive(msi_path: Path, log_path: Path, status_cb=None) -> bool:
    """
    Launch the MSI interactively with elevation so the user sees the installer UI.
    Uses PowerShell Start-Process -Verb RunAs -Wait to prompt for UAC.
    Returns True if the process was started successfully (not a guarantee of install success).
    """
    if status_cb:
        status_cb("Launching WinFsp installer (interactive)", "info")
    try:
        # Build a PowerShell command that starts msiexec elevated and waits
        ps_cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            f"Start-Process -FilePath msiexec -ArgumentList '/i','{str(msi_path)}','/passive','/norestart','/l*v','{str(log_path)}' -Verb RunAs -Wait"
        ]
        logger.info("Running interactive installer via PowerShell: %s", " ".join(ps_cmd))
        subprocess.run(ps_cmd, check=True)
        logger.info("Interactive installer process completed (or was dismissed by user)")
        return True
    except subprocess.CalledProcessError:
        logger.exception("Interactive installer process failed to start")
        if status_cb:
            status_cb("Failed to launch WinFsp installer", "warning")
        return False
    except Exception:
        logger.exception("Unexpected error launching interactive installer")
        if status_cb:
            status_cb("Failed to launch WinFsp installer", "warning")
        return False


def download_and_run_winfsp_interactive(download_url: Optional[str] = None, status_cb=None) -> bool:
    """
    High-level helper: download WinFsp MSI to %TEMP% as winfsp-installer.msi and launch interactive installer.
    Returns True if the installer process was started successfully.
    """
    temp_dir = Path(os.getenv("TEMP", "C:\\Temp"))
    msi_path = temp_dir / "winfsp-installer.msi"
    log_path = temp_dir / "winfsp-install.log"

    # If file already exists and looks valid, skip download
    if msi_path.exists() and msi_path.stat().st_size > 0:
        if status_cb:
            status_cb(f"Found existing installer at {msi_path}", "info")
        logger.info("Using existing MSI at %s", msi_path)
    else:
        ok = download_winfsp(msi_path, download_url=download_url, status_cb=status_cb)
        if not ok:
            logger.error("Failed to download WinFsp MSI")
            return False

    # Launch interactive installer
    return launch_winfsp_installer_interactive(msi_path=msi_path, log_path=log_path, status_cb=status_cb)


def install_winfsp_silent(msi_path: Path, status_cb=None) -> bool:
    """
    Attempt a silent install using msiexec with verbose logging.
    Returns True if msiexec returns 0 or 3010 (success or success+reboot required).
    """
    log_path = Path(os.getenv("TEMP", "C:\\Temp")) / "winfsp-install.log"
    try:
        if status_cb:
            status_cb("Installing WinFsp (silent)", "info")
        cmd = ['msiexec', '/i', str(msi_path), '/qn', '/norestart', '/l*v', str(log_path)]
        logger.info("Running msiexec: %s", " ".join(cmd))
        res = subprocess.run(cmd, capture_output=True, text=True, shell=False)
        rc = res.returncode
        logger.info("msiexec returned %s; stdout=%s stderr=%s", rc, (res.stdout or "")[:1000], (res.stderr or "")[:1000])
        if rc in (0, 3010):
            if status_cb:
                status_cb("WinFsp installed", "info")
            return True
        logger.warning("msiexec returned non-success code %s; log at %s", rc, log_path)
        return False
    except Exception:
        logger.exception("Silent msiexec install failed")
        if status_cb:
            status_cb("Failed to run WinFsp installer", "warning")
        return False


def ensure_winfsp_installed(status_cb=None, download_url: Optional[str] = None) -> bool:
    """
    Ensure WinFsp is installed. Returns True if installed or successfully installed.
    Behavior:
      - If already installed, returns True.
      - Attempts silent install first (download + msiexec /qn).
      - If silent install fails, falls back to launching interactive installer for the user.
    """
    if not os.name == "nt":
        logger.warning("WinFsp installation is only supported on Windows")
        if status_cb:
            status_cb("WinFsp installation only supported on Windows", "warning")
        return False

    if is_winfsp_installed():
        if status_cb:
            status_cb("WinFsp already installed", "info")
        return True

    temp_dir = Path(os.getenv("TEMP", "C:\\Temp"))
    msi_path = temp_dir / "winfsp-installer.msi"

    # Download MSI (skip if already present)
    if not (msi_path.exists() and msi_path.stat().st_size > 0):
        ok = download_winfsp(msi_path, download_url=download_url, status_cb=status_cb)
        if not ok:
            # If download failed, offer interactive download+run helper
            if status_cb:
                status_cb("Failed to download WinFsp", "warning")
            return False

    # Try silent install first
    if install_winfsp_silent(msi_path, status_cb=status_cb):
        # small delay to allow registration
        time.sleep(2)
        if is_winfsp_installed():
            return True

    # Silent install failed; fall back to interactive installer
    if status_cb:
        status_cb("Silent install failed; launching interactive installer", "info")
    launched = launch_winfsp_installer_interactive(msi_path=msi_path, log_path=temp_dir / "winfsp-install.log", status_cb=status_cb)
    if not launched:
        return False

    # After interactive installer completes, check again
    time.sleep(2)
    return is_winfsp_installed()


# If run as a script, provide a simple CLI for manual testing
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    def _status(msg, level="info"):
        print(f"[{level.upper()}] {msg}")

    print("Checking WinFsp...")
    if is_winfsp_installed():
        print("WinFsp already installed.")
    else:
        print("Downloading and launching interactive installer...")
        ok = download_and_run_winfsp_interactive(status_cb=_status)
        print("Launched installer:", ok)