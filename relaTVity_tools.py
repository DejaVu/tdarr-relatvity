# relaTVity_tools.py
"""
RelaTVity tools: download with progress, ffmpeg ensure, encoder detection, GPU enumeration.
Exports:
  download_with_progress, ensure_ffmpeg, detect_encoders, enumerate_gpus
"""

from pathlib import Path
import tempfile
import zipfile
import shutil
import json
import time
import requests
import logging
import subprocess
from relaTVity_core import logger, BASE_DIR, CREATE_NO_WINDOW, run_subprocess, _run_cmd_list

# Temporary working directory
TEMP_DIR = Path(tempfile.gettempdir()) / "relaTVity_temp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)
FFMPEG_EXE = TEMP_DIR / "ffmpeg.exe"

def download_with_progress(url, out_path, timeout=600, status_cb=None):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    filename = out_path.name
    logger.info("Downloading %s -> %s", url, out_path)
    try:
        with requests.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()
            total = r.headers.get("content-length")
            total = int(total) if total else None
            downloaded = 0
            last_report = -1
            chunk_size = 65536
            with open(out_path, "wb") as fh:
                for chunk in r.iter_content(chunk_size=chunk_size):
                    if chunk:
                        fh.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = int(downloaded * 100 / total)
                            if pct != last_report:
                                last_report = pct
                                msg = f"Downloading {filename}: {pct}%"
                                logger.debug(msg)
                                if status_cb:
                                    status_cb(msg, "info")
                        else:
                            if downloaded - last_report >= (512 * 1024):
                                last_report = downloaded
                                mb = downloaded / (1024 * 1024)
                                msg = f"Downloading {filename}: {mb:.1f} MB"
                                logger.debug(msg)
                                if status_cb:
                                    status_cb(msg, "info")
        logger.info("Downloaded %s", out_path)
        if status_cb:
            status_cb(f"Downloaded {filename}", "info")
        return True
    except Exception as e:
        logger.exception("Download failed: %s", e)
        if status_cb:
            status_cb(f"Download failed: {filename}", "warning")
        return False

def ensure_ffmpeg(status_cb=None):
    """
    Ensure ffmpeg is present. Returns path to ffmpeg.exe or None.
    """
    if FFMPEG_EXE.exists():
        if status_cb:
            status_cb("FFMPEG already present", "info")
        return str(FFMPEG_EXE)
    ffzip = TEMP_DIR / "ffmpeg.zip"
    ffurl = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    ok = download_with_progress(ffurl, ffzip, status_cb=status_cb)
    if ok and ffzip.exists():
        try:
            with zipfile.ZipFile(ffzip, "r") as z:
                z.extractall(TEMP_DIR)
            for p in TEMP_DIR.rglob("ffmpeg.exe"):
                try:
                    shutil.copy2(p, FFMPEG_EXE)
                    if status_cb:
                        status_cb("FFMPEG ready", "info")
                    return str(FFMPEG_EXE)
                except Exception:
                    continue
        except Exception:
            logger.exception("Failed to extract ffmpeg")
    # winget fallback (best-effort)
    try:
        run_subprocess("winget install --id Gyan.FFmpeg -e --silent")
        found = shutil.which("ffmpeg")
        if found:
            shutil.copy2(found, FFMPEG_EXE)
            if status_cb:
                status_cb("FFMPEG installed via winget", "info")
            return str(FFMPEG_EXE)
    except Exception:
        logger.exception("winget fallback failed")
    return None

def detect_encoders(ffmpeg_path):
    """
    Return dict of encoder presence (keys like av1_nvenc, libaom, hevc_nvenc).
    """
    patterns = {
        "av1_nvenc": "av1_nvenc",
        "av1_qsv": "av1_qsv",
        "av1_amf": "av1_amf",
        "libaom": "libaom-av1",
        "hevc_nvenc": "hevc_nvenc",
        "hevc_qsv": "hevc_qsv",
        "hevc_amf": "hevc_amf",
        "libx265": "libx265",
    }
    detected = {k: False for k in patterns}
    if not ffmpeg_path:
        logger.debug("ffmpeg not available; skipping encoder detection.")
        return detected
    try:
        out = run_subprocess(f'"{ffmpeg_path}" -hide_banner -encoders', capture=True)
        enc_text = out or ""
        for k, pat in patterns.items():
            detected[k] = (pat in enc_text)
        logger.debug("Detected encoders: %s", detected)
    except Exception:
        logger.exception("Failed to detect encoders")
    return detected

def enumerate_gpus():
    """
    Return a list of GPU names using PowerShell. Returns [] on failure.
    """
    try:
        res = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             'Get-CimInstance Win32_VideoController | Select-Object -Property Name | ConvertTo-Json'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=False
        )
        if not res.stdout:
            return []
        parsed = json.loads(res.stdout)
        if isinstance(parsed, list):
            return [str(item.get("Name", "UnknownGPU")) for item in parsed]
        elif isinstance(parsed, dict):
            return [str(parsed.get("Name", "UnknownGPU"))]
    except Exception:
        logger.exception("Failed to enumerate GPUs")
    return []