# relaTVity_rclone.py
"""
RelaTVity rclone helpers

Provides:
 - ensure_rclone_installed(rclone_dir, temp_dir, status_cb)
 - find_rclone_executable(rclone_dir)
 - build_minimal_rclone_config(remote_name, remote_type, remote_path)
 - write_rclone_config(cfg_path, cfg_text, status_cb)
 - write_sftp_remotes(cfg_path, remotes, status_cb)
 - run_rclone_command(rclone_exe, args, timeout=None)
 - start_rclone_mounts_now(rclone_exe, cfg_path, media_dir, output_dir, status_cb)
 - create_watchdog_script(watchdog_path, rclone_dir, rclone_conf, media_dir, output_dir, node_dir, status_cb)
 - create_startup_shortcut(script_path, status_cb)

This module implements the behaviour from your PowerShell phases:
 - deploy isolated rclone binary if missing
 - inject credentials into rclone.conf
 - fabricate a PowerShell watchdog that restarts mounts and stops Tdarr_Node if mounts disappear
 - establish mounts and start them if not present
 - provide helpers used by the GUI installer
"""

from pathlib import Path
import requests
import zipfile
import shutil
import subprocess
import logging
import os
import time
import sys

from typing import Optional, List, Tuple, Dict, Callable

logger = logging.getLogger(__name__)

RCLONE_DOWNLOAD_URL = "https://downloads.rclone.org/rclone-current-windows-amd64.zip"
DEFAULT_RCLONE_EXE_NAME = "rclone.exe"

def find_rclone_executable(rclone_dir: Path) -> Optional[Path]:
    """
    Return Path to rclone.exe inside rclone_dir if present, otherwise None.
    """
    try:
        r = Path(rclone_dir) / DEFAULT_RCLONE_EXE_NAME
        if r.exists():
            return r
        # try to find any rclone.exe under the dir
        for p in Path(rclone_dir).rglob("rclone.exe"):
            return p
    except Exception:
        logger.exception("Error while searching for rclone executable in %s", rclone_dir)
    return None

def ensure_rclone_installed(rclone_dir: Path, temp_dir: Path, status_cb=None) -> Optional[Path]:
    """
    Ensure an isolated rclone binary exists under rclone_dir.
    If missing, download the official zip, extract rclone.exe and place it in rclone_dir.
    Returns Path to rclone executable on success, or None on failure.
    """
    try:
        rclone_dir = Path(rclone_dir)
        temp_dir = Path(temp_dir)
        rclone_dir.mkdir(parents=True, exist_ok=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception("Failed to ensure rclone/temp directories")
        if status_cb:
            status_cb("Failed to prepare rclone directories", "warning")
        return None

    rclone_exe = find_rclone_executable(rclone_dir)
    if rclone_exe:
        logger.debug("Found existing rclone at %s", rclone_exe)
        if status_cb:
            status_cb(f"rclone deployed to {rclone_exe}", "info")
        return rclone_exe

    # Download and extract
    try:
        if status_cb:
            status_cb("Deploying Isolated rclone Binary", "info")
        zip_path = temp_dir / "rclone.zip"
        logger.info("Downloading rclone zip to %s", zip_path)
        resp = requests.get(RCLONE_DOWNLOAD_URL, stream=True, timeout=60)
        resp.raise_for_status()
        with open(zip_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    fh.write(chunk)

        # Extract and move rclone.exe
        with zipfile.ZipFile(zip_path, "r") as z:
            # find rclone.exe inside the archive
            candidates = [n for n in z.namelist() if n.lower().endswith("/rclone.exe") or n.lower().endswith("\\rclone.exe") or n.lower().endswith("rclone.exe")]
            if not candidates:
                logger.error("rclone.exe not found inside downloaded zip")
                if status_cb:
                    status_cb("rclone binary not found in archive", "warning")
                return None
            # pick the first candidate
            member = candidates[0]
            # extract to temp_dir preserving subdirs
            z.extract(member, path=temp_dir)
            extracted_path = Path(temp_dir) / member
            # if extracted is inside a subdir, find the actual rclone.exe file
            if extracted_path.is_dir():
                found = None
                for p in extracted_path.rglob("rclone.exe"):
                    found = p
                    break
                if not found:
                    logger.error("rclone.exe not found after extraction")
                    if status_cb:
                        status_cb("rclone binary not found after extraction", "warning")
                    return None
                src = found
            else:
                # member may include subdir; normalize to actual file path
                src = extracted_path
                if not src.exists():
                    # fallback: search temp_dir for rclone.exe
                    found = None
                    for p in Path(temp_dir).rglob("rclone.exe"):
                        found = p
                        break
                    if not found:
                        logger.error("rclone.exe not found after extraction (fallback)")
                        if status_cb:
                            status_cb("rclone binary not found after extraction", "warning")
                        return None
                    src = found

            dest = rclone_dir / DEFAULT_RCLONE_EXE_NAME
            shutil.move(str(src), str(dest))
            # ensure executable bit (on Windows not necessary, but harmless)
            try:
                dest.chmod(0o755)
            except Exception:
                pass

        # cleanup temp_dir
        try:
            shutil.rmtree(temp_dir)
        except Exception:
            pass

        if status_cb:
            status_cb(f"rclone deployed to {dest}", "info")
        logger.info("rclone deployed to %s", dest)
        return dest
    except Exception:
        logger.exception("Failed to deploy rclone")
        if status_cb:
            status_cb("Failed to deploy rclone", "warning")
        return None

def build_minimal_rclone_config(remote_name: str = "localmedia", remote_type: str = "local", remote_path: str = "C:/RelaTVity/Media") -> str:
    """
    Build a minimal rclone config text for a local remote.
    """
    cfg = f"[{remote_name}]\n"
    cfg += f"type = {remote_type}\n"
    if remote_type == "local":
        cfg += f"nounc = true\n"
        cfg += f"root_folder_id = \n"
        cfg += f"remote = {remote_path}\n"
    else:
        # generic placeholder
        cfg += f"remote = {remote_path}\n"
    return cfg


def write_rclone_config(cfg_path: Path, cfg_text: str, status_cb=None) -> bool:
    """
    Write the provided rclone config text to cfg_path.
    """
    try:
        cfg_path = Path(cfg_path)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(cfg_text, encoding="utf-8")
        logger.info("Wrote rclone config to %s", cfg_path)
        if status_cb:
            status_cb(f"Wrote rclone config to {cfg_path}", "info")
        return True
    except Exception:
        logger.exception("Failed to write rclone config to %s", cfg_path)
        if status_cb:
            status_cb("Failed to write rclone config", "warning")
        return False


def write_sftp_remotes(cfg_path: Path, remotes: Dict[str, Dict], status_cb=None) -> bool:
    """
    Write SFTP remotes into the rclone config file.
    If a config already exists, append the new remotes instead of overwriting,
    so previously written sections (e.g. localmedia) are preserved.
    remotes: dict mapping remote_name -> {host, user, pass, ...}
    """
    try:
        cfg_path = Path(cfg_path)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)

        # Build the remotes text
        lines = []
        for name, info in remotes.items():
            lines.append(f"[{name}]")
            lines.append("type = sftp")
            host = info.get("host") or info.get("hostname") or ""
            user = info.get("user") or info.get("username") or ""
            passwd = info.get("pass") or info.get("password") or ""
            if host:
                lines.append(f"host = {host}")
            if user:
                lines.append(f"user = {user}")
            if passwd:
                lines.append(f"pass = {passwd}")
            # include flags from your PS snippet
            lines.append("key_use_agent = false")
            lines.append("use_insecure_cipher = true")
            lines.append("shell_type = unix")
            lines.append("")  # blank line between remotes

        new_section = "\n".join(lines).rstrip() + "\n"

        # If file exists, append; otherwise create new file
        if cfg_path.exists():
            try:
                existing = cfg_path.read_text(encoding="utf-8")
            except Exception:
                existing = ""
            # Avoid duplicating sections: if a remote already exists, skip adding it
            for name in remotes.keys():
                if f"[{name}]" in existing:
                    # remove that remote from new_section to avoid duplicate sections
                    # simple approach: skip adding that remote entirely
                    # rebuild new_section excluding existing remotes
                    parts = []
                    for nm, info in remotes.items():
                        if f"[{nm}]" in existing:
                            continue
                        parts.append(nm)
                    # if all remotes already present, nothing to do
                    if not parts:
                        if status_cb:
                            status_cb("Rclone remotes already present in config", "info")
                        logger.debug("All requested SFTP remotes already present in %s", cfg_path)
                        return True
                    # rebuild new_section for only missing remotes
                    lines = []
                    for nm in parts:
                        info = remotes[nm]
                        lines.append(f"[{nm}]")
                        lines.append("type = sftp")
                        host = info.get("host") or info.get("hostname") or ""
                        user = info.get("user") or info.get("username") or ""
                        passwd = info.get("pass") or info.get("password") or ""
                        if host:
                            lines.append(f"host = {host}")
                        if user:
                            lines.append(f"user = {user}")
                        if passwd:
                            lines.append(f"pass = {passwd}")
                        lines.append("key_use_agent = false")
                        lines.append("use_insecure_cipher = true")
                        lines.append("shell_type = unix")
                        lines.append("")
                    new_section = "\n".join(lines).rstrip() + "\n"

            combined = existing.rstrip() + "\n\n" + new_section
            cfg_path.write_text(combined, encoding="utf-8")
            logger.info("Appended SFTP remotes to existing rclone config %s", cfg_path)
            if status_cb:
                status_cb("Appended SFTP remotes to rclone config", "info")
            return True
        else:
            # No existing config — write new file with the remotes
            cfg_path.write_text(new_section, encoding="utf-8")
            logger.info("Wrote new rclone config with SFTP remotes to %s", cfg_path)
            if status_cb:
                status_cb("Wrote rclone config with SFTP remotes", "info")
            return True
    except Exception:
        logger.exception("Failed to write SFTP remotes to %s", cfg_path)
        if status_cb:
            status_cb("Failed to write SFTP remotes", "warning")
        return False


def run_rclone_command(rclone_exe: Path, args: List[str], timeout: Optional[int] = None) -> Tuple[bool, str, str]:
    """
    Run rclone with the provided args (list) and return (ok, stdout, stderr).
    """
    try:
        cmd = [str(rclone_exe)] + args
        logger.debug("Running rclone command: %s", " ".join(cmd))
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        ok = proc.returncode == 0
        return ok, proc.stdout or "", proc.stderr or ""
    except Exception as e:
        logger.exception("rclone command failed: %s", e)
        return False, "", str(e)


def start_rclone_mounts_now(rclone_exe: Optional[Path], cfg_path: Path, media_dir: Path, output_dir: Path, status_cb=None) -> bool:
    """
    Start the two mounts described in your PowerShell:
      - RelaTVityServer:/mnt/superfs/relatvity_ro -> media_dir
      - RelaTVityServer:/srv/localfs/local/automation/tdarr-output -> output_dir

    This function will:
      - ensure local directories exist
      - if the mount appears empty (no files), start rclone mount processes (detached)
      - return True if at least one mount process was started or mounts already present
    """
    try:
        if rclone_exe is None:
            rclone_exe = find_rclone_executable(Path(cfg_path).parent)
            if rclone_exe is None:
                logger.warning("rclone executable not found; cannot start mounts")
                if status_cb:
                    status_cb("rclone not available; cannot start mounts", "warning")
                return False

        media_dir = Path(media_dir)
        output_dir = Path(output_dir)
        media_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        mounts = [
            ("RelaTVityServer:/mnt/superfs/relatvity_ro", str(media_dir)),
            ("RelaTVityServer:/srv/localfs/local/automation/tdarr-output", str(output_dir)),
        ]

        started_any = False
        for remote, local in mounts:
            # If local dir has no files, assume not mounted
            try:
                has_files = any(Path(local).iterdir())
            except Exception:
                has_files = False

            if not has_files:
                # Start mount as detached process
                args = [
                    str(rclone_exe),
                    "mount",
                    remote,
                    local,
                    "--config", str(cfg_path),
                    "--vfs-cache-mode", "full",
                    "--no-console",
                ]
                # On Windows, use creationflags to hide window
                try:
                    subprocess.Popen(
                        args,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        stdin=subprocess.DEVNULL,
                        shell=False,
                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                    )
                except Exception:
                    # fallback without creationflags
                    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, shell=False)
                logger.info("Started rclone mount: %s -> %s", remote, local)
                if status_cb:
                    status_cb(f"Started rclone mount: {local}", "info")
                started_any = True
                # small delay between mounts
                time.sleep(1)
            else:
                logger.debug("Local path %s already contains files; assuming mount present", local)
                if status_cb:
                    status_cb(f"Mount appears present: {local}", "info")

        # Return True if mounts already present or we started any
        return True if (started_any or True) else False
    except Exception:
        logger.exception("Failed to start rclone mounts")
        if status_cb:
            status_cb("Failed to start rclone mounts", "warning")
        return False




def create_watchdog_script(
    watchdog_path: Path,
    rclone_dir: str,
    rclone_conf: str,
    media_dir: str,
    output_dir: str,
    node_dir: Optional[str] = None,
    status_cb: Optional[Callable[[str, str], None]] = None
) -> Optional[Path]:
    """
    Create a PowerShell watchdog script that:
      - checks whether media_dir and output_dir contain files
      - if not, stops Tdarr_Node, kills rclone processes running from the RelaTVity dir,
        and restarts the mounts using the isolated rclone binary and config
      - loops forever with sleeps (30s between checks, 15s after restart)
    The generated script self-relaunches hidden when run interactively and writes a transcript log.
    Returns the path to the written script on success, or None on failure.
    """
    try:
        watchdog_path = Path(watchdog_path)
        watchdog_path.parent.mkdir(parents=True, exist_ok=True)

        node_process = "Tdarr_Node"

        # Use $PSCommandPath so the script can relaunch itself hidden reliably.
        # The script accepts a -Hidden switch to avoid relaunch recursion.
        script = f'''param([switch]$Hidden)

# If not launched with -Hidden, relaunch this script hidden and exit the original process.
if (-not $Hidden) {{
    $psExe = (Get-Command powershell.exe).Source
    $args = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`" -Hidden"
    Start-Process -FilePath $psExe -ArgumentList $args -WindowStyle Hidden
    exit
}}

# Start transcript for reliable logging in non-interactive sessions
$LogFile = Join-Path (Split-Path -Parent $PSCommandPath) "watchdog.log"
Start-Transcript -Path $LogFile -Append -Force
Write-Output "$(Get-Date -Format o) - watchdog started (hidden)"

$BaseDir = "{Path(node_dir or Path.cwd())}"
$RcloneDir = "{rclone_dir}"
$RcloneConf = "{rclone_conf}"
$NodeProcess = "{node_process}"
$MediaDir = "{media_dir}"
$OutputDir = "{output_dir}"

Do {{
    try {{
        # Check if folders exist and contain items
        $MediaCheck = (Test-Path "$MediaDir") -and (Get-ChildItem "$MediaDir" -ErrorAction SilentlyContinue)
        $OutputCheck = (Test-Path "$OutputDir") -and (Get-ChildItem "$OutputDir" -ErrorAction SilentlyContinue)

        if (-not $MediaCheck -or -not $OutputCheck) {{
            Write-Output "$(Get-Date -Format o) - mounts appear missing or empty; performing recovery steps"

            # 1. STOP THE NODE WORKER (Stop processing if mounts fail)
            Get-Process -Name $NodeProcess -ErrorAction SilentlyContinue | ForEach-Object {{
                try {{ $_ | Stop-Process -Force -ErrorAction SilentlyContinue }} catch {{ Write-Output "Failed to stop process $($_.Id): $($_ | Out-String)" }}
            }}

            # 2. ISOLATED RCLONE RESET
            # Kill ONLY the rclone instances running from the RelaTVity directory
            Get-Process -Name rclone -ErrorAction SilentlyContinue | Where-Object {{ $_.Path -like "*RelaTVity*" }} | ForEach-Object {{
                try {{ $_ | Stop-Process -Force -ErrorAction SilentlyContinue }} catch {{ Write-Output "Failed to stop rclone process $($_.Id): $($_ | Out-String)" }}
            }}

            # 3. RE-ESTABLISH MOUNTS using the isolated rclone binary and config
            $rcloneExe = Join-Path "{rclone_dir}" "rclone.exe"
            if (Test-Path $rcloneExe) {{
                try {{
                    Start-Process -FilePath $rcloneExe -ArgumentList "mount", "RelaTVityServer:/mnt/superfs/relatvity_ro", "$MediaDir", "--config", "{rclone_conf}", "--vfs-cache-mode", "full", "--no-console" -WindowStyle Hidden
                    Start-Process -FilePath $rcloneExe -ArgumentList "mount", "RelaTVityServer:/srv/localfs/local/automation/tdarr-output", "$OutputDir", "--config", "{rclone_conf}", "--vfs-cache-mode", "full", "--no-console" -WindowStyle Hidden
                    Write-Output "$(Get-Date -Format o) - rclone mount commands started"
                }} catch {{
                    Write-Output "$(Get-Date -Format o) - Failed to start rclone mounts: $($_.Exception.Message)"
                }}
            }} else {{
                Write-Output "$(Get-Date -Format o) - rclone executable not found at {rclone_dir}\\rclone.exe"
            }}

            Start-Sleep -Seconds 15
        }}
    }} catch {{
        Write-Output "$(Get-Date -Format o) - Exception in watchdog loop: $($_.Exception.Message)"
    }}

    # Sleep between checks
    Start-Sleep -Seconds 30
}} while ($true)

Write-Output "$(Get-Date -Format o) - watchdog exiting (unexpected)"
Stop-Transcript
'''

        watchdog_path.write_text(script, encoding="utf-8")
        logger.info("Watchdog script written to %s", watchdog_path)
        if status_cb:
            try:
                status_cb("Watchdog script created", "info")
            except Exception:
                logger.exception("status_cb raised an exception")
        return watchdog_path
    except Exception:
        logger.exception("Failed to create watchdog script")
        if status_cb:
            try:
                status_cb("Failed to create watchdog script", "warning")
            except Exception:
                logger.exception("status_cb raised an exception")
        return None

def create_startup_shortcut(script_path: Path, status_cb=None) -> bool:
    """
    Create a startup shortcut to run the provided script at user login.
    On Windows, attempts to create a .lnk in the user's Startup folder.
    If winshell or pythoncom is not available, falls back to writing a small .bat in Startup.
    Returns True on success.
    """
    try:
        startup_dir = Path(os.getenv("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        startup_dir.mkdir(parents=True, exist_ok=True)
        script_path = Path(script_path)

        # Try to create a .lnk using pythoncom if available
        try:
            import pythoncom  # type: ignore
            from win32com.shell import shell, shellcon  # type: ignore
            from win32com.client import Dispatch  # type: ignore

            shortcut_path = startup_dir / (script_path.stem + ".lnk")
            shell_link = Dispatch('WScript.Shell').CreateShortcut(str(shortcut_path))
            shell_link.TargetPath = str(sys.executable)
            shell_link.Arguments = f'-NoProfile -ExecutionPolicy Bypass -File "{str(script_path)}"'
            shell_link.WorkingDirectory = str(script_path.parent)
            shell_link.WindowStyle = 7  # hidden
            shell_link.IconLocation = str(sys.executable)
            shell_link.save()
            logger.info("Startup shortcut created at %s", shortcut_path)
            if status_cb:
                status_cb("Startup shortcut created", "info")
            return True
        except Exception:
            # Fallback: create a .bat that launches powershell with the script
            bat_path = startup_dir / (script_path.stem + ".bat")
            bat_content = f'@echo off\r\nstart "" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{str(script_path)}"\r\n'
            bat_path.write_text(bat_content, encoding="utf-8")
            logger.info("Startup batch created at %s", bat_path)
            if status_cb:
                status_cb("Startup batch created", "info")
            return True
    except Exception:
        logger.exception("Failed to create startup shortcut")
        if status_cb:
            status_cb("Failed to create startup shortcut", "warning")
        return False