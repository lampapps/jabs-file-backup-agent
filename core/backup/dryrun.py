import os
import socket
import time
import json
import yaml
from logger import setup_logger, timestamp
from .utils import get_all_files
import boto3
from botocore.exceptions import ClientError

from models.backup_sets import get_or_create_backup_set
from models.backup_jobs import insert_backup_job, finalize_backup_job
from models.backup_files import insert_files
from models.db_core_agent import get_db_connection
from monitoring_client import send_backup_stage


def check_s3_accessible(config, logger):
    """Check if S3 bucket is accessible and writable."""
    aws = config.get("aws", {})
    if not aws.get("enabled"):
        logger.info("S3 sync not enabled, skipping S3 check.")
        return True
    
    bucket = aws.get("bucket")
    region = aws.get("region")
    profile = aws.get("profile", "default")
    
    if not bucket:
        logger.error("S3 enabled but no bucket specified in config.")
        return False
    
    try:
        session = boto3.Session(profile_name=profile, region_name=region)
        s3 = session.resource('s3')
        
        # Try to access the bucket (this checks if it exists and we have permissions)
        s3.meta.client.head_bucket(Bucket=bucket)
        logger.info(f"S3 bucket '{bucket}' is accessible.")
        
        # Test write permissions with a small test object
        test_key = f"jabs_dryrun_test_{int(time.time())}.txt"
        s3.Bucket(bucket).put_object(Key=test_key, Body=b"dryrun test")
        s3.Object(bucket, test_key).delete()
        logger.info(f"S3 bucket '{bucket}' is writable.")
        return True
        
    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code == '404':
            logger.error(f"S3 bucket '{bucket}' does not exist.")
        elif error_code == 'AccessDenied':
            logger.error(f"Access denied to S3 bucket '{bucket}'. Check AWS credentials and permissions.")
        else:
            logger.error(f"S3 bucket '{bucket}' is not accessible: {e}")
        return False
    except Exception as e:
        logger.error(f"Error checking S3 bucket '{bucket}': {e}")
        return False

def run_dryrun_backup(config, backup_type="dryrun", encrypt=False, sync=False, event_id=None, server_set_id=None, job_config_path=None, global_config=None):
    """
    Perform a dryrun backup that mimics a full backup but only writes to the database.
    - Checks source and destination folder access
    - Creates database entries (backup set, job, and files)
    - Does NOT create archive files, directories, or HTML manifest
    - If S3 sync enabled, only checks bucket accessibility
    """
    job_name = config.get("job_name", "unknown_job")
    logger = setup_logger(job_name)
    logger.debug(f"Starting DRYRUN backup job '{job_name}' with provided config.")

    # Generate backup_set_name early for event reporting
    backup_set_name = timestamp()

    # Send stage update
    if event_id:
        send_backup_stage(
            job_name=job_name,
            backup_type=backup_type,
            backup_set_id=event_id,
            backup_set_name=backup_set_name,
            stage=f"Initializing dryrun backup for {job_name}",
            encrypt=encrypt,
            sync=sync
        )

    src = config.get("source")
    dest = config.get("destination")

    # Send stage update for path validation
    if event_id:
        send_backup_stage(
            job_name=job_name,
            backup_type=backup_type,
            backup_set_id=event_id,
            backup_set_name=backup_set_name,
            stage="Validating source and destination paths",
            encrypt=encrypt,
            sync=sync
        )

    # Test source folder
    if not src or not os.path.exists(src):
        error_msg = f"Source path does not exist: {src}"
        logger.error(error_msg)
        return None, event_id, None

    if not os.access(src, os.R_OK):
        error_msg = f"Source path is not readable: {src}"
        logger.error(error_msg)
        return None, event_id, None

    # Test destination folder
    if not dest or not os.path.exists(dest):
        error_msg = f"Destination path does not exist: {dest}"
        logger.error(error_msg)
        return None, event_id, None

    if not os.access(dest, os.W_OK):
        error_msg = f"Destination path is not writable: {dest}"
        logger.error(error_msg)
        return None, event_id, None

    # Test S3 if sync is enabled
    if sync:
        if event_id:
            send_backup_stage(
                job_name=job_name,
                backup_type=backup_type,
                backup_set_id=event_id,
                backup_set_name=backup_set_name,
                stage="Checking S3 bucket access",
                encrypt=encrypt,
                sync=sync
            )

        if not check_s3_accessible(config, logger):
            error_msg = "S3 bucket is not accessible or writable."
            logger.error(error_msg)
            return None, event_id, None

    # Path setup (for validation only - no directories created)
    machine_name = socket.gethostname()
    sanitized_job_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in job_name)
    sanitized_machine_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in machine_name)
    job_dst = os.path.join(dest, sanitized_machine_name, sanitized_job_name)

    # Use event_id (backup_set_id) for consistency, generate human-readable name for filesystem
    backup_set_id = event_id if event_id else f"{socket.gethostname()}_{int(time.time())}"
    backup_set_dir = os.path.join(job_dst, f"backup_set_{backup_set_name}")
    backup_set_id_string = backup_set_name  # For database storage

    logger.info(f"DRYRUN: Would create backup in: {backup_set_dir}")
    
    # Update event for exclude patterns
    if event_id:
        send_backup_stage(
            job_name=job_name,
            backup_type=backup_type,
            backup_set_id=event_id,
            backup_set_name=backup_set_name,
            stage="Loading exclude patterns",
            encrypt=encrypt,
            sync=sync
        )

    # Use the centralized function to get merged exclude patterns
    from .utils import get_merged_exclude_patterns
    exclude_patterns = get_merged_exclude_patterns(config, global_config, job_config_path, logger)

    # Update event for scanning files
    if event_id:
        send_backup_stage(
            job_name=job_name,
            backup_type=backup_type,
            backup_set_id=event_id,
            backup_set_name=backup_set_name,
            stage="Scanning for files to backup (dry run)",
            encrypt=encrypt,
            sync=sync
        )
    
    # Get files that would be backed up
    files = get_all_files(src, exclude_patterns, logger=logger, job_name=job_name)
    logger.info(f"DRYRUN: Found {len(files)} files that would be archived.")

    if not files:
        logger.warning("DRYRUN: No files found to backup.")
        if event_id:
            send_backup_stage(
                job_name=job_name,
                backup_type=backup_type,
                backup_set_id=event_id,
                backup_set_name=backup_set_name,
                stage="No files found for dryrun backup",
                encrypt=encrypt,
                sync=sync
            )
        return "skipped", event_id, backup_set_id_string

    # Get the backup job ID from the event
    # In our schema, the event ID IS the backup job ID
    backup_job_id = event_id
    backup_set_id = None
    
    if backup_job_id:
        logger.info(f"Using event_id as backup_job_id: {backup_job_id}")
        # Get the backup set ID associated with this job
        with get_db_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute('SELECT backup_set_id FROM backup_jobs WHERE id = ?', (backup_job_id,))
                job_result = cursor.fetchone()
                if job_result and job_result['backup_set_id']:
                    backup_set_id = job_result['backup_set_id']
                    logger.info(f"Using backup set ID {backup_set_id} from job {backup_job_id}")
            except Exception as e:
                logger.warning(f"Could not get backup set ID from backup job: {e}")

    # Create database entries for the dryrun if we don't have them already
    try:
        # Update event for database creation
        if event_id:
            send_backup_stage(
                job_name=job_name,
                backup_type=backup_type,
                backup_set_id=event_id,
                backup_set_name=backup_set_name,
                stage="Creating database entries (dry run)",
                encrypt=encrypt,
                sync=sync
            )
        
        # If we don't have a backup_job_id or backup_set_id from the event,
        # we need to create them (should not happen with proper CLI event creation)
        if not backup_job_id or not backup_set_id:
            # Step 1: Create backup set in database
            config_snapshot = json.dumps(config) if config else None
            backup_set_id = get_or_create_backup_set(
                job_name=job_name,
                set_name=backup_set_id_string,
                config_settings=config_snapshot,
                source_path=config.get('source') if config else None  # Add source path
            )
            
            # Step 2: Create backup job in database
            backup_job_id = insert_backup_job(
                backup_set_id=backup_set_id,
                backup_type="dryrun",
                encrypted=encrypt,
                synced=sync,
                event_message="Dryrun backup started"
            )
            
            logger.info(f"DRYRUN: Created database entries - backup_set_id={backup_set_id}, job_id={backup_job_id}")
        else:
            logger.info(f"DRYRUN: Using existing database entries - backup_set_id={backup_set_id}, job_id={backup_job_id}")

        # Step 3: Create file records for database
        total_size_bytes = 0
        file_records = []

        if event_id:
            send_backup_stage(
                job_name=job_name,
                backup_type=backup_type,
                backup_set_id=event_id,
                backup_set_name=backup_set_name,
                stage="Processing file information (dry run)",
                encrypt=encrypt,
                sync=sync
            )
            
        for file_path in files:
            try:
                stat_info = os.stat(file_path)
                rel_path = os.path.relpath(file_path, src)
                file_size = stat_info.st_size
                total_size_bytes += file_size
                
                file_records.append({
                    "tarball": f"dryrun_{backup_set_id_string}.tar.gz",  # Simulated tarball name
                    "path": rel_path,
                    "mtime": stat_info.st_mtime,
                    "size": file_size,
                    "is_new": True,
                    "is_modified": False
                })
                
            except OSError as e:
                logger.warning(f"DRYRUN: Could not stat file {file_path}: {e}")
                continue

        # Step 4: Insert file records into database
        if file_records:
            logger.info(f"DRYRUN: Inserting {len(file_records)} file records into database...")

            if event_id:
                send_backup_stage(
                    job_name=job_name,
                    backup_type=backup_type,
                    backup_set_id=event_id,
                    backup_set_name=backup_set_name,
                    stage=f"Adding {len(file_records)} file records to database (dry run)",
                    encrypt=encrypt,
                    sync=sync
                )
                
            insert_files(backup_job_id, file_records)
            
            # Mark the backup job as completed in database
            finalize_backup_job(
                job_id=backup_job_id,
                status="completed",
                event_message="Dryrun backup completed successfully",
                total_files=len(file_records),
                total_size_bytes=total_size_bytes
            )
            
            logger.info(f"DRYRUN: Backup job completed with {len(file_records)} files, {total_size_bytes} bytes")
        else:
            # No valid files
            finalize_backup_job(
                job_id=backup_job_id,
                status="completed",
                event_message="Dryrun completed with no valid files"
            )
            logger.info("DRYRUN: No valid files to record")

        # Log what would happen in a real backup
        logger.info(f"DRYRUN: Would create directory: {backup_set_dir}")
        logger.info(f"DRYRUN: Would create {len(file_records)} archive files")
        logger.info(f"DRYRUN: Would generate HTML manifest")
        if encrypt:
            logger.info("DRYRUN: Would encrypt archive files")
        if sync:
            logger.info("DRYRUN: Would sync to S3")

        # Important: Don't finalize the event here!
        # Just update it with progress information
        if event_id:
            send_backup_stage(
                job_name=job_name,
                backup_type=backup_type,
                backup_set_id=event_id,
                backup_set_name=backup_set_name,
                stage=f"Dryrun Manifest ({len(file_records)} files)",
                encrypt=encrypt,
                sync=sync
            )

        logger.debug(f"DRYRUN backup completed for {src}")
        return backup_set_dir, event_id, backup_set_id_string

    except Exception as e:
        logger.error(f"Error during dryrun backup: {e}", exc_info=True)
        
        # Try to mark job as failed if we got far enough to create it
        try:
            if 'backup_job_id' in locals():
                finalize_backup_job(
                    job_id=backup_job_id,
                    status="error",
                    error_message=str(e),
                    event_message=f"Dryrun backup failed: {e}"
                )
        except Exception as db_e:
            logger.error(f"Failed to update database with error status: {db_e}")

        raise