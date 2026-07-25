"""Full backup logic for JABS: handles creation, archiving, and database updates for full backup jobs."""

import os
import shutil
import socket
import json
import time

from logger import setup_logger, timestamp, ensure_dir

from models.backup_sets import get_or_create_backup_set
from models.backup_jobs import insert_backup_job, finalize_backup_job
from models.backup_files import insert_files
from monitoring_client import send_backup_stage
from settings import RESTORE_SCRIPT_SRC

from .utils import get_all_files, create_tar_archives, get_merged_exclude_patterns, extract_tar_info, generate_archived_manifest

def run_full_backup(config, backup_type="full", encrypt=False, sync=False, event_id=None, server_set_id=None, job_config_path=None, global_config=None):
    """
    Run a full backup job: collect files, create tarballs, update database, and generate manifest.

    Args:
        config (dict): Job configuration dictionary.
        backup_type (str): Type of backup (full, differential, incremental, dryrun)
        encrypt (bool): Whether to encrypt the backup.
        sync (bool): Whether to sync the backup after completion.
        event_id (str, optional): Event/backup_set_id for tracking.
        job_config_path (str, optional): Path to the job config file.
        global_config (dict, optional): Global configuration dictionary.

    Returns:
        tuple: (backup_set_dir, event_id, backup_set_id_string, tarball_paths)
            Or (None, event_id, None, None) on error.
    """
    job_name = config.get("job_name", "unknown_job")
    logger = setup_logger(job_name)
    logger.debug(f"Starting FULL backup job '{job_name}' with provided config.")

    # Generate backup_set_name early for event reporting
    backup_set_name = timestamp()

    # Send start update to server
    if event_id:
        send_backup_stage(
            job_name=job_name,
            backup_type=backup_type,
            run_id=event_id,
            backup_set_id=server_set_id or event_id,
            backup_set_name=backup_set_name,
            stage=f"Initializing full backup for {job_name}",
            encrypt=encrypt,
            sync=sync
        )

    # Get merged exclude patterns using the utility function
    exclude_patterns = get_merged_exclude_patterns(config, global_config, job_config_path, logger)

    src = config.get("source")
    dest = config.get("destination")
    if not src or not os.path.exists(src):
        error_msg = f"Source path does not exist: {src}"
        logger.error(error_msg)
        return None, event_id, None, None, error_msg, None

    # Path setup
    machine_name = socket.gethostname()
    sanitized_job_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in job_name)
    sanitized_machine_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in machine_name)
    job_dst = os.path.join(dest, sanitized_machine_name, sanitized_job_name)
    ensure_dir(job_dst)

    backup_set_dir = os.path.join(job_dst, f"backup_set_{backup_set_name}")
    backup_set_id_string = backup_set_name  # Human-readable name for filesystem and display
    ensure_dir(backup_set_dir)

    max_tarball_size_mb = config.get("max_tarball_size", 1024)

    # For full backups, we ALWAYS create a new backup set with the config snapshot
    try:
        # Debug the config object
        logger.debug(f"config type: {type(config)}")
        logger.debug(f"config empty? {not bool(config)}")
        logger.debug(f"config keys: {list(config.keys()) if config else 'None'}")

        # Create a JSON string of the config for the config_snapshot field
        config_snapshot = json.dumps(config) if config else None

        # Debug the config_snapshot
        logger.debug(f"config_snapshot type: {type(config_snapshot)}")
        logger.debug(f"config_snapshot is None? {config_snapshot is None}")
        if config_snapshot:
            logger.debug(f"config_snapshot length: {len(config_snapshot)}")
            logger.debug(f"config_snapshot preview: {config_snapshot[:100]}...")

        # Create a new backup set entry in the database
        backup_set_id = get_or_create_backup_set(
            job_name=job_name,
            set_name=backup_set_name,
            config_settings=config_snapshot,
            source_path=config.get('source'),
            server_set_id=server_set_id
        )
        logger.debug(f"Created backup set with ID {backup_set_id}")

        # Always insert a local DB record; event_id is a server-side string, not a local DB integer
        backup_job_id = insert_backup_job(
            backup_set_id=backup_set_id,
            backup_type="full",
            encrypted=encrypt,
            synced=sync,
            event_message="Starting full backup"
        )
        logger.info(f"Created backup job with ID {backup_job_id}")

    except Exception as e:
        error_msg = f"Failed to create database entries: {e}"
        logger.error(error_msg)
        return None, event_id, None, None, error_msg, None

    try:
        # Send stage update: scanning files
        if event_id:
            send_backup_stage(
                job_name=job_name,
                backup_type=backup_type,
                run_id=event_id,
                backup_set_id=server_set_id or event_id,
                backup_set_name=backup_set_name,
                stage=f"Scanning source directory with {len(exclude_patterns)} exclude patterns",
                encrypt=encrypt,
                sync=sync
            )

        logger.debug(f"Collecting files with {len(exclude_patterns)} exclude patterns")
        files = get_all_files(src, exclude_patterns, logger=logger, job_name=job_name)
        logger.debug(f"Collected {len(files)} files after applying exclusion patterns")

        # Send stage update: creating archives
        if event_id:
            send_backup_stage(
                job_name=job_name,
                backup_type=backup_type,
                run_id=event_id,
                backup_set_id=server_set_id or event_id,
                backup_set_name=backup_set_name,
                stage=f"Creating tar archives for {len(files)} files",
                encrypt=encrypt,
                sync=sync
            )

        tarball_paths = create_tar_archives(
            files, backup_set_dir, max_tarball_size_mb, logger, "full", config
        )

        encryption_enabled = config.get("encryption", {}).get("enabled", False) or encrypt
        new_tar_info = []
        total_files = 0
        total_size_bytes = 0

        # Send stage update: extracting file info
        if event_id:
            send_backup_stage(
                job_name=job_name,
                backup_type=backup_type,
                run_id=event_id,
                backup_set_id=server_set_id or event_id,
                backup_set_name=backup_set_name,
                stage="Extracting file information from tarballs",
                encrypt=encrypt,
                sync=sync
            )

        # Extract file info from all tarballs
        for tar_path in tarball_paths:
            tar_info = extract_tar_info(tar_path, encryption_enabled=encryption_enabled)
            if tar_info:  # Handle None return from stub function
                new_tar_info.extend(tar_info)
                total_files += len(tar_info)
                total_size_bytes += sum(f.get('size', 0) for f in tar_info)

        # Insert file records into the database
        if new_tar_info:
            if event_id:
                send_backup_stage(
                    job_name=job_name,
                    backup_type=backup_type,
                    run_id=event_id,
                    backup_set_id=server_set_id or event_id,
                    backup_set_name=backup_set_name,
                    stage=f"Updating database with {len(new_tar_info)} files",
                    encrypt=encrypt,
                    sync=sync
                )
            logger.debug(f"Inserting {len(new_tar_info)} files into database...")
            insert_files(backup_job_id, new_tar_info)
            logger.info(f"Database updated with {total_files} files, {total_size_bytes} bytes")

        # Generate manifest HTML (now reads from database)
        if tarball_paths:
            logger.debug("Writing manifest files...")
            if event_id:
                send_backup_stage(
                    job_name=job_name,
                    backup_type=backup_type,
                    run_id=event_id,
                    backup_set_id=server_set_id or event_id,
                    backup_set_name=backup_set_name,
                    stage="Generating manifest files",
                    encrypt=encrypt,
                    sync=sync
                )

            html_manifest_path = generate_archived_manifest(
                job_name=job_name,
                backup_set_id=backup_set_id_string,
                backup_set_path=backup_set_dir,
                backup_type="full",
                backup_job_id=backup_job_id,
            )
            logger.info(f"Manifest written to: {html_manifest_path}")
        else:
            logger.warning("No tarballs created, skipping manifest generation.")
            total_files = 0
            total_size_bytes = 0

        # Copy restore.py
        try:
            shutil.copy2(RESTORE_SCRIPT_SRC, backup_set_dir)
        except (OSError, shutil.Error) as e:
            logger.warning(f"Could not copy restore.py to backup set: {e}")

        logger.debug(f"FULL backup completed for {src}")
        finalize_backup_job(
            job_id=backup_job_id,
            status="completed",
            event_message="Full backup completed",
            total_files=total_files,
            total_size_bytes=total_size_bytes,
        )
        # Return the actual backup_set_dir instead of job_dst for consistency with incremental/differential
        return backup_set_dir, event_id, backup_set_id_string, tarball_paths, None, {"files": total_files, "bytes": total_size_bytes}

    except Exception as e:
        logger.error(f"An error occurred during the full backup process: {e}", exc_info=True)
        finalize_backup_job(
            job_id=backup_job_id,
            status="failed",
            event_message=f"Full backup failed: {e}",
        )
        raise
