# relaTVity_core.py
"""
RelaTVity core utilities: paths, logger, subprocess wrappers, sanitizers.
Exports:
  logger, BASE_DIR, LOG_FILE, CREATE_NO_WINDOW,
  is_admin, elevate, run_subprocess, _run_cmd_list,
  sanitize_tunnel_name, _on_rm_error,
  _gpu_selection_event, _gpu_selection_choice,
  GuiVisibilityFilter,
  detect_cpu_gpu, build_node_name,
  persist_node_name, read_persisted_node_name
"""

import os
import sys
import ctypes
import subprocess
import logging
from logging.handlers import RotatingFileHandler
import stat
import re
import threading
import getpass
import tempfile
import logging
import shlex
import getpass
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)

# -------------------------
# Paths and constants
# -------------------------
BASE_DIR = Path("C:/RelaTVity")
LOG_FILE = BASE_DIR / "relaTVity_installer.log"
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 3
CREATE_NO_WINDOW = 0x08000000

# Ensure base dir exists for logging early
BASE_DIR.mkdir(parents=True, exist_ok=True)

# -------------------------
# Logger
# -------------------------
logger = logging.getLogger("relaTVity_installer")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    fh = RotatingFileHandler(str(LOG_FILE), maxBytes=LOG_MAX_BYTES,
                             backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

# GUI visibility filter (used by GUI handlers)
class GuiVisibilityFilter(logging.Filter):
    def filter(self, record):
        return bool(getattr(record, "gui", False))

# -------------------------
# Shared GUI synchronization objects
# -------------------------
# These are imported by the GUI and other modules to coordinate GPU selection prompts.
_gpu_selection_event = threading.Event()
_gpu_selection_choice = {"value": None}

# -------------------------
# Helpers
# -------------------------
def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False

def elevate():
    if is_admin():
        return True
    params = " ".join([f'"{arg}"' for arg in sys.argv])
    executable = sys.executable
    ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, params, None, 1)
    sys.exit(0)

def run_subprocess(cmd, capture=False, cwd=None, shell=True):
    """
    Run a subprocess. If capture=True returns stdout string, else returns None.
    """
    try:
        if capture:
            res = subprocess.run(cmd, shell=shell, check=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 cwd=cwd, text=True)
            logger.debug("Command output: %s", res.stdout)
            return res.stdout.strip()
        else:
            subprocess.run(cmd, shell=shell, check=True, cwd=cwd)
            return None
    except subprocess.CalledProcessError as e:
        logger.exception("Command failed: %s", e)
        return None

def _run_cmd_list(cmd_list):
    """
    Run a command list (no shell). Returns (ok, stdout, stderr).
    """
    try:
        res = subprocess.run(cmd_list, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=False)
        return True, res.stdout.strip(), res.stderr.strip()
    except Exception as e:
        logger.exception("Command execution failed: %s", e)
        return False, "", str(e)

def sanitize_tunnel_name(name, max_len=64):
    """
    Produce a WireGuard-safe tunnel identifier:
    - keep letters, digits, hyphen, underscore
    - replace other chars with hyphen
    - collapse repeated hyphens and trim edges
    - truncate to max_len
    """
    if not name:
        return "wg_tunnel"
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", str(name))
    s = re.sub(r"-{2,}", "-", s).strip("-_")
    if not s:
        s = "wg_tunnel"
    if len(s) > max_len:
        s = s[:max_len].rstrip("-_")
    return s

def _on_rm_error(func, path, exc_info):
    """
    shutil.rmtree onerror handler: clear readonly and retry.
    """
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        logger.exception("Failed to remove %s on retry", path)

# -------------------------
# AV1 detection helpers
# -------------------------
def _run_cmd(cmd: list, timeout: int = 6) -> str:
    """Run a command and return stdout (empty string on failure)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.stdout or "") + (p.stderr or "")
    except Exception:
        return ""

def _ffmpeg_available() -> bool:
    """Return True if ffmpeg is callable on PATH or in common locations."""
    try:
        out = _run_cmd(["ffmpeg", "-hide_banner", "-version"], timeout=3)
        return bool(out)
    except Exception:
        return False

def _detect_av1_hw_via_ffmpeg() -> bool:
    """
    Best-effort detection of hardware AV1 decoding/acceleration support using ffmpeg.
    Returns True if a hardware AV1 decoder/accelerator is detected.
    """
    if not _ffmpeg_available():
        return False

    # 1) Check decoders list for hardware AV1 decoders (qsv, nvdec, cuvid, d3d11va, dxva2, vaapi)
    decoders_out = _run_cmd(["ffmpeg", "-hide_banner", "-decoders"], timeout=6).lower()

    hw_tokens = (
        "av1_qsv",    # Intel QSV AV1
        "av1_nvdec",  # NVIDIA NVDEC AV1 (naming may vary)
        "av1_cuvid",  # older cuvid style (rare)
        "av1_vaapi",  # VAAPI hw accel
        "av1_dxva2",  # DXVA2
        "av1_d3d11va" # D3D11VA
    )
    for t in hw_tokens:
        if t in decoders_out:
            return True

    # 2) Check hwaccels list for known hardware accel backends (nvdec/qsv/vaapi/dxva2/d3d11va)
    hwaccels_out = _run_cmd(["ffmpeg", "-hide_banner", "-hwaccels"], timeout=4).lower()
    for backend in ("qsv", "nvdec", "vaapi", "dxva2", "d3d11va"):
        if backend in hwaccels_out:
            # presence of backend doesn't guarantee AV1 support, but it's a strong signal
            # try probing decoders again for av1 tokens combined with backend
            if "av1" in decoders_out:
                return True

    return False
    
def _ps_query(cmd):
    """Run a PowerShell command and return stripped stdout or None on error."""
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=6
        )
        out = out.strip()
        return out if out else None
    except Exception:
        return None

def _shorten_model(s, max_len=32):
    """Shorten and normalize a model string for use in a node name."""
    if not s:
        return None
    s = s.strip()
    s = re.sub(r"\b(Processor|CPU|Graphics|Adapter|Series|GPU|Graphics)\b", "", s, flags=re.I)
    s = re.sub(r"[\(\)\[\],/]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"[^A-Za-z0-9\-_]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-_")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-_")
    return s

# -------------------------
# CPU/GPU detection (simplified)
# -------------------------
def detect_cpu_gpu() -> Tuple[str, str]:
    """
    Return (cpu_label, gpu_label) where:
      - cpu_label is either "AV1" (hardware AV1 decode present) or "SVTAV1_CPU"
      - gpu_label is the literal "GPU" (kept generic)
    This intentionally excludes vendor/model branding for the CPU.
    """
    try:
        has_hw_av1 = _detect_av1_hw_via_ffmpeg()
    except Exception:
        has_hw_av1 = False

    cpu_label = "AV1" if has_hw_av1 else "SVTAV1_CPU"
    gpu_label = "GPU"
    return cpu_label, gpu_label

import os
import getpass
import re

def _compact_cpu_label(cpu: str) -> str:
    """
    Reduce CPU label to vendor + model token.
    Examples:
      "INTEL-13th-Gen-Intel-R-Core-TM-i7-1370" -> "INTEL-i7-1370"
      "AMD-Ryzen-7-5800X" -> "AMD-Ryzen7-5800X"
      "Intel(R) Core(TM) i5-12400" -> "INTEL-i5-12400"
    """
    if not cpu:
        return "CPU"
    s = cpu.upper()
    # Normalize separators
    s = re.sub(r"[^\w\- ]+", " ", s)
    tokens = re.split(r"[\s\-]+", s)
    # Determine vendor
    vendor = "INTEL" if any(t in s for t in ("INTEL", "CORE", "I3", "I5", "I7", "I9")) else \
             "AMD" if any(t in s for t in ("AMD", "RYZEN")) else "CPU"
    # Look for common model patterns: i7-1370, i5-12400, RYZEN7, 13700, 5800X
    model = None
    for t in tokens[::-1]:
        # i7-13700 or i7-1370 style
        if re.match(r"^I[3579]\-?\d{3,5}$", t):
            model = t.replace("-", "")
            break
        # numeric family like 13700 or 5800X
        if re.match(r"^\d{3,5}[A-Z]?$", t):
            model = t
            break
        # Ryzen tokens like RYZEN7 or RYZEN
        if re.match(r"^RYZEN\d?$", t):
            model = t
            break
    if model:
        # keep vendor-model with a dash, normalize Ryzen7 -> Ryzen7
        return f"{vendor}-{model}"
    # fallback: use last two meaningful tokens
    meaningful = [t for t in tokens if t and t not in ("INTEL","CORE","TM","GEN","R")][:3]
    if meaningful:
        return f"{vendor}-{'-'.join(meaningful[:2])}"
    return vendor
    
def _compact_gpu_label(gpu: str) -> str:
    """
    Reduce GPU label to vendor + model token.
    Examples:
      "NVIDIA-NVIDIA-RTX-2000-Ada-Generation-L" -> "NVIDIA-RTX2000-Ada"
      "AMD-Radeon-5700" -> "AMD-Radeon5700"
      "INTEL-IrisXe" -> "INTEL-IrisXe"
    """
    if not gpu:
        return "GPU"
    s = gpu.upper()
    s = re.sub(r"[^\w\- ]+", " ", s)
    tokens = re.split(r"[\s\-]+", s)
    vendor = "NVIDIA" if any(t in s for t in ("NVIDIA","GEFORCE","RTX")) else \
             "AMD" if any(t in s for t in ("AMD","RADEON")) else \
             "INTEL" if any(t in s for t in ("INTEL","IRIS")) else "GPU"
    # Look for RTX + number or RADEON + number or model tokens
    model = None
    # find RTX + number sequence
    for i, t in enumerate(tokens):
        if t in ("RTX","GTX") and i+1 < len(tokens) and re.match(r"^\d{3,4}", tokens[i+1]):
            model = f"{t}{tokens[i+1]}"
            # check for next token like ADA or SUPER
            if i+2 < len(tokens) and re.match(r"^[A-Z]+$", tokens[i+2]):
                model = f"{model}-{tokens[i+2].capitalize()}"
            break
    if not model:
        # Radeon family like RADEON 5700 or RADEONRX 6800
        for i, t in enumerate(tokens):
            if "RADEON" in t:
                # try next token
                if i+1 < len(tokens) and re.match(r"^[A-Z0-9]+", tokens[i+1]):
                    model = f"Radeon{tokens[i+1]}"
                else:
                    model = "Radeon"
                break
    if not model:
        # Iris or generic model token
        for t in tokens[::-1]:
            if re.match(r"^[A-Z][A-Z0-9]+$", t) and len(t) > 2:
                model = t.capitalize()
                break
    if model:
        return f"{vendor}-{model}"
    # fallback: last two tokens
    meaningful = [t for t in tokens if t and t not in ("NVIDIA","GEFORCE","RTX","AMD","RADEON","INTEL","IRIS")]
    if meaningful:
        return f"{vendor}-{'-'.join(meaningful[:2])}"
    return vendor

# -------------------------
# Node name builder
# -------------------------
def build_node_name(prefix=None, max_total=96):
    """
    Build a node name like: <prefix>_<CPUlabel>_<GPU>_GPU
    Examples:
      GiGo_AV1_GPU
      GiGo_SVTAV1_CPU_GPU  (note: we keep the trailing _GPU suffix for consistency)
    """
    user = prefix or os.getenv("DISCORD_DISPLAY_NAME") or os.getenv("NODE_NAME") or getpass.getuser() or "node"
    user = re.sub(r"[^A-Za-z0-9]+", "", str(user)) or "node"

    cpu_label, gpu_label = detect_cpu_gpu()

    # We want final forms like GiGo_AV1_GPU or GiGo_SVTAV1_CPU
    # Keep the trailing "_GPU" suffix for compatibility with existing consumers.
    name = f"{user}_{cpu_label}_{gpu_label}"

    # Ensure length limit: truncate if necessary (should be short anyway)
    if len(name) > max_total:
        name = name[:max_total].rstrip("_-")

    return name
    
# -------------------------
# Persist / read canonical node name
# -------------------------
_NODE_NAME_FILE = BASE_DIR / "node_name.txt"

def persist_node_name(name) -> bool:
    """
    Persist the canonical node name to disk atomically.
    Rejects None, the literal string "None", and empty/whitespace-only names.
    """
    try:
        # Reject actual None
        if name is None:
            logger.warning("persist_node_name called with None; skipping write")
            return False

        # Coerce and normalize
        s = str(name).strip()

        # Reject literal "None" (case-insensitive) and empty strings
        if not s or s.lower() == "none":
            logger.warning("persist_node_name called with invalid name %r; skipping write", name)
            return False

        # Ensure parent exists
        parent = _NODE_NAME_FILE.parent
        parent.mkdir(parents=True, exist_ok=True)

        # Atomic write
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(parent), delete=False) as tf:
            tf.write(s)
            tmp = Path(tf.name)
        tmp.replace(_NODE_NAME_FILE)

        logger.info("Persisted node name to %s: %s", _NODE_NAME_FILE, s)
        return True
    except Exception:
        logger.exception("Failed to persist node name %r", name)
        return False


def read_persisted_node_name():
    """Read persisted node name or return None."""
    try:
        if _NODE_NAME_FILE.exists():
            return _NODE_NAME_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        logger.exception("Failed to read persisted node name")
    return None