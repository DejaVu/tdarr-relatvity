# relaTVity_tdarr.py
"""
Tdarr download, configuration, and updater/tray launcher helpers.
"""

import os
import shutil
import zipfile
import time
import requests
import subprocess
from pathlib import Path
import json
import certifi
from requests.exceptions import SSLError

from relaTVity_core import logger, BASE_DIR, CREATE_NO_WINDOW

# Public constant for tests
TDARR_DOWNLOAD_URL = "https://storage.tdarr.io/versions/2.17.01/win32_x64/Tdarr_Updater.zip"
TDARR_INSTALL_PATH = Path("C:/Tdarr_Updater")
TDARR_TEMP_ZIP = TDARR_INSTALL_PATH / "tdarr.zip"
TDARR_CONFIG_DIR = Path("C:/Tdarr_Updater/configs")
TDARR_CONFIG_PATH = TDARR_CONFIG_DIR / "Tdarr_Node_Config.json"

# -------------------------
# Helpers
# -------------------------
def _ensure_install_path():
    try:
        TDARR_INSTALL_PATH.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception("Failed to ensure Tdarr install directory %s", TDARR_INSTALL_PATH)

def download_tdarr(status_cb=None, url=None, timeout=300):
    """
    Download Tdarr zip to TDARR_INSTALL_PATH/tdarr.zip.
    Returns True on success, False on failure.
    """
    _ensure_install_path()
    url = url or TDARR_DOWNLOAD_URL
    if not url:
        logger.error("No Tdarr download URL configured")
        if status_cb:
            status_cb("Tdarr download URL not configured", "error")
        return False

    try:
        logger.info("Downloading tdarr.zip: %s", url)
        if status_cb:
            status_cb("Downloading tdarr.zip", "info")
        # Use certifi bundle for reliable CA verification
        resp = requests.get(url, stream=True, timeout=30, verify=certifi.where())
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        with open(TDARR_TEMP_ZIP, "wb") as fh:
            downloaded = 0
            for chunk in resp.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                fh.write(chunk)
                downloaded += len(chunk)
                # Optionally report progress
                if total and status_cb:
                    pct = int(downloaded * 100 / total)
                    status_cb(f"Downloading tdarr.zip: {pct}%", "info")
        logger.info("Downloaded %s", TDARR_TEMP_ZIP)
        if status_cb:
            status_cb(f"Downloaded {TDARR_TEMP_ZIP}", "info")
        return True
    except SSLError as e:
        logger.exception("SSL error when downloading Tdarr: %s", e)
        if status_cb:
            status_cb("SSL certificate verification failed for Tdarr download; check system CA or URL", "error")
        return False
    except Exception:
        logger.exception("Failed to download Tdarr")
        if status_cb:
            status_cb("Failed to download Tdarr", "error")
        return False

def extract_tdarr(status_cb=None):
    """
    Extract the downloaded tdarr.zip into TDARR_INSTALL_PATH.
    Returns True on success.
    """
    try:
        if not TDARR_TEMP_ZIP.exists():
            logger.error("Tdarr zip not found: %s", TDARR_TEMP_ZIP)
            if status_cb:
                status_cb("Tdarr zip not found", "error")
            return False
        logger.info("Extracting Tdarr to %s", TDARR_INSTALL_PATH)
        with zipfile.ZipFile(TDARR_TEMP_ZIP, "r") as z:
            z.extractall(TDARR_INSTALL_PATH)
        logger.info("Tdarr extracted to %s", TDARR_INSTALL_PATH)
        if status_cb:
            status_cb("Tdarr downloaded and extracted", "info")
        return True
    except Exception:
        logger.exception("Failed to extract Tdarr")
        if status_cb:
            status_cb("Failed to extract Tdarr", "error")
        return False

def write_tdarr_config(node_name, ffmpeg_path, status_cb=None):
    """
    Write Tdarr node config JSON to C:/Tdarr_Updater/configs/Tdarr_Node_Config.json
    with the exact structure required by your example.
    """
    try:
        TDARR_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        cfg = {
            "nodeName": node_name,
            "serverURL": "http://10.8.0.1:8266",
            "serverIP": "10.89.100.35",
            "serverPort": "8266",
            "handbrakePath": "",
            "ffmpegPath": ffmpeg_path or "",
            "mkvpropeditPath": "",
            "pathTranslators": [
                {"server": "/media", "node": "C:/RelaTVity/Media"},
                {"server": "/tdarr-output", "node": "C:/RelaTVity/Tdarr-Output"}
            ],
            "nodeType": "mapped",
            "unmappedNodeCache": "C:\\Tdarr_Updater\\unmappedNodeCache",
            "logLevel": "INFO",
            "priority": -1,
            "cronPluginUpdate": "",
            "apiKey": "tapi_9c9axDGLz",
            "maxLogSizeMB": 10,
            "pollInterval": 2000,
            "startPaused": False
        }

        with open(TDARR_CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)

        logger.info("Wrote Tdarr config to %s", TDARR_CONFIG_PATH)
        if status_cb:
            status_cb(f"Wrote Tdarr config to {TDARR_CONFIG_PATH}", "info")
        return True
    except Exception:
        logger.exception("Failed to write Tdarr config")
        if status_cb:
            status_cb("Failed to write Tdarr config", "warning")
        return False

# -------------------------
# Updater (no tray launch)
# -------------------------
def run_tdarr_updater_then_tray(status_cb=None, updater_timeout=180):
    """
    Run Tdarr_Updater.exe (non-blocking if it continues running).
    This function intentionally does NOT start the tray; the caller (installer)
    is responsible for starting the tray/node to avoid duplicate launches.
    Returns True if an updater executable was found (and started or exited quickly),
    False if no updater was found.
    """
    _ensure_install_path()

    # Find updater executable
    updater_candidates = [
        TDARR_INSTALL_PATH / "Tdarr_Updater.exe",
        TDARR_INSTALL_PATH / "Tdarr-Updater.exe",
        TDARR_INSTALL_PATH / "Tdarr_Updater" / "Tdarr_Updater.exe",
    ]
    updater_exe = None
    for c in updater_candidates:
        if c.exists():
            updater_exe = c
            break
    if not updater_exe:
        for p in TDARR_INSTALL_PATH.rglob("*.exe"):
            name = p.name.lower()
            if "tdarr" in name and "updat" in name:
                updater_exe = p
                break

    if not updater_exe:
        logger.warning("Tdarr updater not found; skipping updater step")
        if status_cb:
            status_cb("Tdarr updater not found; skipping", "warning")
        return False

    # --- Run updater: ensure cwd, capture brief stderr, and do not block long-term ---
    logger.info("Running Tdarr updater: %s", updater_exe)
    if status_cb:
        status_cb(f"Running Tdarr updater: {updater_exe.name}", "info")
    try:
        exe_path = str(updater_exe)
        exe_cwd = str(updater_exe.parent)

        proc = subprocess.Popen(
            [exe_path],
            cwd=exe_cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,   # capture stderr briefly to detect immediate crashes
            stdin=subprocess.DEVNULL,
            shell=False,
            creationflags=CREATE_NO_WINDOW
        )
        logger.info("Started Tdarr updater (pid=%s) cwd=%s", proc.pid, exe_cwd)

        # Wait a short time to see if it exits immediately (indicates crash or quick exit)
        try:
            out_err = proc.communicate(timeout=3)
            # If process exited within timeout, log returncode and any stderr
            if proc.returncode is not None:
                logger.info("Tdarr updater exited quickly (code=%s)", proc.returncode)
                stderr = out_err[1] if len(out_err) > 1 else None
                if stderr:
                    try:
                        s = stderr.decode(errors="replace") if isinstance(stderr, bytes) else str(stderr)
                        logger.debug("Tdarr updater stderr: %s", s)
                    except Exception:
                        logger.debug("Tdarr updater stderr (raw): %r", stderr)
                if status_cb:
                    status_cb(f"Tdarr updater exited (code {proc.returncode})", "warning")
            else:
                logger.debug("Tdarr updater still running after short check")
        except subprocess.TimeoutExpired:
            # Process is still running — assume it detached or is interactive; do not block installer
            logger.info("Tdarr updater appears to be running (did not exit immediately); continuing installer")
            if status_cb:
                status_cb("Tdarr updater started", "info")
    except Exception:
        logger.exception("Failed to run Tdarr updater")
        if status_cb:
            status_cb("Failed to run Tdarr updater; continuing", "warning")
        # Still return True because updater was found but failed to start cleanly
        return True

    # Do not auto-start the tray here; let the caller (installer) manage starting tray/node.
    return True

# -------------------------
# Cleanup helper (optional)
# -------------------------
def remove_tdarr_install(status_cb=None):
    """
    Remove the TDARR_INSTALL_PATH directory entirely. Use with caution.
    """
    try:
        if TDARR_INSTALL_PATH.exists():
            shutil.rmtree(TDARR_INSTALL_PATH)
            logger.info("Removed Tdarr install directory %s", TDARR_INSTALL_PATH)
            if status_cb:
                status_cb("Removed Tdarr install directory", "info")
            return True
        else:
            logger.info("Tdarr install directory not present: %s", TDARR_INSTALL_PATH)
            if status_cb:
                status_cb("Tdarr install directory not present", "info")
            return True
    except Exception:
        logger.exception("Failed to remove Tdarr install directory")
        if status_cb:
            status_cb("Failed to remove Tdarr install directory", "warning")
        return False