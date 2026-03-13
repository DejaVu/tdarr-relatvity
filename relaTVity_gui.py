# relaTVity_gui.py
"""
RelaTVity GUI and orchestration.
Run this file to start the installer GUI.
"""

import threading
import time
import tkinter as tk
from tkinter import ttk, scrolledtext, simpledialog, messagebox
import json
import os
import logging
from pathlib import Path
import re

# Image support for GUI logo
try:
    from PIL import Image, ImageTk
except Exception:
    Image = None
    ImageTk = None

LOGO_PATH = Path("logo.png")
LOGO_SIZE = (240, 240)

from relaTVity_core import (
    logger,
    GuiVisibilityFilter,
    is_admin,
    elevate,
    BASE_DIR,
    sanitize_tunnel_name,
    _gpu_selection_event,
    _gpu_selection_choice,
    read_persisted_node_name,
    build_node_name,
    persist_node_name,
)

from relaTVity_tools import ensure_ffmpeg, detect_encoders, enumerate_gpus
from relaTVity_tdarr import download_tdarr, write_tdarr_config, run_tdarr_updater_then_tray, TDARR_DOWNLOAD_URL
from relaTVity_wireguard import get_wireguard_config, install_wireguard_and_apply, create_watchdog, register_scheduled_task
from relaTVity_uninstall import uninstall_all
from relaTVity_notify import send_discord_node_online
from relaTVity_rclone import ensure_rclone_installed, start_rclone_mounts_now, write_sftp_remotes, build_minimal_rclone_config, run_rclone_command, create_watchdog_script, create_startup_shortcut, find_rclone_executable
from relaTVity_winfsp import ensure_winfsp_installed, is_winfsp_installed


# rclone helpers (including SFTP remotes + watchdog creation)
from relaTVity_rclone import (
    ensure_rclone_installed,
    write_rclone_config,
    run_rclone_command,
    build_minimal_rclone_config,
    write_sftp_remotes,
    create_watchdog_script,
    create_startup_shortcut,
    find_rclone_executable,
    start_rclone_mounts_now,
)

LOG_FILE = BASE_DIR / "relaTVity_installer.log"

# Console helpers
def allocate_console():
    try:
        import ctypes
        ctypes.windll.kernel32.AllocConsole()
    except Exception:
        pass

def free_console():
    try:
        import ctypes
        ctypes.windll.kernel32.FreeConsole()
    except Exception:
        pass

# GUI log handler
class GuiLogHandler(logging.Handler):
    def __init__(self, text_widget):
        super().__init__(level=logging.DEBUG)
        self.text_widget = text_widget
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S"))

    def emit(self, record):
        msg = self.format(record)
        def append():
            try:
                self.text_widget.configure(state="normal")
                self.text_widget.insert(tk.END, msg + "\n")
                self.text_widget.see(tk.END)
                self.text_widget.configure(state="disabled")
            except Exception:
                pass
        try:
            self.text_widget.after(0, append)
        except Exception:
            pass

class ConsoleHandler(logging.StreamHandler):
    def __init__(self):
        super().__init__()
        self.setLevel(logging.DEBUG)

def run_installer_thread(user_name, debug, gui_log_append):
    if debug:
        allocate_console()
        ch = ConsoleHandler()
        ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
        logger.addHandler(ch)
    else:
        free_console()

    def status_cb(msg, level="info"):
        try:
            gui_log_append(msg)
        except Exception:
            pass
        if level == "debug":
            logger.debug(msg)
        elif level == "warning":
            logger.warning(msg)
        elif level == "error":
            logger.error(msg)
        else:
            logger.info(msg)

    try:
        logger.info("Starting installer run (debug=%s)", debug)
        installer_main(user_name=user_name, debug=debug, status_callback=status_cb)
        logger.info("Installer run finished")
    except Exception:
        logger.exception("Installer error occurred")


def installer_main(user_name="UnknownUser", debug=False, status_callback=None):
    """
    Orchestrates the installer flow in the requested order:
      1) Download FFMPEG
      2) Detect and select GPU
      3) Install and apply WireGuard
      4) Deploy rclone and write config
      5) Mount remote folders and ensure mounts are running
      6) Download and install Tdarr
      7) Start Tdarr Node
    """
    def internal_status(msg, level="info"):
        if level == "debug":
            logger.debug(msg)
        elif level == "warning":
            logger.warning(msg)
        elif level == "error":
            logger.error(msg)
        else:
            logger.info(msg)
        if status_callback:
            status_callback(msg, level)

    # Ensure we are elevated
    if not is_admin():
        internal_status("Elevation required. Relaunching as Administrator...", "warning")
        elevate()

    sanitized_user = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in user_name)

    # Prepare directories used by the installer
    rclone_dir = Path("C:/RelaTVity/rclone_isolated")
    media_dir = Path("C:/RelaTVity/Media")
    output_dir = Path("C:/RelaTVity/Tdarr-Output")
    tdarr_install_dir = Path("C:/Tdarr_Updater")

    for d in [BASE_DIR, tdarr_install_dir, media_dir, output_dir, rclone_dir]:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.exception("Failed to ensure directory %s", d)
            internal_status(f"Failed to ensure directory {d}", "warning")

    # ---------------------------------------------------------------------
    # 1) Ensure ffmpeg is available
    # ---------------------------------------------------------------------
    internal_status("Ensuring ffmpeg is available", "info")
    ffmpeg_path = None
    try:
        ffmpeg_path = ensure_ffmpeg(status_cb=internal_status)
        internal_status(f"ffmpeg available at: {ffmpeg_path}", "info")
    except Exception:
        logger.exception("ffmpeg check/install failed")
        internal_status("ffmpeg check/install failed", "warning")

    # ---------------------------------------------------------------------
    # 2) Detect encoders and enumerate GPUs, then prompt selection if needed
    # ---------------------------------------------------------------------
    internal_status("Detecting encoders and enumerating GPUs", "info")
    try:
        encoders = detect_encoders(ffmpeg_path)
    except Exception:
        logger.exception("Failed to detect encoders")
        encoders = {}

    try:
        gpu_list = enumerate_gpus()
    except Exception:
        logger.exception("Failed to enumerate GPUs")
        gpu_list = []

    if not gpu_list:
        chosen_gpu = "UnknownGPU"
    elif len(gpu_list) == 1:
        chosen_gpu = gpu_list[0]
    else:
        payload = json.dumps(gpu_list)
        if status_callback:
            status_callback(f"SELECT_GPU:{payload}", "info")
        _gpu_selection_event.clear()
        _gpu_selection_event.wait(timeout=300)
        chosen_gpu = _gpu_selection_choice.get("value") or gpu_list[0]

    internal_status(f"Selected GPU: {chosen_gpu}", "info")

    # ---------------------------------------------------------------------
    # Prepare canonical node name (persisted or generated)
    # ---------------------------------------------------------------------
    try:
        node_name = read_persisted_node_name()
    except Exception:
        node_name = None

    if not node_name:
        try:
            node_name = build_node_name(prefix=sanitized_user)
            persist_node_name(node_name)
            internal_status(f"Generated node name: {node_name}", "info")
        except Exception:
            logger.exception("Failed to build/persist node name")
            node_name = f"{sanitized_user}_CPU_GPU_{'GPU' if any(encoders.get(k) for k in ('av1_nvenc','hevc_nvenc','av1_qsv')) else 'CPU'}"
            internal_status(f"Using fallback node name: {node_name}", "warning")
    else:
        internal_status(f"Using persisted node name: {node_name}", "info")

    # ---------------------------------------------------------------------
    # 3) Install WireGuard and apply configuration (must be before mounts)
    # ---------------------------------------------------------------------
    internal_status("Installing and applying WireGuard", "info")
    try:
        tunnel_id = sanitize_tunnel_name(node_name)
        wg_conf_path = get_wireguard_config(node_name, tunnel_id, status_cb=internal_status)
        install_wireguard_and_apply(tunnel_id, status_cb=internal_status)
        internal_status("WireGuard installed and applied", "info")
    except Exception:
        logger.exception("WireGuard install/apply failed")
        internal_status("WireGuard install/apply failed", "warning")
        
    # Ensure WinFsp is installed (required for rclone mount on Windows)
    internal_status("Checking WinFsp dependency", "info")
    try:
        # Local import avoids circular import or failed-top-level import issues
        from relaTVity_winfsp import ensure_winfsp_installed, is_winfsp_installed

        if not is_winfsp_installed():
            internal_status("WinFsp not detected; installing...", "info")
            ok_winfsp = ensure_winfsp_installed(status_cb=internal_status)
            if ok_winfsp:
                internal_status("WinFsp installed", "info")
            else:
                internal_status("WinFsp installation failed; rclone mounts may not work", "warning")
        else:
            internal_status("WinFsp already installed", "info")
    except Exception:
        logger.exception("WinFsp check/install step failed")
        internal_status("WinFsp check/install step failed", "warning")
        

    # ---------------------------------------------------------------------
    # 4) Deploy isolated rclone and write config (local + SFTP remotes)
    # ---------------------------------------------------------------------
    internal_status("Deploying isolated rclone and writing config", "info")
    rclone_exe = None
    cfg_path = rclone_dir / "rclone.conf"
    try:
        temp_dir = Path(os.getenv("TEMP", "C:/Temp")) / "relaTVity_temp"
        rclone_exe = ensure_rclone_installed(rclone_dir, temp_dir, status_cb=internal_status)
        if not rclone_exe:
            internal_status("rclone deployment failed", "warning")
        else:
            internal_status(f"rclone deployed to {rclone_exe}", "info")
            # Write minimal local remote
            cfg_text = build_minimal_rclone_config(remote_name="localmedia", remote_type="local", remote_path=str(media_dir))
            write_rclone_config(cfg_path, cfg_text, status_cb=internal_status)
            internal_status("Wrote minimal local rclone remote", "info")

            # Write SFTP remotes (example credentials from PS1)
            remotes = {
                "RelaTVity-Media": {"host": "epqi.co.uk", "user": "tdarr", "pass": "islTK9mbEXHRgaTK0KE0525MDwyr"},
                "RelaTVity-Output": {"host": "epqi.co.uk", "user": "tdarr", "pass": "A8P8x_VtKjWp7P3tfvlNsGcWe1X0"}
            }
            ok_cfg = write_sftp_remotes(cfg_path, remotes, status_cb=internal_status)
            if ok_cfg:
                internal_status("Wrote SFTP remotes to rclone config", "info")
            else:
                internal_status("Failed to write SFTP remotes", "warning")
    except Exception:
        logger.exception("rclone deployment/config failed")
        internal_status("rclone deployment/config failed", "warning")

    # ---------------------------------------------------------------------
    # 5) Mount folders now and ensure mounts are running (start mounts before Tdarr)
    # ---------------------------------------------------------------------
    internal_status("Creating watchdog and starting mounts", "info")
    try:
        watchdog_path = Path("C:/RelaTVity/watchdog.ps1")
        created_watchdog = create_watchdog_script(
            watchdog_path,
            rclone_dir=str(rclone_dir),
            rclone_conf=str(cfg_path),
            media_dir=str(media_dir),
            output_dir=str(output_dir),
            node_dir=str(tdarr_install_dir),
            status_cb=internal_status
        )
        if created_watchdog:
            internal_status("Watchdog script created", "info")
            created_shortcut = create_startup_shortcut(watchdog_path, status_cb=internal_status)
            if created_shortcut:
                internal_status("Startup shortcut created", "info")
            else:
                internal_status("Failed to create startup shortcut", "warning")
        else:
            internal_status("Failed to create watchdog script", "warning")

        # Start mounts immediately (non-blocking) if rclone is available
        if rclone_exe:
            started = start_rclone_mounts_now(rclone_exe, cfg_path, media_dir, output_dir, status_cb=internal_status)
            if started:
                internal_status("rclone mounts started", "info")
            else:
                internal_status("Failed to start rclone mounts immediately; watchdog will attempt mounts at login", "warning")
        else:
            internal_status("rclone not available; cannot start mounts", "warning")

        # Quick verification: list localmedia using the isolated config
        try:
            if rclone_exe and cfg_path.exists():
                cmd = ["--config", str(cfg_path), "lsd", "localmedia:"]
                ok, out, err = run_rclone_command(rclone_exe, cmd, timeout=30)
                if ok:
                    internal_status("rclone localmedia listing succeeded", "info")
                else:
                    internal_status(f"rclone localmedia listing failed: {err.strip() or out.strip()}", "warning")
        except Exception:
            logger.exception("rclone verification failed")
            internal_status("rclone verification failed", "warning")
    except Exception:
        logger.exception("Failed to create/start mounts")
        internal_status("Failed to create/start mounts", "warning")

    # ---------------------------------------------------------------------
    # 6) Download and install Tdarr (only after mounts are started)
    # ---------------------------------------------------------------------
    internal_status("Preparing Tdarr configuration and download", "info")
    try:
        write_tdarr_config(node_name, ffmpeg_path or "", status_cb=internal_status)
        internal_status("Tdarr config written", "info")
    except Exception:
        logger.exception("Failed to write Tdarr config")
        internal_status("Failed to write Tdarr config", "warning")

    tdarr_ok = False
    try:
        if status_callback:
            status_callback("Downloading Tdarr", "info")
        tdarr_ok = download_tdarr(status_cb=status_callback or internal_status)
        if tdarr_ok:
            internal_status("Tdarr downloaded", "info")
        else:
            internal_status("Tdarr download failed", "warning")
    except Exception:
        logger.exception("Tdarr download failed")
        internal_status("Tdarr download failed", "warning")

    # ---------------------------------------------------------------------
    # 7) Start Tdarr Node (start after mounts are up and Tdarr downloaded)
    # ---------------------------------------------------------------------
    try:
        if tdarr_ok:
            # Run the updater/tray installer first (keeps existing behavior)
            run_tdarr_updater_then_tray(status_cb=status_callback or internal_status)

            # Helper: check if a process with the given exe name is running
            def _is_process_running(exe_name: str) -> bool:
                try:
                    import psutil
                    name = exe_name.lower()
                    for p in psutil.process_iter(attrs=("name",)):
                        try:
                            if p.info.get("name") and p.info["name"].lower() == name:
                                return True
                        except Exception:
                            continue
                    return False
                except Exception:
                    # Fallback to PowerShell query if psutil not available
                    try:
                        import subprocess, shlex
                        ps_cmd = f"Get-CimInstance Win32_Process -Filter \"Name='{exe_name}'\" | Select-Object -First 1 ProcessId"
                        proc = subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd],
                                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=False)
                        return bool(proc.stdout.strip())
                    except Exception:
                        return False

            import time, subprocess
            wait_seconds = 0
            max_wait = 120
            node_exe = tdarr_install_dir / "Tdarr_Node.exe"
            alt_node_exe = tdarr_install_dir / "Tdarr_Node" / "Tdarr_Node.exe"
            tray_exe = tdarr_install_dir / "Tdarr_Node_Tray.exe"
            alt_tray_exe = tdarr_install_dir / "Tdarr_Node" / "Tdarr_Node_Tray.exe"

            found_node = None
            found_tray = None

            while wait_seconds < max_wait and not (found_node or found_tray):
                if node_exe.exists():
                    found_node = node_exe
                elif alt_node_exe.exists():
                    found_node = alt_node_exe

                if tray_exe.exists():
                    found_tray = tray_exe
                elif alt_tray_exe.exists():
                    found_tray = alt_tray_exe

                if found_node or found_tray:
                    break

                time.sleep(2)
                wait_seconds += 2

            # Diagnostics if nothing found
            if not found_node and not found_tray:
                try:
                    listing = "\n".join([str(p) for p in tdarr_install_dir.rglob("*") if p.is_file()])
                except Exception:
                    listing = f"Could not enumerate {tdarr_install_dir}"
                logger.warning("Tdarr Node/Tray not found under %s after %ss. Files:\n%s", tdarr_install_dir, max_wait, listing)
                internal_status("Tdarr_Node executable not found after install", "warning")
                tray_started = False
                node_started = False
            else:
                tray_started = False
                node_started = False

                # Start tray only if not already running
                if found_tray:
                    exe_name = found_tray.name
                    if _is_process_running(exe_name):
                        logger.info("Tdarr tray already running, skipping start: %s", exe_name)
                        internal_status(f"Tdarr tray already running: {exe_name}", "info")
                        tray_started = True
                    else:
                        try:
                            proc_tray = subprocess.Popen(
                                [str(found_tray)],
                                cwd=str(found_tray.parent),
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                            )
                            logger.info("Tdarr tray started: %s (pid=%s)", found_tray.name, getattr(proc_tray, "pid", "n/a"))
                            internal_status(f"Tdarr tray started: {found_tray.name}", "info")
                            tray_started = True
                        except Exception:
                            logger.exception("Failed to start Tdarr tray at %s", found_tray)
                            internal_status("Failed to start Tdarr tray", "warning")

                # Start Node only if not already running
                if found_node:
                    exe_name = found_node.name
                    if _is_process_running(exe_name):
                        logger.info("Tdarr Node already running, skipping start: %s", exe_name)
                        internal_status(f"Tdarr Node already running: {exe_name}", "info")
                        node_started = True
                    else:
                        try:
                            proc_node = subprocess.Popen(
                                [str(found_node)],
                                cwd=str(found_node.parent),
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                            )
                            logger.info("Started Tdarr_Node: %s (pid=%s)", found_node, getattr(proc_node, "pid", "n/a"))
                            internal_status("Started Tdarr_Node", "info")
                            node_started = True
                        except Exception:
                            logger.exception("Failed to start Tdarr_Node process at %s", found_node)
                            internal_status("Failed to start Tdarr_Node process", "warning")

                if not tray_started and found_tray:
                    internal_status("Tdarr tray not started", "warning")
                if not node_started and found_node:
                    internal_status("Tdarr node not started", "warning")
        else:
            internal_status("Skipping Tdarr start because download/install failed", "warning")
    except Exception:
        logger.exception("Error while downloading/starting Tdarr")
        internal_status("Error while downloading/starting Tdarr", "warning")

    # Post to Discord (non-blocking) to announce node online only once
    try:
        # Only post if tray or node is running (avoid false positives)
        try:
            running_any = False
            if 'tray_started' in locals() and tray_started:
                running_any = True
            if 'node_started' in locals() and node_started:
                running_any = True
        except Exception:
            running_any = True  # conservative fallback

        if running_any:
            av1_caps = []
            if encoders.get("av1_nvenc"):
                av1_caps.append("svt_av1 (nvenc)")
            if encoders.get("av1_qsv"):
                av1_caps.append("svt_av1 (qsv)")
            if encoders.get("av1_amf"):
                av1_caps.append("svt_av1 (amf)")
            if encoders.get("libaom"):
                av1_caps.append("libaom (CPU)")

            threading.Thread(
                target=lambda: send_discord_node_online(
                    node_name,
                    av1_capabilities=av1_caps,
                    status_text="ACTIVE / CLOAKED",
                    webhook_url=None,
                    status_cb=internal_status
                ),
                daemon=True
            ).start()
        else:
            logger.info("Skipping Discord post because Tdarr tray/node not started")
    except Exception:
        logger.exception("Discord notification failed to start")
        internal_status("Discord notification failed to start", "warning")

    # Finalize
    internal_status("Installer finished.", "info")
    logger.info("Installer finished for node %s", node_name)


class InstallerGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("RelaTVity Node Installer")
        self.geometry("980x700")
        self.resizable(False, False)
        self._create_widgets()
        self._attach_handlers()
        self._gui_log_handler = None

    def _create_widgets(self):
        header = ttk.Frame(self, padding=(12, 12))
        header.pack(fill="x")

        # Logo container (uses PIL if available)
        logo_container = ttk.Frame(header)
        logo_container.pack(side="left", fill="y", padx=(0, 12))

        try:
            # Resolve candidate paths
            candidates = [
                LOGO_PATH.resolve() if isinstance(LOGO_PATH, Path) else Path("logo.png").resolve(),
                Path(__file__).resolve().parent / "logo.png"
            ]
            logo_path = None
            for c in candidates:
                logger.debug("Logo candidate: %s (exists=%s)", str(c), c.exists())
                if c.exists():
                    logo_path = c
                    break

            if logo_path and Image and ImageTk:
                try:
                    img = Image.open(logo_path).convert("RGBA")
                    img = img.resize(LOGO_SIZE, Image.LANCZOS)
                    self._logo_photo = ImageTk.PhotoImage(img)   # keep reference
                    logo_label = ttk.Label(logo_container, image=self._logo_photo)
                    logo_label.pack(side="top", pady=(6, 6))
                    logger.info("Loaded logo from %s", logo_path)
                except Exception as e:
                    logger.exception("Failed to load/resize logo.png: %s", e)
                    logo_label = ttk.Label(logo_container, text="RelaTVity", font=("Segoe UI", 18, "bold"))
                    logo_label.pack(side="top", pady=(6, 6))
            else:
                logger.debug("logo.png not found or PIL missing; showing text fallback. Path tried: %s", str(candidates[0]))
                logo_label = ttk.Label(logo_container, text="RelaTVity", font=("Segoe UI", 18, "bold"))
                logo_label.pack(side="top", pady=(6, 6))
        except Exception:
            logger.exception("Unexpected error while loading logo")
            logo_label = ttk.Label(logo_container, text="RelaTVity", font=("Segoe UI", 18, "bold"))
            logo_label.pack(side="top", pady=(6, 6))

        ttk.Label(logo_container, text="Protocol 1.3.3.7 - The Quiet Viewer", font=("Segoe UI", 9)).pack(side="top")

        # About panel
        about_frame = ttk.Frame(header)
        about_frame.pack(side="right", fill="both", expand=True)

        info = ttk.LabelFrame(about_frame, text="About", padding=(12, 8))
        info.pack(fill="both", expand=True)
        info_text = (
            "This installer prepares directories, installs dependencies, generates a WireGuard "
            "configuration, and registers a watchdog task. Use Debug mode to show live logs."
        )
        lbl = ttk.Label(info, text=info_text, wraplength=420, justify="left")
        lbl.pack()

        # Controls row
        controls = ttk.Frame(self, padding=(12, 8))
        controls.pack(fill="x", padx=12, pady=(6, 0))

        self.debug_var = tk.BooleanVar(value=False)
        debug_chk = ttk.Checkbutton(controls, text="Enable Debug Console", variable=self.debug_var)
        debug_chk.pack(side="left")

        ttk.Label(controls, text="Discord Display Name:").pack(side="left", padx=(12, 4))
        try:
            default_user = os.getlogin()
        except Exception:
            default_user = "UnknownUser"
        self.name_var = tk.StringVar(value=default_user)
        name_entry = ttk.Entry(controls, textvariable=self.name_var, width=28)
        name_entry.pack(side="left")

        self.run_btn = ttk.Button(controls, text="Run Installer", command=self.on_run)
        self.run_btn.pack(side="left", padx=(12, 0))

        self.uninstall_btn = ttk.Button(controls, text="Uninstall Everything", command=self.on_uninstall)
        self.uninstall_btn.pack(side="left", padx=(8, 0))

        self.open_log_btn = ttk.Button(controls, text="Open Log File", command=self.on_open_log)
        self.open_log_btn.pack(side="left", padx=(8, 0))

        self.exit_btn = ttk.Button(controls, text="Exit", command=self.on_exit)
        self.exit_btn.pack(side="right")

        # Progress label
        progress_frame = ttk.Frame(self, padding=(12, 4))
        progress_frame.pack(fill="x", padx=12)
        self._progress_var = tk.StringVar(value="")
        self._progress_label = ttk.Label(progress_frame, textvariable=self._progress_var, font=("Consolas", 10))
        self._progress_label.pack(fill="x")

        # Activity log
        log_frame = ttk.LabelFrame(self, text="Activity Log", padding=(8, 8))
        log_frame.pack(fill="both", expand=True, padx=12, pady=12)

        self.log_text = scrolledtext.ScrolledText(log_frame, state="disabled", wrap="word", font=("Consolas", 10))
        self.log_text.pack(fill="both", expand=True)

        # Attach GUI log handler
        self._gui_log_handler = GuiLogHandler(self.log_text)
        self._gui_log_handler.addFilter(GuiVisibilityFilter())
        logger.addHandler(self._gui_log_handler)

    def _attach_handlers(self):
        self.protocol("WM_DELETE_WINDOW", self.on_exit)

    def _show_gpu_selection_dialog(self, gpu_list):
        """
        Modal dialog that shows a button for each GPU. Sets _gpu_selection_choice and signals event.
        This must be called from the GUI thread.
        """
        try:
            dlg = tk.Toplevel(self)
            dlg.title("Select GPU")
            dlg.transient(self)
            dlg.grab_set()
            dlg.resizable(False, False)

            # Center the dialog over the main window
            self.update_idletasks()
            w = 560
            h = 40 + 40 * min(len(gpu_list), 8)
            x = self.winfo_rootx() + max(0, (self.winfo_width() - w) // 2)
            y = self.winfo_rooty() + max(0, (self.winfo_height() - h) // 2)
            dlg.geometry(f"{w}x{h}+{x}+{y}")

            ttk.Label(dlg, text="Select the GPU to use:", font=("Segoe UI", 10, "bold")).pack(padx=12, pady=(12, 6))

            btn_frame = ttk.Frame(dlg)
            btn_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))

            # Create a button for each GPU
            def make_callback(name):
                def cb():
                    try:
                        _gpu_selection_choice["value"] = name
                        _gpu_selection_event.set()
                    finally:
                        try:
                            dlg.grab_release()
                        except Exception:
                            pass
                        dlg.destroy()
                return cb

            for g in gpu_list:
                # Truncate long names visually but keep full name in callback
                display = g if len(g) <= 64 else (g[:60] + "...")
                b = ttk.Button(btn_frame, text=display, command=make_callback(g))
                b.pack(fill="x", pady=4)

            # Cancel button
            def on_cancel():
                _gpu_selection_choice["value"] = None
                _gpu_selection_event.set()
                try:
                    dlg.grab_release()
                except Exception:
                    pass
                dlg.destroy()

            cancel = ttk.Button(btn_frame, text="Cancel", command=on_cancel)
            cancel.pack(fill="x", pady=(8, 0))

            # Ensure the dialog is modal and waits for user action
            self.wait_window(dlg)
        except Exception:
            logger.exception("GPU selection dialog failed; defaulting to first GPU")
            _gpu_selection_choice["value"] = None
            _gpu_selection_event.set()

    def _append_line(self, text):
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert(tk.END, f"{time.strftime('%Y-%m-%d %H:%M:%S')} INFO: {text}\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state="disabled")
        except Exception:
            pass

    def _set_progress(self, text):
        try:
            self._progress_var.set(text)
        except Exception:
            pass

    def _clear_progress(self):
        try:
            self._progress_var.set("")
        except Exception:
            pass

    def on_run(self):
        self.run_btn.config(state="disabled")
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state="disabled")
        self._clear_progress()

        debug = bool(self.debug_var.get())
        user_name = self.name_var.get().strip() or "UnknownUser"

        def append_to_gui_log(msg):
            # GPU selection prompt (now uses modal button dialog)
            if isinstance(msg, str) and msg.startswith("SELECT_GPU:"):
                payload = msg.split(":", 1)[1]
                try:
                    gpu_list = json.loads(payload)
                except Exception:
                    gpu_list = []
                # Show modal dialog on GUI thread
                try:
                    if gpu_list:
                        # schedule the dialog to run on the main thread
                        self.after(0, lambda: self._show_gpu_selection_dialog(gpu_list))
                        self._append_line("GPU selection requested.")
                    else:
                        _gpu_selection_choice["value"] = None
                        _gpu_selection_event.set()
                        self._append_line("No GPUs found; using default.")
                except Exception:
                    logger.exception("Failed to present GPU selection dialog")
                    _gpu_selection_choice["value"] = None
                    _gpu_selection_event.set()
                    self._append_line("GPU selection failed; using default.")
                return

            # Progress messages
            if isinstance(msg, str) and (msg.startswith("Downloading ") or msg.startswith("Downloaded ") or msg.startswith("Download failed")):
                self._set_progress(msg)
                if msg.startswith("Downloaded ") or msg.startswith("Download failed"):
                    self._append_line(msg)
                    threading.Thread(target=lambda: (time.sleep(1.5), self._clear_progress()), daemon=True).start()
                return

            self._append_line(msg)

        t = threading.Thread(target=lambda: run_installer_thread(user_name, debug, append_to_gui_log), daemon=True)
        t.start()

    def on_uninstall(self):
        node_hint = simpledialog.askstring("Node name (optional)", "Enter the node name to target (optional). Leave blank to auto-detect:", parent=self)
        confirm = simpledialog.askstring("Confirm Uninstall", "Type DELETE to confirm full uninstall and removal of all created files and tasks:", parent=self)
        if not confirm or confirm.strip().upper() != "DELETE":
            self._append_line("Uninstall cancelled by user.")
            return
        threading.Thread(target=lambda: uninstall_all(node_name_hint=node_hint, confirmed=True, status_cb=lambda m, l=None: self._append_line(m)), daemon=True).start()

    def on_open_log(self):
        try:
            if LOG_FILE.exists():
                os.startfile(str(LOG_FILE))
            else:
                messagebox.showinfo("Log file", "Log file not found.")
        except Exception as e:
            messagebox.showerror("Error", f"Unable to open log file: {e}")

    def on_exit(self):
        if messagebox.askokcancel("Exit", "Exit the installer?"):
            self.destroy()

def launch_gui():
    app = InstallerGUI()
    app.mainloop()

if __name__ == "__main__":
    launch_gui()