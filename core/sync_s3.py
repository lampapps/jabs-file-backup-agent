"""Sync a backup set directory to AWS S3 using the AWS CLI."""

import os
import subprocess
import socket
import shutil
import time
import signal

from logger import setup_logger
from models.backup_jobs import update_job_sync_status
from emailer import process_email_event

# Stub functions (dashboard-side features)
def update_event(*args, **kwargs):
    """Stub: Events are reported to the dashboard via API."""
    pass

def event_exists(*args, **kwargs):
    """Stub: Events are reported to the dashboard via API."""
    return False


def check_aws_credentials(logger):
    """
    Check if AWS CLI is available and credentials are valid.
    Returns True if credentials appear valid, False otherwise.
    """
    # First, check if AWS CLI is installed
    if not shutil.which("aws"):
        logger.warning("AWS CLI not found. Cannot perform sync.")
        return False

    # Check if the required environment variables or AWS config files exist
    aws_access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    aws_secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")

    if aws_access_key and aws_secret_key:
        logger.debug("AWS credentials found in environment variables.")
        return True

    # Check for AWS native credentials file (cross-platform)
    home_dir = os.path.expanduser("~")
    aws_creds_file = os.path.join(home_dir, ".aws", "credentials")
    aws_config_file = os.path.join(home_dir, ".aws", "config")

    if os.path.exists(aws_creds_file) or os.path.exists(aws_config_file):
        logger.debug("AWS credentials/config files found.")
        return True

    logger.warning("No AWS credentials found in environment variables or config files.")
    return False

def sync_to_s3(backup_set_path, config, event_id=None):
    # pylint: disable=too-many-locals
    """
    Sync only the current backup set directory to AWS S3.
    :param backup_set_path: Path to the latest backup set directory.
    :param config: Parsed YAML configuration with AWS details.
    :param event_id: The event ID to update.
    """
    job_name = config.get("job_name", "unknown")
    logger = setup_logger(job_name)

    # Get the backup job ID - in the events view, event.id is the same as job_id
    job_id = event_id  # Since event ID corresponds to backup_jobs.id in the events view

    aws_config = config.get("aws", {})
    # Get profile from environment variable first, fall back to config
    profile = os.environ.get("AWS_PROFILE") or aws_config.get("profile", "default")
    region = aws_config.get("region", None)
    bucket = aws_config.get("bucket")
    storage_class = aws_config.get("storage_class", "STANDARD")
    machine_name = socket.gethostname()
    prefix = machine_name

    # Check if we have AWS credentials in environment variables
    has_env_creds = bool(os.environ.get("AWS_ACCESS_KEY_ID") and
                         os.environ.get("AWS_SECRET_ACCESS_KEY"))

    if not bucket:
        error_msg = "AWS S3 bucket name is not specified in the configuration."
        logger.error(error_msg)
        if event_id and event_exists(event_id):
            update_event(
                event_id=event_id,
                event_message=f"S3 sync skipped: {error_msg}",
                status="running"  # Keep it as running so CLI can finalize
            )
        # Update job sync status to False
        if job_id:
            update_job_sync_status(job_id, False)
        return False

    # Extract the parent directory (job directory name) from the backup set path
    job_dir_name = os.path.basename(os.path.dirname(backup_set_path))
    backup_set_name = os.path.basename(backup_set_path)

    # Use the job_dir_name directly instead of re-sanitizing the job name
    # This ensures we use the exact same directory name that's already on disk
    s3_path = f"s3://{bucket}/{prefix}/{job_dir_name}/{backup_set_name}"

    logger.info(
        f"Syncing backup set: {backup_set_path} to S3 bucket: {bucket} under "
        f"prefix: {prefix}/{job_dir_name}/{backup_set_name}"
    )

    if not os.path.isdir(backup_set_path):
        error_msg = f"Backup set directory does not exist: {backup_set_path}"
        logger.error(error_msg)
        if event_id and event_exists(event_id):
            update_event(
                event_id=event_id,
                event_message=f"S3 sync skipped: {error_msg}",
                status="running"
            )
        # Update job sync status to False
        if job_id:
            update_job_sync_status(job_id, False)
        return False

    # Validate AWS CLI and basic credential setup
    logger.debug("Checking for AWS CLI and credentials...")
    if not check_aws_credentials(logger):
        error_msg = "AWS CLI not available or credentials not configured. S3 sync will be skipped."
        logger.warning(error_msg)

        # Send an error email notification
        hostname = socket.gethostname()
        email_subject = f"[JABS] AWS Credentials Missing on {hostname} for Job '{job_name}'"
        email_body = f"""
JABS backup job could not complete S3 synchronization because AWS credentials are missing or invalid.

Job Name: {job_name}
Backup Set: {backup_set_name}
Host: {hostname}
Bucket: {bucket}
Error: {error_msg}

Please check your AWS credential configuration. You can configure AWS credentials in one of these ways:
1. Add AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY to your .env file
2. Configure AWS CLI credentials using 'aws configure' command
3. Set up an IAM role for the host if running on AWS

The backup job completed successfully but the S3 sync step was skipped.
"""
        # Use error event type for immediate notification
        process_email_event("error", email_subject, email_body)

        if event_id and event_exists(event_id):
            update_event(
                event_id=event_id,
                event_message=f"S3 sync skipped: {error_msg}",
                status="running"
            )
        # Update job sync status to False
        if job_id:
            update_job_sync_status(job_id, False)
        return False

    logger.debug(f"Checking if bucket '{bucket}' exists...")
    cmd = [
        "aws", "s3api", "head-bucket", "--bucket", bucket
    ]

    # Only use profile if we don't have environment credentials
    if not has_env_creds:
        cmd.extend(["--profile", profile])

    if region:
        cmd.extend(["--region", region])

    try:
        # Use a timeout to prevent hanging
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        # Wait for up to 10 seconds
        for _ in range(10):
            if process.poll() is not None:
                break
            time.sleep(1)

        # If still running after timeout, kill it
        if process.poll() is None:
            process.terminate()
            process.wait(2)  # Give it 2 more seconds to terminate
            if process.poll() is None:
                process.kill()  # Force kill if still not terminated

            logger.warning(f"Bucket check timed out after 10 seconds. Assuming bucket '{bucket}' doesn't exist.")
            raise subprocess.TimeoutExpired(cmd, 10)

        # Check the return code
        if process.returncode == 0:
            logger.debug(f"Bucket '{bucket}' exists.")
        else:
            stderr = process.stderr.read()
            logger.warning(f"Bucket check failed: {stderr}")
            raise subprocess.CalledProcessError(process.returncode, cmd, stderr=stderr)

    except (subprocess.SubprocessError, subprocess.TimeoutExpired) as e:
        logger.warning(f"Bucket '{bucket}' does not exist or cannot be accessed. Attempting to create it...")
        try:
            create_cmd = [
                "aws", "s3api", "create-bucket", "--bucket", bucket
            ]

            # Only use profile if we don't have environment credentials
            if not has_env_creds:
                create_cmd.extend(["--profile", profile])

            if region and region != "us-east-1":
                create_cmd.extend([
                    "--region", region,
                    "--create-bucket-configuration", f"LocationConstraint={region}"
                ])
            elif region:
                create_cmd.extend(["--region", region])

            # Use a timeout for bucket creation too
            process = subprocess.Popen(
                create_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            # Wait for up to 15 seconds
            for _ in range(15):
                if process.poll() is not None:
                    break
                time.sleep(1)

            # If still running after timeout, kill it
            if process.poll() is None:
                process.terminate()
                process.wait(2)
                if process.poll() is None:
                    process.kill()

                error_msg = "Bucket creation timed out after 15 seconds."
                logger.error(error_msg)

                # Send error email about bucket creation timeout
                hostname = socket.gethostname()
                email_subject = f"[JABS] AWS Bucket Creation Timeout on {hostname} for Job '{job_name}'"
                email_body = f"""
JABS backup job could not complete S3 synchronization because AWS bucket creation timed out.

Job Name: {job_name}
Backup Set: {backup_set_name}
Host: {hostname}
Bucket: {bucket}
Error: {error_msg}

The backup job completed successfully but the S3 sync step was skipped.
"""
                process_email_event("error", email_subject, email_body)

                if event_id and event_exists(event_id):
                    update_event(
                        event_id=event_id,
                        event_message=f"S3 sync skipped: {error_msg}",
                        status="running"
                    )
                # Update job sync status to False
                if job_id:
                    update_job_sync_status(job_id, False)
                return False

            if process.returncode != 0:
                stderr = process.stderr.read()
                error_msg = f"Failed to create bucket: {stderr}"
                logger.error(error_msg)

                # Send error email about bucket creation failure
                hostname = socket.gethostname()
                email_subject = f"[JABS] AWS Bucket Creation Failed on {hostname} for Job '{job_name}'"
                email_body = f"""
JABS backup job could not complete S3 synchronization because AWS bucket creation failed.

Job Name: {job_name}
Backup Set: {backup_set_name}
Host: {hostname}
Bucket: {bucket}
Error: {error_msg}

The backup job completed successfully but the S3 sync step was skipped.
"""
                process_email_event("error", email_subject, email_body)

                if event_id and event_exists(event_id):
                    update_event(
                        event_id=event_id,
                        event_message=f"S3 sync skipped: {error_msg}",
                        status="running"
                    )
                # Update job sync status to False
                if job_id:
                    update_job_sync_status(job_id, False)
                return False

            logger.debug(f"Bucket '{bucket}' created.")

        except Exception as bucket_error:
            error_msg = f"Failed to create or access bucket: {str(bucket_error)}"
            logger.error(error_msg)

            # Send error email about bucket access failure
            hostname = socket.gethostname()
            email_subject = f"[JABS] AWS Bucket Access Failed on {hostname} for Job '{job_name}'"
            email_body = f"""
JABS backup job could not complete S3 synchronization because AWS bucket access failed.

Job Name: {job_name}
Backup Set: {backup_set_name}
Host: {hostname}
Bucket: {bucket}
Error: {error_msg}

The backup job completed successfully but the S3 sync step was skipped.
"""
            process_email_event("error", email_subject, email_body)

            if event_id and event_exists(event_id):
                update_event(
                    event_id=event_id,
                    event_message=f"S3 sync skipped: {error_msg}",
                    status="running"
                )
            # Update job sync status to False
            if job_id:
                update_job_sync_status(job_id, False)
            return False

    try:
        cmd = [
            "aws", "s3", "sync", backup_set_path, s3_path,
            "--storage-class", storage_class,
            "--exclude", "*",  # Exclude everything first
            "--include", "*"   # Then include everything (forces processing)
        ]

        # Only use profile if we don't have environment credentials
        if not has_env_creds:
            cmd.extend(["--profile", profile])

        if region:
            cmd.extend(["--region", region])

        logger.info(
            f"Syncing {backup_set_path} to {s3_path} with storage class {storage_class} "
            f"(optimized for performance)..."
        )

        # Set AWS CLI configuration for better performance via environment variables
        env = os.environ.copy()
        env.update({
            # Performance optimizations
            'AWS_CLI_FILE_ENCODING': 'UTF-8',
            'AWS_MAX_ATTEMPTS': '3',
            'AWS_RETRY_MODE': 'adaptive',
            # S3 Transfer configuration
            'AWS_S3_MAX_CONCURRENT_REQUESTS': '20',     # Increase concurrent requests
            'AWS_S3_MAX_BANDWIDTH': '10MB',             # Limit bandwidth to 10MB/s
            'AWS_S3_MULTIPART_THRESHOLD': '64MB',       # Use multipart for files > 64MB
            'AWS_S3_MULTIPART_CHUNKSIZE': '16MB',       # 16MB chunks
            'AWS_S3_MAX_QUEUE_SIZE': '10000',           # Increase queue size
            'AWS_S3_USE_ACCELERATE_ENDPOINT': 'false'   # Disable acceleration (can be slow for some regions)
        })

        # Use Popen for better control over the process
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env
        )

        # For large backups, we don't want to kill the process, but we do want to
        # implement a keep-alive mechanism to prevent the script from stalling
        stdout_lines = []
        start_time = time.time()
        last_output_time = start_time

        # Use this loop to periodically check if the process is still running and has output
        while process.poll() is None:
            # Read output without blocking
            stdout_line = process.stdout.readline()
            if stdout_line:
                stdout_lines.append(stdout_line)
                last_output_time = time.time()
                # Log progress more frequently to show activity
                if len(stdout_lines) % 5 == 0:  # Every 5 files instead of 10
                    elapsed = time.time() - start_time
                    rate = len(stdout_lines) / elapsed if elapsed > 0 else 0
                    logger.debug(f"S3 sync in progress... {len(stdout_lines)} files processed ({rate:.1f} files/sec)")

            # Increase timeout for high-speed uploads (more data means longer individual operations)
            if time.time() - last_output_time > 120:  # 2 minutes for large files
                logger.warning("No output from AWS S3 sync for 2 minutes, checking status...")
                # Try sending SIGINFO (not available on Windows) or just continue waiting
                try:
                    if hasattr(signal, 'SIGINFO'):
                        process.send_signal(signal.SIGINFO)
                except:
                    pass
                last_output_time = time.time()  # Reset the counter

            # Sleep a bit to avoid CPU spinning
            time.sleep(0.5)

        # Process has finished, collect any remaining output
        stdout, stderr = process.communicate()
        stdout_lines.extend(stdout.splitlines())

        if process.returncode != 0:
            error_msg = f"S3 sync failed with return code {process.returncode}: {stderr}"
            logger.error(error_msg)

            # Send error email about sync failure
            hostname = socket.gethostname()
            email_subject = f"[JABS] AWS S3 Sync Failed on {hostname} for Job '{job_name}'"
            email_body = f"""
JABS backup job S3 synchronization failed.

Job Name: {job_name}
Backup Set: {backup_set_name}
Host: {hostname}
Bucket: {bucket}
Error: {error_msg}

The backup job completed successfully but the S3 sync step failed.
"""
            process_email_event("error", email_subject, email_body)

            if event_id and event_exists(event_id):
                update_event(
                    event_id=event_id,
                    event_message=f"S3 sync failed: {error_msg}",
                    status="running"
                )
            # Update job sync status to False
            if job_id:
                update_job_sync_status(job_id, False)
            return False

        files_synced = len(stdout_lines)
        elapsed_time = time.time() - start_time
        avg_rate = files_synced / elapsed_time if elapsed_time > 0 else 0
        
        # Calculate approximate data transfer rate if we can estimate file sizes
        logger.info(f"Sync to AWS S3 completed successfully: {files_synced} files processed in {elapsed_time:.1f}s ({avg_rate:.1f} files/sec)")

        # Update event with success message including performance info
        if event_id and event_exists(event_id):
            update_event(
                event_id=event_id,
                event_message=f"S3 sync completed: {files_synced} files uploaded to {bucket}/{prefix}/{job_dir_name}/{backup_set_name} in {elapsed_time:.1f}s",
                status="running"  # Keep it as running so CLI can finalize
            )

        # Update job sync status to True
        if job_id:
            update_job_sync_status(job_id, True)

        # Return without finalizing - let CLI do that
        return True
    except Exception as e:
        error_msg = f"An unexpected error occurred during sync: {str(e)}"
        logger.error(error_msg)

        # Send error email about unexpected sync error
        hostname = socket.gethostname()
        email_subject = f"[JABS] AWS S3 Sync Error on {hostname} for Job '{job_name}'"
        email_body = f"""
JABS backup job encountered an unexpected error during S3 synchronization.

Job Name: {job_name}
Backup Set: {backup_set_name}
Host: {hostname}
Bucket: {bucket}
Error: {error_msg}

The backup job completed successfully but the S3 sync step encountered an error.
"""
        process_email_event("error", email_subject, email_body)

        if event_id and event_exists(event_id):
            update_event(
                event_id=event_id,
                event_message=f"S3 sync failed: {error_msg}",
                status="running"  # Keep status as running to allow the CLI to continue
            )
        # Update job sync status to False
        if job_id:
            update_job_sync_status(job_id, False)
        return False
