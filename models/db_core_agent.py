"""Core database utilities and schema management for JABS Agent.

Handles connection management and schema initialization for local agent backups.
Agent database is isolated and only tracks backup sets, jobs, and files locally.
"""

import sqlite3
import os
from contextlib import contextmanager

# Default agent database location
AGENT_DB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
AGENT_DB_PATH = os.path.join(AGENT_DB_DIR, "jabs_agent.sqlite")

@contextmanager
def get_db_connection(db_path: str = AGENT_DB_PATH):
    """Context manager for SQLite database connection with foreign key support."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
    finally:
        conn.close()

def init_db(db_path: str = AGENT_DB_PATH):
    """Initialize the agent database schema (backup metadata only)."""
    # Ensure the parent directory exists
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    with get_db_connection(db_path) as conn:
        c = conn.cursor()

        # Enable foreign key constraints
        c.execute("PRAGMA foreign_keys = ON")

        # Create backup tracking tables only (no monitoring tables)
        _create_backup_sets_table(c)
        _create_backup_jobs_table(c)
        _create_backup_files_table(c)
        _create_indexes(c)
        _migrate_schema(conn)

        conn.commit()

def _create_backup_sets_table(cursor):
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS backup_sets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_name TEXT NOT NULL,           -- e.g., "test2", "jabs"
        set_name TEXT NOT NULL,           -- e.g., "20250706_130851" (from full backup)
        server_set_id TEXT,               -- UUID4 sent to server as backup_set_id (shared by full + incrementals)
        created_at REAL NOT NULL,         -- When the full backup was first run
        updated_at REAL NOT NULL,         -- Last activity in this set
        description TEXT,
        is_active BOOLEAN DEFAULT 1,      -- Can mark old sets as inactive
        config_snapshot TEXT,             -- Config used when set was created
        source_path TEXT,                 -- Source path for restoration purposes
        hostname TEXT,                    -- Added for events view
        UNIQUE(job_name, set_name)
    );
    """)

def _create_backup_jobs_table(cursor):
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS backup_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        backup_set_id INTEGER NOT NULL,
        backup_type TEXT NOT NULL,        -- 'full', 'differential', 'incremental', 'dryrun'
        started_at REAL NOT NULL,
        completed_at REAL,
        status TEXT NOT NULL DEFAULT 'running', -- 'running', 'completed', 'failed', 'cancelled'
        encrypted BOOLEAN DEFAULT 0,
        synced BOOLEAN DEFAULT 0,
        runtime_seconds INTEGER,
        total_files INTEGER DEFAULT 0,
        total_size_bytes INTEGER DEFAULT 0,
        event_message TEXT,
        error_message TEXT,               -- For failed jobs
        FOREIGN KEY (backup_set_id) REFERENCES backup_sets(id) ON DELETE CASCADE
    );
    """)

def _create_backup_files_table(cursor):
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS backup_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        backup_job_id INTEGER NOT NULL,
        tarball TEXT NOT NULL,
        path TEXT NOT NULL,
        mtime REAL NOT NULL,
        size_bytes INTEGER NOT NULL,
        checksum TEXT,                    -- Optional integrity checking
        is_new BOOLEAN DEFAULT 0,         -- True for new files (incremental/diff)
        is_modified BOOLEAN DEFAULT 0,    -- True for modified files (incremental/diff)
        FOREIGN KEY (backup_job_id) REFERENCES backup_jobs(id) ON DELETE CASCADE
    );
    """)

def _create_indexes(cursor):
    """Create indexes for agent tables only (backup tracking)."""
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_backup_sets_job_name ON backup_sets(job_name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_backup_jobs_set_id ON backup_jobs(backup_set_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_backup_jobs_type ON backup_jobs(backup_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_backup_jobs_started_at ON backup_jobs(started_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_backup_files_job_id ON backup_files(backup_job_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_backup_files_path ON backup_files(path)")

def _migrate_schema(conn):
    """Apply schema migrations for existing databases."""
    c = conn.cursor()
    c.execute("PRAGMA table_info(backup_sets)")
    columns = {row[1] for row in c.fetchall()}
    if 'server_set_id' not in columns:
        c.execute("ALTER TABLE backup_sets ADD COLUMN server_set_id TEXT")
