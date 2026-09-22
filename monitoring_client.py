"""Client for reporting agent events and metrics to the central JABS dashboard."""

import requests
import time
import os
from typing import Optional
from dotenv import load_dotenv
from settings import ENV_PATH, VERSION, AGENT_TYPE, AGENT_KEY

# Setup logging
import logging
logger = logging.getLogger("monitoring")

# Load environment to get dashboard connection info if available
load_dotenv(ENV_PATH)

# Dashboard URL (default to localhost for now, can be configured via env var).
# JABS_SERVER_URL is accepted as a deprecated alias for backward compatibility.
DASHBOARD_URL = os.getenv("JABS_DASHBOARD_URL")


def _auth_headers() -> dict:
    """Headers required to authenticate to the dashboard API."""
    if not AGENT_KEY:
        logger.warning("JABS_AGENT_KEY is not set; dashboard requests will be rejected")
    return {"X-API-Key": AGENT_KEY or ""}


def send_event(
    event_type: str,
    message: str,
    run_id: str = None,
    group_id: str = None,
    job_name: str = None,
    backup_type: str = None,
    group_label: str = None,
    stage: str = None,
    status: str = None,
    duration_seconds: float = None,
    files_backed_up: int = None,
    bytes_backed_up: int = None,
    bytes_compressed: int = None,
    error_code: Optional[int] = None,
    error_message: str = None,
    timestamp: Optional[int] = None
) -> bool:
    """Send an event to the dashboard with all fields at the top level."""
    try:
        payload = {
            "version": VERSION,
            "agent_type": AGENT_TYPE,
            "event_type": event_type,
            "message": message,
            "timestamp": timestamp or int(time.time())
        }

        if run_id is not None:
            payload["run_id"] = run_id
        if group_id is not None:
            payload["group_id"] = group_id
        if job_name is not None:
            payload["job_name"] = job_name
        if backup_type is not None:
            payload["backup_type"] = backup_type
        if group_label is not None:
            payload["group_label"] = group_label
        if stage is not None:
            payload["stage"] = stage
        if status is not None:
            payload["status"] = status
        if duration_seconds is not None:
            payload["duration_seconds"] = duration_seconds
        if files_backed_up is not None:
            payload["files_backed_up"] = files_backed_up
        if bytes_backed_up is not None:
            payload["bytes_backed_up"] = bytes_backed_up
        if bytes_compressed is not None:
            payload["bytes_compressed"] = bytes_compressed
        if error_code is not None:
            payload["error_code"] = error_code
        if error_message is not None:
            payload["error_message"] = error_message

        logger.debug(f"Sending event to dashboard: {event_type} - {message}")

        response = requests.post(
            f"{DASHBOARD_URL}/api/monitoring/events",
            json=payload,
            headers=_auth_headers(),
            timeout=5
        )

        if response.status_code in [200, 201]:
            logger.debug(f"Event sent successfully")
            return True
        else:
            logger.warning(f"Failed to send event: {response.status_code} {response.text}")
            return False

    except requests.exceptions.RequestException as e:
        logger.debug(f"Failed to send event: {e}")
        return False


def send_group_purged(server_group_id: str, message: str = None) -> bool:
    """
    Tell the dashboard that a job group was rotated (purged) out of local storage.

    Marks every backup_jobs row sharing this group_id on the dashboard
    with status='purged' and logs a 'purged' event on each — this does NOT
    delete any dashboard rows. Call this right after a local backup set (and
    its DB records) has actually been deleted (see
    core/backup/common.py:rotate_backups).

    Args:
        server_group_id: The dashboard-side group_id shared by the full
            backup and any incremental/differential children in the set.
        message: Optional human-readable message; defaults to a generic one.

    Returns:
        True if the dashboard acknowledged the purge, False otherwise.
    """
    try:
        payload = {
            "group_id": server_group_id,
            "message": message or "Job group rotated out of local storage",
        }

        response = requests.post(
            f"{DASHBOARD_URL}/api/monitoring/group-purged",
            json=payload,
            headers=_auth_headers(),
            timeout=10
        )

        if response.status_code == 200:
            logger.debug(f"Reported purged job group '{server_group_id}' to dashboard")
            return True

        logger.warning(f"Failed to report purged job group '{server_group_id}': {response.status_code} {response.text}")
        return False

    except requests.exceptions.RequestException as e:
        logger.debug(f"Failed to report purged job group '{server_group_id}': {e}")
        return False


def send_backup_start(
    job_name: str,
    backup_type: str,
    group_id: str,
    group_label: str,
    run_id: str = None
) -> bool:
    return send_event(
        event_type="heartbeat",
        message=f"Starting {backup_type} backup for {job_name}",
        run_id=run_id,
        group_id=group_id,
        job_name=job_name,
        backup_type=backup_type,
        group_label=group_label,
        stage="Starting backup"
    )


def send_backup_stage(
    job_name: str,
    backup_type: str,
    group_id: str,
    group_label: str,
    stage: str,
    run_id: str = None
) -> bool:
    return send_event(
        event_type="heartbeat",
        message=f"Backup {job_name}: {stage}",
        run_id=run_id,
        group_id=group_id,
        job_name=job_name,
        backup_type=backup_type,
        group_label=group_label,
        stage=stage
    )


def send_backup_complete(
    job_name: str,
    backup_type: str,
    group_id: str,
    group_label: str,
    duration_seconds: float,
    run_id: str = None,
    files_backed_up: int = 0,
    bytes_backed_up: int = 0,
    bytes_compressed: int = 0,
    success: bool = True,
    error_message: Optional[str] = None
) -> bool:
    if success:
        event_type = "backup_complete"
        message = "Backup Complete"
        status = "success"
    else:
        event_type = "error"
        message = f"Backup '{job_name}' failed"
        if error_message:
            message += f": {error_message}"
        status = "failed"

    return send_event(
        event_type=event_type,
        message=message,
        run_id=run_id,
        group_id=group_id,
        job_name=job_name,
        backup_type=backup_type,
        group_label=group_label,
        stage="Completed" if success else "Error",
        status=status,
        duration_seconds=duration_seconds,
        files_backed_up=files_backed_up,
        bytes_backed_up=bytes_backed_up,
        bytes_compressed=bytes_compressed,
        error_message=None if success else error_message,
        error_code=None if success else 1
    )


def send_scheduler_check(running_jobs: int = 0) -> bool:
    """
    Send scheduler check event for mini-chart.

    Args:
        running_jobs: Number of jobs triggered during this check

    Returns:
        True if successful, False otherwise
    """
    return send_event(
        event_type="heartbeat",
        message=f"Scheduler check completed. {running_jobs} job(s) triggered.",
        stage="Scheduler check"
    )
