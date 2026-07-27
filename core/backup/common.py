"""Common backup utilities for locking, backup rotation, and shared backup logic."""

import os
import sys
import time
import json
import shutil
import glob
import socket
import subprocess
import ctypes
import platform

import portalocker

from logger import setup_logger, ensure_dir
from models.backup_sets import get_or_create_backup_set, get_backup_set_by_job_and_set, delete_backup_set, list_backup_sets
from models.backup_jobs import get_last_backup_job, get_last_full_backup_job, insert_backup_job, finalize_backup_job
from models.backup_files import insert_files
from models.db_core_agent import get_db_connection
from monitoring_client import sync_job_backup_sets
from .utils import create_tar_archives, should_exclude, get_merged_exclude_patterns, extract_tar_info, generate_archived_manifest
from .full import run_full_backup

# Stub functions (dashboard-side features)
def update_event(*args, **kwargs):
    """Stub: Events are reported to the dashboard via API."""
    pass

def finalize_event(*args, **kwargs):
    """Stub: Events are reported to the dashboard via API."""
    pass

def event_exists(*args, **kwargs):
    """Stub: Events are reported to the dashboard via API."""
    return False


def acquire_lock(lock_path):
    """
    Acquire an exclusive lock on the given file path using portalocker.
    
    Args:
        lock_path: Path to the lock file
        
    Returns:
        Open file handle with lock acquired
        
    Raises:
        RuntimeError: If the lock cannot be acquired
    """
    # Ensure the directory exists
    lock_dir = os.path.dirname(lock_path)
    if not os.path.exists(lock_dir):
        try:
            os.makedirs(lock_dir, exist_ok=True)
        except OSError as e:
            raise RuntimeError(f"Could not create lock directory: {lock_dir}") from e

    try:
        # First, check if the lock file exists but is stale
        if os.path.exists(lock_path):
            try:
                # Try to read the file without locking
                with open(lock_path, 'r', encoding='utf-8') as existing:
                    try:
                        lock_info = json.load(existing)
                        pid = lock_info.get("pid", 0)
                        created_at = lock_info.get("created_at", 0)

                        # If lock is very old (over 2 hours), remove it
                        if time.time() - created_at > 7200:
                            os.remove(lock_path)
                            print(f"Removed stale lock file: {lock_path}")
                    except (json.JSONDecodeError, ValueError):
                        # Invalid JSON, remove the lock file
                        os.remove(lock_path)
                        print(f"Removed invalid lock file: {lock_path}")
            except Exception:
                # Can't read the file, ignore
                pass

        # Create the file normally, then apply the lock
        lock_file = open(lock_path, 'a+', encoding='utf-8')

        # Get an exclusive lock (non-blocking)
        portalocker.lock(lock_file, portalocker.LOCK_EX | portalocker.LOCK_NB)

        # Truncate the file and write our process info
        lock_file.seek(0)
        lock_file.truncate()

        # Write process information to the lock file
        lock_info = {
            "pid": os.getpid(),
            "created_at": time.time(),
            "hostname": socket.gethostname(),
            "command": " ".join(["python"] + [os.path.basename(arg) for arg in sys.argv])
        }

        # Write the lock info as JSON
        json.dump(lock_info, lock_file)
        lock_file.flush()

        return lock_file

    except portalocker.exceptions.LockException as e:
        # Could not acquire lock - try to read the existing lock for better error message
        try:
            with open(lock_path, 'r', encoding='utf-8') as existing_lock:
                try:
                    lock_info = json.load(existing_lock)
                    pid = lock_info.get("pid", "unknown")
                    created = time.strftime('%Y-%m-%d %H:%M:%S',
                                          time.localtime(lock_info.get("created_at", 0)))
                    msg = f"Lock held by PID {pid} since {created}"
                except json.JSONDecodeError:
                    msg = "Lock exists but content is not valid JSON"
        except Exception:
            msg = "Lock exists but details couldn't be read"

        raise RuntimeError(f"Could not acquire lock: {lock_path}. {msg}") from e
    except Exception as e:
        raise RuntimeError(f"Error acquiring lock: {lock_path}") from e

def release_lock(lock_file):
    """
    Release the lock, close the file, and remove the lock file.

    Args:
        lock_file: Open file handle with lock acquired
    """
    if lock_file:
        try:
            # Get the path before closing the file
            lock_path = lock_file.name

            # Unlock and close the file
            portalocker.unlock(lock_file)
            lock_file.close()

            # Remove the lock file
            if os.path.exists(lock_path):
                os.remove(lock_path)
        except Exception as e:
            # Just try to close in case of error and log the issue
            try:
                lock_file.close()
            except Exception:
                pass

            # Try to get and remove the lock file anyway
            try:
                lock_path = getattr(lock_file, 'name', None)
                if lock_path and os.path.exists(lock_path):
                    os.remove(lock_path)
            except Exception:
                # If we can't remove it, log but don't crash
                pass

def is_process_running(pid):
    """
    Check if a process with the given PID is running, in a cross-platform way.

    Args:
        pid: Process ID to check

    Returns:
        True if the process is running, False otherwise
    """
    if platform.system() == "Windows":
        # Windows implementation

        # Use tasklist to check if PID exists
        try:
            output = subprocess.check_output(f"tasklist /FI \"PID eq {pid}\"", shell=True)
            return str(pid) in str(output)
        except:
            # Alternative using kernel32.dll
            try:
                kernel32 = ctypes.windll.kernel32
                SYNCHRONIZE = 0x00100000
                process = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
                if process != 0:
                    kernel32.CloseHandle(process)
                    return True
                return False
            except:
                return False
    else:
        # Unix-like implementation (Linux, macOS)
        try:
            os.kill(pid, 0)  # Signal 0 doesn't kill the process, just checks existence
            return True
        except ProcessLookupError:  # The process doesn't exist
            return False
        except OSError:  # Permission error, but process exists
            return True

def is_locked(lock_file_path):
    """
    Check if a lock file exists and is valid.
    Returns True if the job is locked, False otherwise.
    """
    if not os.path.exists(lock_file_path):
        return False

    try:
        # Try to open the lock file to read the PID
        with open(lock_file_path, 'r', encoding='utf-8') as f:
            pid_str = f.read().strip()

        if not pid_str:
            # Empty lock file, consider it stale
            return False

        # Parse the PID
        try:
            pid = int(pid_str)
        except ValueError:
            # Invalid PID, consider it stale
            return False

        # Check if the process is still running
        try:
            # Send signal 0 to check if process exists without affecting it
            os.kill(pid, 0)
            # Process exists, lock is valid
            return True
        except OSError:
            # Process doesn't exist, stale lock
            return False
    except (IOError, OSError, Exception):
        # Can't read the file or other error, assume not locked to be safe
        return False

def rotate_backups(job_dst, keep_sets, logger, config=None):
    """
    Rotate backup sets to keep only the latest 'keep_sets' sets and clean up corresponding database records and S3 folders.

    This function:
    1. Rotates filesystem backup sets (keeping the latest N)
    2. Rotates database records (keeping the latest N)
    3. Cleans up S3 buckets if configured
    """

    # Prefer the configured job name (matches DB records); fall back to directory name.
    job_name = (config or {}).get("job_name") or os.path.basename(job_dst)
    logger.info(f"Starting backup rotation for job '{job_name}', keeping {keep_sets} sets")

    # STEP 1: Rotate filesystem backup sets
    backup_sets = sorted(glob.glob(os.path.join(job_dst, "backup_set_*")), reverse=True)
    logger.debug(f"Found {len(backup_sets)} backup sets in filesystem for job '{job_name}'")

    if len(backup_sets) > keep_sets:
        # These are the sets to delete from the filesystem
        to_delete = backup_sets[keep_sets:]
        logger.debug(f"Will delete {len(to_delete)} oldest backup sets from filesystem")

        # Determine whether S3 cleanup is truly enabled
        aws_cfg = (config or {}).get("aws", {}) or {}
        aws_enabled = bool(aws_cfg.get("enabled", False))
        bucket = aws_cfg.get("bucket")
        profile = aws_cfg.get("profile", "default")
        region = aws_cfg.get("region")
        machine_name = socket.gethostname()
        sanitized_job_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in job_name)
        prefix = machine_name

        # Check for AWS credentials in environment variables
        has_env_creds = bool(os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"))

        # Track which set_names were actually deleted from filesystem; use these for DB cleanup.
        deleted_set_names = []

        for old_set in to_delete:
            try:
                backup_set_dir_name = os.path.basename(old_set)  # e.g., "backup_set_YYYYMMDD_HHMMSS"
                set_name = backup_set_dir_name.replace("backup_set_", "")

                shutil.rmtree(old_set)
                deleted_set_names.append(set_name)
                logger.info(f"Deleted old backup set directory: {old_set}")

                # Clean up S3 ONLY when explicitly enabled
                if aws_enabled and bucket:
                    s3_backup_set_path = f"s3://{bucket}/{prefix}/{sanitized_job_name}/{backup_set_dir_name}"
                    rm_cmd = ["aws", "s3", "rm", s3_backup_set_path, "--recursive"]

                    # Only use profile if we don't have environment credentials
                    if not has_env_creds and profile:
                        rm_cmd.extend(["--profile", profile])
                    if region:
                        rm_cmd.extend(["--region", region])

                    logger.debug(f"Deleting S3 backup set: {s3_backup_set_path}")
                    try:
                        subprocess.run(rm_cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                        logger.info(f"Deleted S3 backup set: {s3_backup_set_path}")
                    except subprocess.SubprocessError as e:
                        logger.error(f"Error deleting S3 backup set {s3_backup_set_path}: {e}")
            except Exception as e:
                logger.error(f"Error deleting backup set {old_set}: {e}")

        # Rotate database records to match what we actually deleted from disk.
        # This avoids wiping historical events due to DB-only sets.
        db_result = {"sets_deleted": 0, "jobs_deleted": 0, "files_deleted": 0}
        try:
            for set_name in deleted_set_names:
                bs_row = get_backup_set_by_job_and_set(job_name, set_name)
                if not bs_row:
                    continue

                backup_set_id = bs_row["id"] if isinstance(bs_row, dict) or hasattr(bs_row, "__getitem__") else None
                if backup_set_id is None:
                    continue

                # Count records before deletion so logs remain informative
                with get_db_connection() as conn:
                    c = conn.cursor()
                    c.execute("SELECT COUNT(*) FROM backup_jobs WHERE backup_set_id = ?", (backup_set_id,))
                    job_count = c.fetchone()[0]
                    c.execute(
                        """
                        SELECT COUNT(*) FROM backup_files
                        WHERE backup_job_id IN (SELECT id FROM backup_jobs WHERE backup_set_id = ?)
                        """,
                        (backup_set_id,),
                    )
                    file_count = c.fetchone()[0]

                # Use the existing deletion helper (deletes files + jobs + set)
                if delete_backup_set(backup_set_id):
                    db_result["sets_deleted"] += 1
                    db_result["jobs_deleted"] += int(job_count)
                    db_result["files_deleted"] += int(file_count)

            if db_result["sets_deleted"]:
                logger.info(
                    "Database rotation results: %s sets, %s jobs, %s file records deleted",
                    db_result["sets_deleted"],
                    db_result["jobs_deleted"],
                    db_result["files_deleted"],
                )
        except Exception as e:
            logger.error(f"Error rotating database records for job '{job_name}': {e}")
    else:
        logger.info(f"No filesystem backup sets to rotate for job '{job_name}'")

    # STEP 3: Reconcile the dashboard's records with what remains in the agent's
    # own database for this job, so any sets rotated out here (or previously,
    # if a prior sync call failed) are also removed from the dashboard.
    try:
        remaining_sets = list_backup_sets(job_name=job_name, limit=10000)
        active_backup_set_ids = [
            row["server_set_id"] for row in remaining_sets if row["server_set_id"]
        ]
        sync_job_backup_sets(job_name, active_backup_set_ids)
    except Exception as e:
        logger.error(f"Error syncing backup sets with dashboard for job '{job_name}': {e}")

    logger.info(f"Backup rotation completed for job '{job_name}'")

def run_partial_backup(config, backup_type, source_getter_fn, encrypt=False, sync=False, event_id=None, server_set_id=None, job_config_path=None, global_config=None):
    """
    Shared logic for incremental and differential backups.

    Args:
        config: The job configuration
        backup_type: 'incremental' or 'differential'
        source_getter_fn: Function to get source files for comparison
        encrypt: Whether to encrypt the backup
        sync: Whether to sync the backup
        event_id: The event ID
        job_config_path: Path to the job config file
        global_config: Global configuration

    Returns:
        Tuple containing: (backup_set_dir, event_id, backup_set_name, tarball_paths)
        Or ("skipped", event_id, None, None) if no changes
        Or (None, event_id, None, None) on error
    """
    # Get job name from config - use original name consistently throughout
    job_name = config.get("job_name", "unknown_job")
    logger = setup_logger(job_name)
    logger.debug(f"Starting {backup_type.upper()} backup job '{job_name}' with provided config.")

    # Update event with our current status
    if event_id and event_exists(event_id):
        update_event(event_id, event_message=f"Initializing {backup_type} backup for {job_name}")

    # Get source and destination paths from config
    src = config.get("source")
    dest = config.get("destination")
    if not src or not os.path.exists(src):
        error_msg = f"Source path does not exist: {src}"
        logger.error(error_msg)
        # Finalize the event ONLY for errors
        if event_id and event_exists(event_id):
            finalize_event(
                event_id=event_id,
                status="error",
                event_message=error_msg,
                backup_set_id=None,
                runtime="00:00:00"
            )
        return None, event_id, None, None, error_msg, None

    # Update event for next stage
    if event_id and event_exists(event_id):
        update_event(event_id, event_message="Setting up backup paths and loading exclude patterns")

    # Path setup
    machine_name = socket.gethostname()
    sanitized_job_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in job_name)
    sanitized_machine_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in machine_name)
    job_dst = os.path.join(dest, sanitized_machine_name, sanitized_job_name)
    ensure_dir(job_dst)

    # Get merged exclude patterns
    exclude_patterns = get_merged_exclude_patterns(config, global_config, job_config_path, logger)

    # Get max tarball size from config (default 1024 MB)
    max_tarball_size_mb = config.get("max_tarball_size", 1024)

    # Get the appropriate last backup job based on backup type
    if backup_type == 'incremental':
        # For incremental, we need any previous backup job (full or incremental)
        last_job = get_last_backup_job(job_name=job_name, completed_only=True)
        job_type_description = "previous backup job"
    else:  # differential
        # For differential, we specifically need the last full backup job
        last_job = get_last_full_backup_job(job_name=job_name)
        job_type_description = "previous full backup job"

    if not last_job:
        logger.warning(f"No {job_type_description} found in database, running full backup instead.")
        # Update the event to show we're changing to full backup
        if event_id and event_exists(event_id):
            update_event(
                event_id=event_id,
                event_message=f"No {job_type_description} found; running full backup instead of {backup_type}.",
                backup_type="full"
            )

        return run_full_backup(config, encrypt=encrypt, sync=sync, event_id=event_id, server_set_id=server_set_id, job_config_path=job_config_path, global_config=global_config)

    # Get the backup set that this job belongs to
    backup_set_name = last_job['set_name']

    logger.debug(f"Found {job_type_description} in set: {backup_set_name}, type: {backup_type}")

    # For both incremental and differential, use the same backup set
    target_backup_set_name = backup_set_name
    target_backup_set_dir = os.path.join(job_dst, f"backup_set_{target_backup_set_name}")

    # Ensure the target backup set directory exists
    if not os.path.exists(target_backup_set_dir):
        error_msg = f"Target backup set directory not found: {target_backup_set_dir}"
        logger.error(error_msg)
        # Finalize the event ONLY for errors
        if event_id and event_exists(event_id):
            finalize_event(
                event_id=event_id,
                status="error",
                event_message=error_msg,
                backup_set_id=None,
                runtime="00:00:00"
            )
        return None, event_id, None, None, error_msg, None

    # CRITICAL: Use original job name (not sanitized) for database operations
    # Get or create the backup set for file comparison - without updating config_snapshot for incremental/differential
    
    # Check if the backup set already exists
    existing_set = get_backup_set_by_job_and_set(job_name, target_backup_set_name)
    
    # Only include source_path when creating a new set, not for existing ones
    source_path = None if existing_set else config.get('source')
    
    backup_set_id = get_or_create_backup_set(
        job_name=job_name,  # Use original job name
        set_name=target_backup_set_name,
        # Do not update config_snapshot for incremental/differential backups
        config_settings=None,
        source_path=source_path,
        server_set_id=server_set_id
    )

    # Get source files for comparison using the provided getter function
    # For incremental, this will be all files in the backup set
    # For differential, this will be just files from the last full backup
    source_description = "backup set" if backup_type == "incremental" else "last full backup"
    logger.debug(f"Checking for changes against {source_description}: {target_backup_set_name}")

    # The source_getter_fn parameter should handle the appropriate lookup logic
    source_files = source_getter_fn(backup_set_id if backup_type == "incremental" else job_name)

    # Convert database files to the format expected by comparison function
    manifest_data = {
        "files": []
    }

    for db_file in source_files:
        manifest_data["files"].append({
            "path": db_file["path"],
            "mtime": db_file["mtime"],
            "size": db_file["size"]
        })

    logger.debug(f"Comparing against {len(manifest_data['files'])} files from {source_description}")

    # Detect new or modified files by comparing with source files
    files = get_new_or_modified_files_from_data(
        src, manifest_data, exclude_patterns=exclude_patterns, job_name=job_name
    )
    if not files:
        logger.info(f"No files changed or added since {source_description}. Skipping {backup_type} backup.")
        # We don't finalize here - let the CLI handle it
        return "skipped", event_id, None, None, None, None

    # Update event for starting the backup
    if event_id and event_exists(event_id):
        update_event(event_id, event_message=f"Found {len(files)} new or modified files, creating tarballs")

    # Create local DB record for this incremental/differential job
    backup_job_id = insert_backup_job(
        backup_set_id=backup_set_id,
        backup_type=backup_type,
        encrypted=encrypt,
        synced=sync,
        event_message=f"Starting {backup_type} backup"
    )
    logger.info(f"Created {backup_type} backup job with ID {backup_job_id}")

    try:
        # Create tarballs for the new/changed files in the existing backup set directory
        tarball_paths = create_tar_archives(
            files,
            target_backup_set_dir,
            max_tarball_size_mb,
            logger,
            backup_type,
            config
        )

        encryption_enabled = config.get("encryption", {}).get("enabled", False) or encrypt
        new_tar_info = []
        for tar_path in tarball_paths:
            tar_info = extract_tar_info(tar_path, encryption_enabled=encryption_enabled)
            if tar_info:
                new_tar_info.extend(tar_info)

        # Mark files as new or modified based on backup type
        for file_info in new_tar_info:
            path = file_info.get("path", "")
            normalized_path = os.path.normpath(path).replace("\\", "/")

            if backup_type == 'incremental':
                # For incremental, check if file exists in previous backups
                file_exists = False
                for db_file in source_files:
                    db_path = db_file.get("path", "")
                    normalized_db_path = os.path.normpath(db_path).replace("\\", "/")

                    if normalized_path == normalized_db_path:
                        file_exists = True
                        file_info['is_new'] = False
                        file_info['is_modified'] = True
                        break

                if not file_exists:
                    file_info['is_new'] = True
                    file_info['is_modified'] = False
            else:
                # For differential, all files are considered modified since last full backup
                file_info['is_new'] = False
                file_info['is_modified'] = True

        # Insert the files into the database for this backup job
        if tarball_paths:
            # Calculate totals first
            total_files = len(new_tar_info)
            total_size_bytes = sum(f.get('size', 0) for f in new_tar_info)

            logger.debug(f"Inserting {total_files} files into database for backup job {backup_job_id}")
            insert_files(backup_job_id, new_tar_info)

            # Update event with progress
            if event_id and event_exists(event_id):
                update_event(
                    event_id=event_id,
                    event_message=f"Backed up {total_files} files ({total_size_bytes} bytes)"
                )

            logger.info(f"Tarballs created for: {total_files} files, {total_size_bytes} bytes")

            # Generate updated HTML manifest for the backup set
            if event_id and event_exists(event_id):
                update_event(event_id, event_message="Generating manifest files")

            html_manifest_path = generate_archived_manifest(
                job_name=job_name,
                backup_set_id=target_backup_set_name,
                backup_set_path=target_backup_set_dir,
                backup_type=backup_type,
                backup_job_id=backup_job_id,
            )
            logger.info(f"Manifest written to: {html_manifest_path}")

        else:
            logger.warning("No tarballs created")
            if event_id and event_exists(event_id):
                update_event(event_id, event_message="No files to backup")

        # Important: Don't finalize the event here!
        # Just update it with progress information
        if event_id and event_exists(event_id):
            update_event(
                event_id=event_id,
                event_message=f"{backup_type.capitalize()} backup completed successfully",
                status="running"  # Keep it running so CLI can finalize it
            )

        logger.debug(f"{backup_type.upper()} backup completed for {src}")
        finalize_backup_job(
            job_id=backup_job_id,
            status="completed",
            event_message=f"{backup_type.capitalize()} backup completed",
            total_files=len(new_tar_info),
            total_size_bytes=sum(f.get('size', 0) for f in new_tar_info),
        )
        # Return the tarball_paths as the 4th return value
        return (
            target_backup_set_dir, event_id, target_backup_set_name, tarball_paths, None,
            {"files": len(new_tar_info), "bytes": sum(f.get('size', 0) for f in new_tar_info)}
        )

    except Exception as e:
        logger.error(f"Error during {backup_type} backup: {e}", exc_info=True)
        finalize_backup_job(
            job_id=backup_job_id,
            status="failed",
            event_message=f"{backup_type.capitalize()} backup failed: {e}",
        )
        raise

def get_new_or_modified_files_from_data(src, manifest_data, exclude_patterns=None,job_name="unknown_job"):
    """
    Compare filesystem with manifest data to find new or modified files.
    This replaces the JSON file-based comparison with in-memory data comparison.

    Args:
        src: Source directory to scan
        manifest_data: Dictionary of previous backup files
        exclude_patterns: List of patterns to exclude
        backup_type: Type of backup ('incremental' or 'differential')

    Returns:
        List of file paths that are new or modified
    """
    if exclude_patterns is None:
        exclude_patterns = []

    logger = setup_logger(job_name)

    # Debug - print the patterns to help diagnose issues
    #logger.info(f"{backup_type.capitalize()} backup scanning with {len(exclude_patterns)} exclusion patterns") #debug

    # Normalize the paths in manifest_files to ensure consistent comparison
    manifest_files = {}
    for file_info in manifest_data.get("files", []):
        path = file_info.get("path", "")
        # Normalize path: ensure forward slashes, remove any leading/trailing slashes
        normalized_path = os.path.normpath(path).replace("\\", "/")
        manifest_files[normalized_path] = {
            "mtime": file_info.get("mtime", 0),
            "size": file_info.get("size", 0)
        }

    logger.debug(f"Loaded {len(manifest_files)} files from previous backup for comparison")

    new_or_modified = []
    dirs_excluded = 0
    files_excluded = 0
    files_checked = 0

    for root, dirs, files in os.walk(src):
        # Process directories to exclude before walking them
        i = 0
        while i < len(dirs):
            dir_path = os.path.join(root, dirs[i])

            if should_exclude(dir_path, exclude_patterns, src, logger=logger):
                dirs.pop(i)
                dirs_excluded += 1
            else:
                i += 1

        # Process files
        for file in files:
            file_path = os.path.join(root, file)
            rel_path = os.path.relpath(file_path, src)
            normalized_rel_path = os.path.normpath(rel_path).replace("\\", "/")
            files_checked += 1

            # Check if file should be excluded
            if should_exclude(file_path, exclude_patterns, src, logger=logger):
                files_excluded += 1
                continue

            try:
                stat_info = os.stat(file_path)
                current_mtime = stat_info.st_mtime
                current_size = stat_info.st_size

                # Check if file is new or modified
                if normalized_rel_path not in manifest_files:
                    logger.debug(f"New file found: {normalized_rel_path}")
                    new_or_modified.append(file_path)
                else:
                    manifest_file = manifest_files[normalized_rel_path]
                    # Use a more precise comparison and add logging for troubleshooting
                    mtime_diff = abs(current_mtime - manifest_file["mtime"])
                    if mtime_diff > 1 or current_size != manifest_file["size"]:
                        logger.debug(f"Modified file: {normalized_rel_path} - "
                                     f"mtime diff: {mtime_diff}, "
                                     f"size: {current_size} vs {manifest_file['size']}")
                        new_or_modified.append(file_path)

            except OSError as e:
                logger.warning(f"Could not access file {file_path}: {e}")
                continue

    logger.debug(f"SUMMARY: Checked {files_checked} files, excluded {dirs_excluded} directories and {files_excluded} files")
    logger.info(f"Found {len(new_or_modified)} new or modified files to back up")

    if len(new_or_modified) <= 10:
        for file_path in new_or_modified:
            logger.debug(f"Will back up: {os.path.relpath(file_path, src)}")

    return new_or_modified
