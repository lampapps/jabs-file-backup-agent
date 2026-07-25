"""Database operations for backup set records in JABS.

Provides functions to create, retrieve, update, delete, and rotate backup sets and their associated jobs and files.
"""
import socket
import time
import sqlite3
import logging
from typing import List, Dict, Optional
from models.db_core_agent import get_db_connection

def get_or_create_backup_set(job_name: str, set_name: str, config_settings: Optional[str] = None, source_path: Optional[str] = None, server_set_id: Optional[str] = None) -> int:
    """Get existing backup set or create new one if it doesn't exist."""
    with get_db_connection() as conn:
        c = conn.cursor()

        # Try to get existing backup set
        c.execute("SELECT id FROM backup_sets WHERE job_name = ? AND set_name = ?", (job_name, set_name))
        row = c.fetchone()

        if row:
            # Update the updated_at timestamp and server_set_id if provided
            if server_set_id:
                c.execute("UPDATE backup_sets SET updated_at = ?, server_set_id = ? WHERE id = ?", (time.time(), server_set_id, row['id']))
            else:
                c.execute("UPDATE backup_sets SET updated_at = ? WHERE id = ?", (time.time(), row['id']))
            conn.commit()
            return row['id']
        else:
            # Create new backup set
            current_time = time.time()
            hostname = socket.gethostname()
            c.execute("""
                INSERT INTO backup_sets (job_name, set_name, server_set_id, created_at, updated_at, config_snapshot, source_path, hostname)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (job_name, set_name, server_set_id, current_time, current_time, config_settings, source_path, hostname))
            conn.commit()
            return c.lastrowid

def get_backup_set_by_job_id(backup_job_id: int) -> Optional[dict]:
    """Get the backup_sets row for the set that contains the given backup_job_id."""
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT * FROM backup_sets
            WHERE id = (SELECT backup_set_id FROM backup_jobs WHERE id = ?)
        """, (backup_job_id,))
        row = c.fetchone()
        return dict(row) if row else None


def get_backup_set_by_job_and_set(job_name: str, set_name: str) -> Optional[sqlite3.Row]:
    """Get a backup set by job_name and set_name."""
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM backup_sets WHERE job_name = ? AND set_name = ?", (job_name, set_name))
        return c.fetchone()

def get_last_set_info_for_job(job_name: str) -> Optional[dict]:
    """Return server_set_id and set_name of the most recent completed full backup set for a job."""
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT bs.server_set_id, bs.set_name
            FROM backup_sets bs
            JOIN backup_jobs bj ON bj.backup_set_id = bs.id
            WHERE bs.job_name = ?
              AND bj.backup_type = 'full'
              AND bj.status = 'completed'
              AND bs.server_set_id IS NOT NULL
            ORDER BY bj.completed_at DESC
            LIMIT 1
        """, (job_name,))
        row = c.fetchone()
        return {'server_set_id': row['server_set_id'], 'set_name': row['set_name']} if row else None

def list_backup_sets(job_name: Optional[str] = None, limit: int = 20) -> List[sqlite3.Row]:
    """List backup sets, optionally filtered by job_name."""
    with get_db_connection() as conn:
        c = conn.cursor()
        if job_name:
            c.execute("""
                SELECT * FROM backup_sets 
                WHERE job_name = ?
                ORDER BY created_at DESC LIMIT ?
            """, (job_name, limit))
        else:
            c.execute("""
                SELECT * FROM backup_sets 
                ORDER BY created_at DESC LIMIT ?
            """, (limit,))
        return c.fetchall()

def delete_backup_set(set_id: int) -> bool:
    """
    Delete a backup set and all its associated data.
    
    This deletes:
    1. All backup files associated with jobs in this set
    2. All backup jobs in this set
    3. The backup set itself
    
    Returns:
        True if deletion was successful, False otherwise
    """
    logger = logging.getLogger("app")

    logger.info(f"Deleting backup set with ID {set_id} and all related records")

    try:
        with get_db_connection() as conn:
            c = conn.cursor()

            # First, check that the backup set exists
            c.execute("SELECT id, job_name, set_name FROM backup_sets WHERE id = ?", (set_id,))
            backup_set = c.fetchone()
            if not backup_set:
                logger.error(f"Backup set with ID {set_id} not found in database")
                return False

            logger.debug(f"Found backup set: {dict(backup_set)}")

            # Count how many backup jobs are associated with this set
            c.execute("SELECT COUNT(*) FROM backup_jobs WHERE backup_set_id = ?", (set_id,))
            job_count = c.fetchone()[0]

            # Count how many backup files are associated with this set
            c.execute("""
                SELECT COUNT(*) FROM backup_files 
                WHERE backup_job_id IN (
                    SELECT id FROM backup_jobs WHERE backup_set_id = ?
                )
            """, (set_id,))
            file_count = c.fetchone()[0]

            logger.info(f"About to delete {job_count} job(s) and {file_count} file record(s) for backup set {set_id}")

            # Delete all related backup files
            c.execute("""
                DELETE FROM backup_files 
                WHERE backup_job_id IN (
                    SELECT id FROM backup_jobs WHERE backup_set_id = ?
                )
            """, (set_id,))
            files_deleted = c.rowcount

            # Then, delete all backup jobs
            c.execute("DELETE FROM backup_jobs WHERE backup_set_id = ?", (set_id,))
            jobs_deleted = c.rowcount

            # Finally, delete the backup set
            c.execute("DELETE FROM backup_sets WHERE id = ?", (set_id,))
            sets_deleted = c.rowcount

            conn.commit()

            logger.info(f"Successfully deleted backup set {set_id}: {sets_deleted} set(s), {jobs_deleted} job(s), {files_deleted} file record(s)")
            return True
    except Exception as e:
        logger.error(f"Failed to delete backup set {set_id}: {e}", exc_info=True)
        return False

def set_backup_set_config(backup_set_id: int, config_settings: str) -> bool:
    """Set the config snapshot for an existing backup set."""
    logger = logging.getLogger("app")

    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute(
                "UPDATE backup_sets SET config_snapshot = ? WHERE id = ?",
                (config_settings, backup_set_id)
            )
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"Failed to set config snapshot: {e}")
        return False
