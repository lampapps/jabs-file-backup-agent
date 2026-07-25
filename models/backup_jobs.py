"""Database operations for backup job records in JABS.

Provides functions to insert, update, finalize, and query backup job metadata.
"""
import time
import sqlite3
from typing import List, Optional
from models.db_core_agent import get_db_connection

def insert_backup_job(
    backup_set_id: int,
    backup_type: str,
    encrypted: bool = False,
    synced: bool = False,
    started_at: float = None,
    event_message: str = None
) -> int:
    """Insert a new backup job."""
    if started_at is None:
        started_at = time.time()

    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO backup_jobs (
                backup_set_id, backup_type, started_at, encrypted, synced, event_message
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (backup_set_id, backup_type, started_at, encrypted, synced, event_message))
        conn.commit()
        return c.lastrowid

def finalize_backup_job(
    job_id: int,
    completed_at: float = None,
    status: str = "completed",
    event_message: str = None,
    error_message: str = None,
    total_files: int = 0,
    total_size_bytes: int = 0,
    runtime_seconds: Optional[int] = None
):
    """Update backup job when completed."""
    if completed_at is None:
        completed_at = time.time()

    # Calculate runtime if not explicitly provided
    if runtime_seconds is None:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT started_at FROM backup_jobs WHERE id = ?", (job_id,))
            row = c.fetchone()
            runtime_seconds = int(completed_at - row['started_at']) if row else 0

    # Ensure runtime is at least 1 second if job completed successfully
    # This prevents "00:00:00" display for very short successful jobs
    if status == "completed" and runtime_seconds == 0:
        runtime_seconds = 1

    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("""
            UPDATE backup_jobs 
            SET completed_at = ?, status = ?, event_message = ?, error_message = ?, 
                runtime_seconds = ?, total_files = ?, total_size_bytes = ?
            WHERE id = ?
        """, (completed_at, status, event_message, error_message, runtime_seconds,
              total_files, total_size_bytes, job_id))
        conn.commit()
        return c.rowcount > 0

def get_jobs_for_backup_set(backup_set_id: int) -> List[sqlite3.Row]:
    """Get all jobs for a backup set."""
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT * FROM backup_jobs 
            WHERE backup_set_id = ? 
            ORDER BY started_at ASC
        """, (backup_set_id,))
        return c.fetchall()

def get_last_backup_job(
    job_name: str,
    backup_type: Optional[str] = None,
    completed_only: bool = True
) -> Optional[sqlite3.Row]:
    """Get the most recent backup job for a job name, ignoring restore jobs."""
    with get_db_connection() as conn:
        c = conn.cursor()
        query = """
            SELECT bj.*, bs.job_name, bs.set_name, bs.id as backup_set_id
            FROM backup_jobs bj
            JOIN backup_sets bs ON bj.backup_set_id = bs.id
            WHERE bs.job_name = ?
              AND bj.backup_type NOT IN ('restore', 'dryrun')
        """
        params = [job_name]

        if backup_type:
            query += " AND bj.backup_type = ?"
            params.append(backup_type)

        if completed_only:
            query += " AND bj.status = 'completed'"

        query += " ORDER BY bj.started_at DESC LIMIT 1"
        c.execute(query, params)
        return c.fetchone()

def get_last_full_backup_job(job_name: str) -> Optional[sqlite3.Row]:
    """Get the most recent completed full backup job for a job name."""
    return get_last_backup_job(job_name, backup_type="full", completed_only=True)

def update_job_sync_status(job_id: int, synced: bool):
    """Update the synced status of a backup job."""
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE backup_jobs SET synced = ? WHERE id = ?", (synced, job_id))
        conn.commit()
        return c.rowcount > 0
