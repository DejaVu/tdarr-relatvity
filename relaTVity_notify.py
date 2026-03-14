# relaTVity_notify.py
"""
Discord notification helper for RelaTVity.
Provides send_discord_node_online(...) to post an embed when a node comes online.
"""

import os
import json
import time
import logging
import requests
from datetime import datetime, timezone

logger = logging.getLogger("relaTVity_notify")

# Default webhook (use environment variable in production)
DEFAULT_WEBHOOK = os.getenv(
    "DISCORD_WEBHOOK_URL",
    "https://discord.com/api/webhooks/1480631821057130537/Z5euFQKpocHnaYbcsebORx6Gkzq9OHGhDmaYhWAb-JB-cX9Ds6qjjROhvPnQarM3YpUj"
)

# Emoji codepoints (kept as unicode strings)
GEM_EMOJI = "\U0001F48E"
SAT_EMOJI = "\U0001F4E1"
SHD_EMOJI = "\U0001F6E1"
COG_EMOJI = "\u2699"

def _iso_timestamp_utc():
    return datetime.now(timezone.utc).isoformat()

def send_discord_node_online(node_name,
                             av1_capabilities=None,
                             status_text="ACTIVE / CLOAKED",
                             webhook_url=None,
                             timeout=10,
                             status_cb=None):
    """
    Post a Discord embed announcing the node is online.

    Parameters
    - node_name: str, canonical node name
    - av1_capabilities: list[str] or str, e.g. ["svt_av1", "svt_av1_10bit"] or a single string
    - status_text: str, status line to show
    - webhook_url: optional override; if None uses DEFAULT_WEBHOOK
    - timeout: request timeout in seconds
    - status_cb: optional callback(status_message, level) to report progress

    Returns True on success, False on failure.
    """
    url = webhook_url or DEFAULT_WEBHOOK
    if not url:
        if status_cb:
            status_cb("Discord webhook URL not configured", "warning")
        logger.warning("Discord webhook URL not configured")
        return False

    # Normalize capabilities into a code block string
    if av1_capabilities is None:
        code_content = ""
    elif isinstance(av1_capabilities, (list, tuple)):
        code_content = "\n".join(str(x) for x in av1_capabilities)
    else:
        code_content = str(av1_capabilities)

    timestamp = _iso_timestamp_utc()

    fields = [
        {"name": f"{SAT_EMOJI} Node Identity", "value": f"`{node_name}`", "inline": True},
        {"name": f"{SHD_EMOJI} Status", "value": f"`{status_text}`", "inline": True},
    ]
    if code_content:
        fields.append({"name": f"{COG_EMOJI} AV1 Capabilities", "value": f"```{code_content}```", "inline": False})

    embed = {
        "title": f"{GEM_EMOJI} RELATVITY INTEL: Node Online",
        "description": "A new operative has materialized on the network.",
        "color": 16776960,
        "fields": fields,
        "footer": {"text": "Protocol 1.3.3.1 - The Quiet Viewer"},
        "timestamp": timestamp
    }

    payload = {"embeds": [embed]}

    headers = {"Content-Type": "application/json; charset=utf-8"}

    try:
        if status_cb:
            status_cb("Posting node online notification to Discord", "info")
        r = requests.post(url, json=payload, headers=headers, timeout=timeout)
        if r.status_code in (200, 204):
            if status_cb:
                status_cb("Discord notification posted", "info")
            logger.info("Discord notification posted (status=%s)", r.status_code)
            return True
        else:
            logger.warning("Discord webhook returned %s: %s", r.status_code, r.text[:1000])
            if status_cb:
                status_cb(f"Discord webhook returned {r.status_code}", "warning")
            return False
    except Exception as e:
        logger.exception("Failed to post Discord webhook: %s", e)
        if status_cb:
            status_cb("Failed to post Discord webhook", "warning")
        return False