"""Client for sending Uptime Kuma push-monitor heartbeats.

Independent of the JABS dashboard (see monitoring_client.py) — this reports
directly to a self-hosted Uptime Kuma instance's push monitor, so a stopped
scheduler can trigger Uptime Kuma's own alerting (email, Discord, etc.) even
if the JABS dashboard itself is down or unreachable.

Configured via the UPTIME_KUMA_URL environment variable (see .env and
settings.py) — consistent with JABS_DASHBOARD_URL, since the push URL embeds
a secret token in its path (https://host/api/push/<token>) the same way an
API key would. Disabled when UPTIME_KUMA_URL is empty/unset.

Pinged from scheduler.py on every scheduler check (i.e. on the cron cadence
that invokes it, typically every 15 minutes) — not per backup job — so the
push monitor's expected heartbeat interval can be set to match that cadence
and catch a stalled scheduler long before it causes a missed backup.
"""

import logging

import requests
from settings import UPTIME_KUMA_URL

logger = logging.getLogger("uptime_kuma")


def ping_uptime_kuma(status: str, message: str = "") -> bool:
    """Send a push heartbeat to the configured Uptime Kuma push monitor.

    status: "up" or "down"
    Returns True if the ping was sent and accepted, False if skipped
    (not configured) or the request failed. Never raises — a flaky/absent
    Uptime Kuma instance must never break the scheduler run.
    """
    if not UPTIME_KUMA_URL:
        return False

    # Strip any query string the user may have copied from Uptime Kuma's UI
    base_url = UPTIME_KUMA_URL.split("?", 1)[0]
    params = {"status": status, "msg": message, "ping": ""}

    try:
        response = requests.get(base_url, params=params, timeout=10)
        if response.status_code == 200:
            logger.debug(f"Uptime Kuma ping sent: status={status}")
            return True
        logger.warning(f"Uptime Kuma ping failed: {response.status_code} {response.text}")
        return False
    except requests.exceptions.RequestException as e:
        logger.warning(f"Uptime Kuma ping failed: {e}")
        return False

