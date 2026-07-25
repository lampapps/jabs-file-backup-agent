"""Client for reporting agent events and metrics to the central JABS server."""

import requests
import socket
import time
import os
from typing import Optional
from dotenv import load_dotenv
from settings import ENV_PATH, VERSION, AGENT_TYPE

# Setup logging
import logging
logger = logging.getLogger("monitoring")

# Load environment to get server connection info if available
load_dotenv(ENV_PATH)

# Server URL (default to localhost for now, can be configured via env var)
SERVER_URL = os.getenv("JABS_SERVER_URL", "http://localhost:5001")


def get_hostname() -> str:
    """Get the machine hostname."""
    return socket.gethostname()


def get_ip_address() -> str:
    """Get the primary IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def send_event(
    event_type: str,
    message: str,
    run_id: str = None,
    backup_set_id: str = None,
    job_name: str = None,
    backup_type: str = None,
    backup_set_name: str = None,
    encrypt: bool = None,
    sync: bool = None,
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
    """Send an event to the server with all fields at the top level."""
    try:
        payload = {
            "hostname": get_hostname(),
            "ip_address": get_ip_address(),
            "version": VERSION,
            "agent_type": AGENT_TYPE,
            "event_type": event_type,
            "message": message,
            "timestamp": timestamp or int(time.time())
        }

        if run_id is not None:
            payload["run_id"] = run_id
        if backup_set_id is not None:
            payload["backup_set_id"] = backup_set_id
        if job_name is not None:
            payload["job_name"] = job_name
        if backup_type is not None:
            payload["backup_type"] = backup_type
        if backup_set_name is not None:
            payload["backup_set_name"] = backup_set_name
        if encrypt is not None:
            payload["encrypt"] = encrypt
        if sync is not None:
            payload["sync"] = sync
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

        logger.debug(f"Sending event to server: {event_type} - {message}")

        response = requests.post(
            f"{SERVER_URL}/api/monitoring/events",
            json=payload,
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


def send_backup_start(
    job_name: str,
    backup_type: str,
    backup_set_id: str,
    backup_set_name: str,
    run_id: str = None,
    encrypt: bool = False,
    sync: bool = False
) -> bool:
    return send_event(
        event_type="heartbeat",
        message=f"Starting {backup_type} backup for {job_name}",
        run_id=run_id,
        backup_set_id=backup_set_id,
        job_name=job_name,
        backup_type=backup_type,
        backup_set_name=backup_set_name,
        encrypt=encrypt,
        sync=sync,
        stage="Starting backup"
    )


def send_backup_stage(
    job_name: str,
    backup_type: str,
    backup_set_id: str,
    backup_set_name: str,
    stage: str,
    run_id: str = None,
    encrypt: bool = False,
    sync: bool = False
) -> bool:
    return send_event(
        event_type="heartbeat",
        message=f"Backup {job_name}: {stage}",
        run_id=run_id,
        backup_set_id=backup_set_id,
        job_name=job_name,
        backup_type=backup_type,
        backup_set_name=backup_set_name,
        encrypt=encrypt,
        sync=sync,
        stage=stage
    )


def send_backup_complete(
    job_name: str,
    backup_type: str,
    backup_set_id: str,
    backup_set_name: str,
    duration_seconds: float,
    run_id: str = None,
    files_backed_up: int = 0,
    bytes_backed_up: int = 0,
    bytes_compressed: int = 0,
    encrypt: bool = False,
    sync: bool = False,
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
        backup_set_id=backup_set_id,
        job_name=job_name,
        backup_type=backup_type,
        backup_set_name=backup_set_name,
        encrypt=encrypt,
        sync=sync,
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


def sync_job_backup_sets(job_name: str, active_backup_set_ids: list) -> bool:
    """
    Tell the server which backup_set_ids this agent still has locally for a job.

    The server deletes any backup_jobs it has for this host+job whose
    backup_set_id is not in the given list, keeping it in sync after the
    agent rotates old backup sets out of its own database.

    Args:
        job_name: The job name to reconcile.
        active_backup_set_ids: List of server_set_id values still present in the
            agent's local database for this job.

    Returns:
        True if the server acknowledged the sync, False otherwise.
    """
    try:
        payload = {
            "hostname": get_hostname(),
            "ip_address": get_ip_address(),
            "job_name": job_name,
            "active_backup_set_ids": active_backup_set_ids,
        }

        response = requests.post(
            f"{SERVER_URL}/api/monitoring/sync-job-sets",
            json=payload,
            timeout=10
        )

        if response.status_code == 200:
            logger.debug(f"Synced backup sets for job '{job_name}' with server")
            return True

        logger.warning(f"Failed to sync backup sets for job '{job_name}': {response.status_code} {response.text}")
        return False

    except requests.exceptions.RequestException as e:
        logger.debug(f"Failed to sync backup sets for job '{job_name}': {e}")
        return False
