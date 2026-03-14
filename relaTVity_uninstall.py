# relaTVity_uninstall.py
"""
RelaTVity uninstall and cleanup helpers.
Exports:
  uninstall_all -needs work
"""

import shutil
from pathlib import Path
import os
import stat
from relaTVity_core import logger, is_admin, elevate, _on_rm_error, run_subprocess
import tempfile

TDARR_INSTALL_PATH = Path("C:/Tdarr_Updater")
BASE_DIR = Path("C:/RelaTVity")
RCLONE_DIR = BASE_DIR / ".rclone"
TEMP_DIR = Path(tempfile.gettempdir()) / "relaTVity_temp"

def uninstall_all(node_name_hint=None, confirmed=False, status_cb=None):
    """
    Remove installed files, stop tunnels, delete scheduled task, and clean directories.
    Requires confirmed=True to proceed.
    """
    if not confirmed:
        if status_cb:
            status_cb("Uninstall cancelled by user.", "info")
        return

    if not is_admin():
        elevate()

    if status_cb:
        status_cb("Uninstall started. See log for details.", "info")

    # Attempt to stop/uninstall WireGuard tunnels
    wg_exe = Path("C:/Program Files/WireGuard/wireguard.exe")
    tunnel_names = set()
    if node_name_hint:
        tunnel_names.add(node_name_hint)

    wg_dir = TDARR_INSTALL_PATH / "WireGuard"
    if wg_dir.exists():
        for p in wg_dir.glob("*.conf"):
            tunnel_names.add(p.stem)

    for tname in list(tunnel_names):
        if wg_exe.exists():
            try:
                run_subprocess([str(wg_exe), "/stoptunnelservice", tname], shell=False)
            except Exception:
                logger.exception("Failed to stop tunnel %s", tname)
            try:
                run_subprocess([str(wg_exe), "/uninstalltunnelservice", tname], shell=False)
            except Exception:
                logger.exception("Failed to uninstall tunnel %s", tname)

    # Remove WireGuard configs
    if wg_dir.exists():
        for p in wg_dir.glob("*.conf"):
            try:
                p.unlink()
            except Exception:
                logger.exception("Failed to remove %s", p)
        try:
            wg_dir.rmdir()
        except Exception:
            pass

    # Remove Tdarr config files
    try:
        cfg = TDARR_INSTALL_PATH / "configs" / "Tdarr_Node_Config.json"
        if cfg.exists():
            cfg.unlink()
    except Exception:
        logger.exception("Failed to remove Tdarr config")

    # Remove watchdog and scheduled task
    watchdog_path = BASE_DIR / "relaTVity_maint.ps1"
    try:
        if watchdog_path.exists():
            watchdog_path.unlink()
    except Exception:
        logger.exception("Failed to remove watchdog script")

    try:
        run_subprocess('schtasks /Delete /TN "RelaTVityMaintenance" /F')
    except Exception:
        logger.exception("Failed to delete scheduled task (may not exist)")

    # Kill rclone and other known processes (best-effort)
    try:
        import psutil
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = (p.info.get("name") or "").lower()
                cmd = " ".join(p.info.get("cmdline") or [])
                if "rclone" in name or "rclone" in cmd:
                    logger.info("Killing process %s (pid=%s)", name, p.pid)
                    p.kill()
            except Exception:
                pass
    except Exception:
        # fallback: taskkill best-effort
        try:
            run_subprocess('taskkill /IM rclone.exe /F')
        except Exception:
            pass

    # Remove directories (best-effort)
    for d in [TDARR_INSTALL_PATH, RCLONE_DIR, BASE_DIR, TEMP_DIR]:
        try:
            if d.exists():
                shutil.rmtree(d, onerror=_on_rm_error)
                logger.debug("Removed directory %s", d)
        except Exception:
            logger.exception("Failed to remove %s", d)

    if status_cb:
        status_cb("Uninstall complete. See log for details.", "info")
    logger.info("Uninstall completed.")