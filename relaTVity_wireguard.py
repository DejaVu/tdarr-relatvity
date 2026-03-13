# relaTVity_wireguard.py
"""
WireGuard helpers: obtain WireGuard config from wg-easy, write config file,
install/start tunnel via local WireGuard executable, create watchdog script,
and register a scheduled task.

This module expects helpers from relaTVity_core:
  - logger
  - BASE_DIR
  - read_persisted_node_name
  - build_node_name
  - persist_node_name
  - run_subprocess and _run_cmd_list from your project (imported below)
"""

import os
import re
import time
import subprocess
from pathlib import Path

import requests

# Import core helpers (ensure relaTVity_core.py is on PYTHONPATH)
from relaTVity_core import (
    logger,
    BASE_DIR,
    read_persisted_node_name,
    build_node_name,
    persist_node_name,
)

# If your project provides run_subprocess/_run_cmd_list, import them; otherwise define minimal wrappers
try:
    from relaTVity_core import run_subprocess, _run_cmd_list  # if provided
except Exception:
    def run_subprocess(cmd, capture=True):
        return subprocess.call(cmd, shell=True)

    def _run_cmd_list(cmd_list):
        try:
            res = subprocess.run(cmd_list, capture_output=True, text=True, check=True)
            return True, res.stdout, res.stderr
        except subprocess.CalledProcessError as e:
            return False, e.stdout, e.stderr

# Paths and defaults
TDARR_INSTALL_PATH = Path("C:/Tdarr_Updater")
WG_CONF_DIR = TDARR_INSTALL_PATH / "WireGuard"
TEMP_DIR = Path(os.getenv("TEMP", "C:/Temp")) / "relaTVity_temp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)
WG_CONF_DIR.mkdir(parents=True, exist_ok=True)

# WireGuard / wg-easy endpoints and credentials (restore your working values)
WG_BASE_HTTPS = "http://vpn.epqi.co.uk:51821"   # forced to HTTP to avoid TLS on this port
WG_BASE_HTTP = "http://vpn.epqi.co.uk:51821"
WG_PASSWORD = "Crystal1"
WG_DOWNLOAD_URL = "https://download.wireguard.com/windows-client/wireguard-amd64-0.5.msi"

# -------------------------
# Helper: canonical node name retrieval
# -------------------------
def get_canonical_node_name(prefix="GiGo"):
    """
    Return the canonical node name used by the system.
    Reads persisted name if present; otherwise builds, persists, and returns it.
    """
    name = read_persisted_node_name()
    if name:
        logger.debug("Using persisted node name: %s", name)
        return name
    name = build_node_name(prefix=prefix)
    try:
        persist_node_name(name)
    except Exception:
        logger.exception("Failed to persist generated node name")
    logger.info("Generated node name: %s", name)
    return name

# -------------------------
# WireGuard API + Install/Start Logic (restored flow with defensive parsing)
# -------------------------
def get_wireguard_config(node_name, tunnel_id, status_cb=None):
    """
    Fetch or create a WireGuard config and save it as <tunnel_id>.conf.
    Returns Path to the written conf on success, or None on failure.
    """
    if status_cb:
        status_cb("Generating VPN Node information", "info")

    # --- ensure node_name and persist to node_name.txt for compatibility/debugging ---
    try:
        # If caller passed a falsy node_name, build/read a canonical one
        if not node_name:
            try:
                node_name = get_canonical_node_name()
            except Exception:
                logger.exception("Failed to compute canonical node name; using fallback")
                node_name = "node-unknown"

        # Minimal sanitisation/coercion
        try:
            node_name = str(node_name).strip() or "node-unknown"
        except Exception:
            node_name = "node-unknown"

        # Persist via existing helper if available (keeps previous behaviour)
        try:
            persist_node_name(node_name)
        except Exception:
            logger.debug("persist_node_name failed or not available")

        # Also write a simple node_name.txt file for external scripts / legacy behaviour
        try:
            node_file = BASE_DIR / "node_name.txt"
            node_file.parent.mkdir(parents=True, exist_ok=True)
            node_file.write_text(node_name, encoding="utf-8")
            logger.debug("Wrote node name to %s: %s", node_file, node_name)
        except Exception:
            logger.exception("Failed to write node_name.txt")
    except Exception:
        logger.exception("Unexpected error while ensuring/persisting node_name")

    # Continue with original flow
    out_dir = TDARR_INSTALL_PATH / "WireGuard"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{tunnel_id}.conf"

    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    def try_base(base_url):
        try:
            s = requests.Session()

            # Authenticate (session cookie)
            r = s.post(f"{base_url}/api/session",
                       json={"password": WG_PASSWORD},
                       headers=headers, timeout=15)
            logger.debug("Session POST %s returned %s / %s", f"{base_url}/api/session", r.status_code, (r.text or "")[:1000])
            r.raise_for_status()

            # Copy cookies into session explicitly if needed (requests.Session already keeps them)
            try:
                from requests.cookies import create_cookie
                for c in r.cookies:
                    newc = create_cookie(
                        name=c.name, value=c.value,
                        domain=c.domain or requests.utils.urlparse(base_url).hostname,
                        path=c.path or "/"
                    )
                    s.cookies.set_cookie(newc)
            except Exception:
                logger.exception("Cookie copy failed")

            # Try to find existing client
            client_id = None
            endpoints = [
                "/api/wireguard/clients",
                "/api/wireguard/client",
                "/api/wireguard",
                "/api/clients",
                "/api/client",
            ]
            candidates = []
            for ep in endpoints:
                try:
                    rlist = s.get(f"{base_url}{ep}", headers=headers, timeout=15)
                    logger.debug("Client list GET %s returned %s / %s", f"{base_url}{ep}", rlist.status_code, (rlist.text or "")[:1000])
                    if rlist.status_code != 200:
                        continue
                    try:
                        jl = rlist.json()
                    except ValueError:
                        continue

                    if isinstance(jl, list):
                        candidates = jl
                    elif isinstance(jl, dict):
                        candidates = jl.get("clients") or jl.get("data") or jl.get("items") or []
                        if not candidates:
                            candidates = [v for v in jl.values() if isinstance(v, dict)]
                    else:
                        candidates = []

                    for c in candidates:
                        if not isinstance(c, dict):
                            continue
                        name = str(c.get("name") or c.get("clientName") or c.get("displayName") or "")
                        if name and name.lower() == node_name.lower():
                            client_id = c.get("id") or c.get("client_id") or c.get("uuid")
                            break
                    if client_id:
                        break
                except Exception:
                    logger.exception("Error querying %s", ep)

            # Create client if missing (try common endpoints)
            if not client_id:
                try:
                    payload_name = str(node_name) if node_name else "node-unknown"
                    rcreate = s.post(f"{base_url}/api/wireguard/client",
                                     json={"name": payload_name},
                                     headers=headers, timeout=15)
                    logger.debug("Create client POST %s returned %s / %s", f"{base_url}/api/wireguard/client", getattr(rcreate, "status_code", None), (getattr(rcreate, "text", "") or "")[:1000])
                except Exception:
                    rcreate = None

                jr = None
                if rcreate is not None:
                    try:
                        jr = rcreate.json()
                    except Exception:
                        jr = None

                if jr:
                    client_id = jr.get("id") or (jr.get("client") or {}).get("id") or (jr.get("data") or {}).get("id")

                if not client_id and rcreate is not None:
                    loc = rcreate.headers.get("Location") or rcreate.headers.get("location")
                    if loc:
                        m = re.search(r"([^/]+)$", loc)
                        if m:
                            client_id = m.group(1)

            if not client_id:
                # As a last resort, if we have candidates from earlier, pick the first
                if candidates:
                    first = candidates[0]
                    if isinstance(first, dict):
                        client_id = first.get("id") or first.get("client_id") or first.get("uuid")

            if not client_id:
                logger.warning("No client id found or created at %s", base_url)
                return False

            # Try config endpoints (various common patterns)
            base = base_url.rstrip("/")
            cfg_urls = [
                f"{base}/api/wireguard/client/{client_id}/configuration",
                f"{base}/api/wireguard/client/{client_id}/configuration?download=true",
                f"{base}/api/wireguard/client/{client_id}/config",
                f"{base}/api/wireguard/client/{client_id}/download",
                f"{base}/api/wireguard/client/{client_id}.conf",
                f"{base}/static/wireguard/client/{client_id}.conf",
                f"{base}/api/clients/{client_id}/config",
                f"{base}/api/client/{client_id}/config",
                f"{base}/api/client/{client_id}/configuration",
            ]

            for url in cfg_urls:
                try:
                    rconf = s.get(url, headers=headers, timeout=15)
                    logger.debug("Config GET %s returned %s / %s", url, getattr(rconf, "status_code", None), (getattr(rconf, "text", "") or "")[:1000])
                    if getattr(rconf, "status_code", None) == 200:
                        body = rconf.text or ""
                        if body.lstrip().startswith("[Interface") or body.lstrip().startswith("[Interface]"):
                            out_path.write_text(body, encoding="utf-8")
                            logger.info("Wrote WireGuard config to %s", out_path)
                            return out_path
                        # Some endpoints return JSON with config inside
                        try:
                            jr = rconf.json()
                            for k in ("conf", "config", "wg_conf", "wgconf", "configuration"):
                                v = jr.get(k)
                                if isinstance(v, str) and v.strip().startswith("[Interface"):
                                    out_path.write_text(v, encoding="utf-8")
                                    logger.info("Wrote WireGuard config to %s", out_path)
                                    return out_path
                            if isinstance(jr.get("data"), dict):
                                for k in ("conf", "config", "wg_conf"):
                                    v = jr["data"].get(k)
                                    if isinstance(v, str) and v.strip().startswith("[Interface"):
                                        out_path.write_text(v, encoding="utf-8")
                                        logger.info("Wrote WireGuard config to %s", out_path)
                                        return out_path
                        except Exception:
                            pass
                except Exception:
                    logger.exception("Error fetching %s", url)

            return False

        except Exception:
            logger.exception("WireGuard API error")
            return False

    # Try HTTPS then HTTP (match previous working flow)
    try:
        if try_base(WG_BASE_HTTPS):
            return out_path
    except Exception:
        logger.exception("HTTPS base attempt failed")

    try:
        if try_base(WG_BASE_HTTP):
            return out_path
    except Exception:
        logger.exception("HTTP base attempt failed")

    logger.warning("WireGuard config retrieval failed.")
    return None

# -------------------------
# Download / install / apply tunnel using local WireGuard executable
# -------------------------
def download_with_progress(url, dest_path, status_cb=None, timeout=60):
    try:
        logger.info("Downloading %s -> %s", url, dest_path)
        if status_cb:
            status_cb("Downloading VPN", "info")
        resp = requests.get(url, stream=True, timeout=30)
        resp.raise_for_status()
        with open(dest_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                fh.write(chunk)
        logger.info("Downloaded %s", dest_path)
        return True
    except Exception:
        logger.exception("Failed to download %s", url)
        if status_cb:
            status_cb("Failed to download WireGuard installer", "warning")
        return False


def install_wireguard_and_apply(tunnel_id, status_cb=None):
    if status_cb:
        status_cb("Applying VPN tunnel", "info")

    wg_exe = Path("C:/Program Files/WireGuard/wireguard.exe")
    msi_path = TEMP_DIR / "wireguard-installer.msi"

    if not wg_exe.exists():
        if download_with_progress(WG_DOWNLOAD_URL, msi_path, status_cb=status_cb):
            try:
                run_subprocess(f'msiexec /i "{msi_path}" /qn /norestart')
                time.sleep(3)
            except Exception:
                logger.exception("Failed to run msiexec for WireGuard installer")
        try:
            msi_path.unlink(missing_ok=True)
        except Exception:
            pass

    if not wg_exe.exists():
        if status_cb:
            status_cb("WireGuard executable missing; cannot apply tunnel", "warning")
        return

    wg_conf = TDARR_INSTALL_PATH / "WireGuard" / f"{tunnel_id}.conf"
    if not wg_conf.exists():
        if status_cb:
            status_cb("WireGuard config missing; cannot apply tunnel", "warning")
        return

    ok_install, out_i, err_i = _run_cmd_list([str(wg_exe), "/installtunnelservice", str(wg_conf)])
    if not ok_install:
        logger.warning("WireGuard install returned non-zero; stdout=%s stderr=%s", out_i, err_i)
        if status_cb:
            status_cb("WireGuard install returned an error; attempting to start tunnel anyway", "warning")

    ok_start, out_s, err_s = _run_cmd_list([str(wg_exe), "/starttunnelservice", tunnel_id])
    if ok_start:
        if status_cb:
            status_cb("WireGuard tunnel applied and started", "info")
        return

    ok, out, err = _run_cmd_list([str(wg_exe), "/listtunnels"])
    if not ok:
        if status_cb:
            status_cb("Could not list WireGuard tunnels", "warning")
        return

    tunnels = [line.strip() for line in out.splitlines() if line.strip()]
    candidates = []

    if tunnel_id in tunnels:
        candidates = [tunnel_id]
    else:
        for t in tunnels:
            if tunnel_id.lower() in t.lower():
                candidates.append(t)

        if not candidates:
            tokens = [tok for tok in re.split(r"[-_]", tunnel_id) if tok]
            for t in tunnels:
                low = t.lower()
                if any(tok.lower() in low for tok in tokens):
                    candidates.append(t)

    seen = set()
    candidates = [c for c in candidates if not (c in seen or seen.add(c))]

    for cand in candidates:
        ok2, out2, err2 = _run_cmd_list([str(wg_exe), "/starttunnelservice", cand])
        if ok2:
            if status_cb:
                status_cb(f"WireGuard tunnel started: {cand}", "info")
            return

    if status_cb:
        status_cb("Failed to start any WireGuard tunnel", "error")


# -------------------------
# Watchdog and scheduled task helpers
# -------------------------
def create_watchdog(tunnel_id, status_cb=None):
    try:
        script_path = BASE_DIR / "relaTVity_maint.ps1"
        script = f"""
# RelaTVity watchdog - ensure WireGuard tunnel {tunnel_id} is running
try {{
    $wgExe = "C:\\Program Files\\WireGuard\\wireguard.exe"
    if (Test-Path $wgExe) {{
        $tunnels = & $wgExe /listtunnels 2>&1
        if ($tunnels -notmatch "{tunnel_id}") {{
            & $wgExe /starttunnelservice "{tunnel_id}" 2>&1
        }}
    }}
}} catch {{
}}
"""
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(script)
        logger.debug("Watchdog script written to %s", script_path)
        if status_cb:
            status_cb("Watchdog script created", "info")
        return script_path
    except Exception:
        logger.exception("Failed to create watchdog script")
        if status_cb:
            status_cb("Failed to create watchdog script", "warning")
        return None


def register_scheduled_task(script_path, status_cb=None):
    try:
        task_name = "RelaTVityMaintenance"
        cmd = [
            "schtasks",
            "/Create",
            "/SC", "MINUTE",
            "/MO", "5",
            "/TN", task_name,
            "/TR", f'powershell -ExecutionPolicy Bypass -File "{str(script_path)}"',
            "/F"
        ]
        ok, out, err = _run_cmd_list(cmd)
        if ok:
            logger.debug("Scheduled task registered: %s", task_name)
            if status_cb:
                status_cb("Scheduled task registered", "info")
            return True
        else:
            logger.warning("Failed to register scheduled task: %s", err)
            if status_cb:
                status_cb("Failed to register scheduled task", "warning")
            return False
    except Exception:
        logger.exception("Failed to register scheduled task")
        if status_cb:
            status_cb("Failed to register scheduled task", "warning")
        return False