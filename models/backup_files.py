"""Database operations for backup file records in JABS.

Provides functions to insert, retrieve, and search backup file metadata
associated with backup jobs and sets.
"""
import sqlite3
from typing import List, Dict, Any
from models.db_core_agent import get_db_connection

def insert_files(backup_job_id: int, files: List[Dict[str, Any]]):
    """Insert backup files for a backup job."""
    with get_db_connection() as conn:
        c = conn.cursor()
        c.executemany("""
            INSERT INTO backup_files (backup_job_id, tarball, path, mtime, size_bytes, is_new, is_modified)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [
            (
                backup_job_id,
                f["tarball"],
                f["path"],
                f["mtime"],
                f.get("size", 0),  # Handle both 'size' and 'size_bytes'
                f.get("is_new", False),
                f.get("is_modified", False)
            )
            for f in files
        ])
        conn.commit()

def get_files_for_backup_job(backup_job_id: int) -> List[Dict[str, Any]]:
    """Get all files for a backup job."""
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT tarball, path, mtime, size_bytes as size, is_new, is_modified
            FROM backup_files
            WHERE backup_job_id = ?
            ORDER BY path
        """, (backup_job_id,))
        return [dict(row) for row in c.fetchall()]

def get_files_for_backup_set(backup_set_id: int) -> List[Dict[str, Any]]:
    """Get all files across all jobs in a backup set."""
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT bf.tarball, bf.path, bf.mtime, bf.size_bytes as size, bf.is_new, bf.is_modified,
                   bj.backup_type, bj.started_at as job_started_at
            FROM backup_files bf
            JOIN backup_jobs bj ON bf.backup_job_id = bj.id
            WHERE bj.backup_set_id = ?
            ORDER BY bf.path
        """, (backup_set_id,))
        return [dict(row) for row in c.fetchall()]

def get_files_for_last_full_backup(job_name: str) -> List[Dict[str, Any]]:
    """Get files from the last completed full backup for differential comparison."""
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT bf.tarball, bf.path, bf.mtime, bf.size_bytes as size
            FROM backup_files bf
            JOIN backup_jobs bj ON bf.backup_job_id = bj.id
            JOIN backup_sets bs ON bj.backup_set_id = bs.id
            WHERE bs.job_name = ? AND bj.backup_type = 'full' AND bj.status = 'completed'
            ORDER BY bj.started_at DESC, bf.path
        """, (job_name,))
        return [dict(row) for row in c.fetchall()]

def get_files_for_backup_set_by_job_id(backup_job_id: int) -> List[Dict[str, Any]]:
    """Get all files across every job in the backup set containing backup_job_id.

    Returns each file record with its backup_type so the manifest can show
    which archive holds each version of a file.
    """
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT bf.tarball, bf.path, bf.mtime, bf.size_bytes AS size,
                   bf.is_new, bf.is_modified, bj.backup_type, bj.started_at
            FROM backup_files bf
            JOIN backup_jobs bj ON bf.backup_job_id = bj.id
            WHERE bj.backup_set_id = (
                SELECT backup_set_id FROM backup_jobs WHERE id = ?
            )
            ORDER BY bf.path ASC, bj.started_at ASC
        """, (backup_job_id,))
        return [dict(row) for row in c.fetchall()]


def search_files(query: str, job_name: str = None) -> List[sqlite3.Row]:
    """Search for files by path pattern."""
    with get_db_connection() as conn:
        c = conn.cursor()
        if job_name:
            c.execute("""
                SELECT 
                    bf.path,
                    bf.mtime,
                    bf.size_bytes as size,
                    bf.tarball,
                    bs.job_name,
                    bs.set_name,
                    bj.backup_type
                FROM backup_files bf
                JOIN backup_jobs bj ON bf.backup_job_id = bj.id
                JOIN backup_sets bs ON bj.backup_set_id = bs.id
                WHERE bs.job_name = ? AND bf.path LIKE ?
                ORDER BY bs.updated_at DESC, bf.path
            """, (job_name, f"%{query}%"))
        else:
            c.execute("""
                SELECT 
                    bf.path,
                    bf.mtime,
                    bf.size_bytes as size,
                    bf.tarball,
                    bs.job_name,
                    bs.set_name,
                    bj.backup_type
                FROM backup_files bf
                JOIN backup_jobs bj ON bf.backup_job_id = bj.id
                JOIN backup_sets bs ON bj.backup_set_id = bs.id
                WHERE bf.path LIKE ?
                ORDER BY bs.job_name, bs.updated_at DESC, bf.path
            """, (f"%{query}%",))

        return c.fetchall()
