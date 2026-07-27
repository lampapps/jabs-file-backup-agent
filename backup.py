#!/venv/bin/python3
"""JABS Backup Agent: Execute backup jobs with encryption and cloud sync support.

Primary entry point for running individual backup jobs. Can be called directly
from the terminal or via cron by the scheduler.

Usage:
    python backup.py --job JOB_NAME [--type full|incremental|differential|dryrun] [--encrypt] [--sync]
    Examples:
        python backup.py --job "Jim Home"
        python backup.py --job "Jim Home" --type full --encrypt --sync
        python backup.py --job config/jobs/example.yaml --type incremental
"""

import argparse
import os
import sys
import subprocess
import time
import uuid
from pathlib import Path
from dotenv import load_dotenv
import yaml
import socket

from logger import setup_logger
from settings import GLOBAL_CONFIG_PATH, LOCK_DIR, CONFIG_DIR, ENV_PATH, BASE_DIR
from core.sync_s3 import sync_to_s3
from core.encrypt import encrypt_tarballs
from core.backup import run_backup
from emailer import process_email_event
from core.backup.common import acquire_lock, release_lock, rotate_backups
from monitoring_client import (
    send_event, send_backup_start, send_backup_stage, send_backup_complete
)

# Set the working directory to the project root
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# Create a module-level logger
cli_logger = setup_logger("cli")

# Load .env file from the path defined in settings
load_dotenv(ENV_PATH)

# Get the passphrase
PASSPHRASE = os.getenv("JABS_ENCRYPT_PASSPHRASE")

# Get AWS profile from environment
AWS_PROFILE = os.getenv("AWS_PROFILE")
if AWS_PROFILE:
    os.environ["AWS_PROFILE"] = AWS_PROFILE

# Event tracking via monitoring API
event_counter = {}  # Track event IDs locally for reference


def create_event(job_name="", event_message="", backup_type="", encrypt=False, sync=False, config=None):
    """
    Create a backup event and report to the dashboard.

    For full backups: generates a new UUID as the dashboard-side backup_set_id (group identifier).
    For incremental/differential: looks up the parent full backup's UUID so the dashboard can group them.
    Each run also gets a unique run_id UUID for per-row dashboard lookup.

    Returns a unique run_id for tracking this backup operation.
    """
    from logger import timestamp
    from models.backup_sets import get_last_set_info_for_job

    start_time = int(time.time())
    run_id = str(uuid.uuid4())

    if backup_type == "full":
        server_set_id = str(uuid.uuid4())
        backup_set_name = timestamp()
    else:
        # incremental or differential — share the parent full's server_set_id
        parent_info = get_last_set_info_for_job(job_name)
        if parent_info and parent_info.get('server_set_id'):
            server_set_id = parent_info['server_set_id']
            backup_set_name = parent_info['set_name']
        else:
            # No completed full found; will fall back to full — generate new UUID
            server_set_id = str(uuid.uuid4())
            backup_set_name = timestamp()

    event_counter[run_id] = {
        "job_name": job_name,
        "backup_type": backup_type,
        "message": event_message,
        "start_time": start_time,
        "run_id": run_id,
        "server_set_id": server_set_id,
        "backup_set_name": backup_set_name,
        "encrypt": encrypt,
        "sync": sync
    }

    # Send backup start event
    send_backup_start(
        job_name=job_name,
        backup_type=backup_type,
        run_id=run_id,
        backup_set_id=server_set_id,
        backup_set_name=backup_set_name,
        encrypt=encrypt,
        sync=sync
    )

    return run_id


def update_event(event_id="", event_message="", status="running"):
    """
    Update an ongoing backup event with stage information.
    """
    if event_id and event_id in event_counter:
        event_counter[event_id]["message"] = event_message
        event_counter[event_id]["status"] = status

    # Report update to dashboard with stage description
    if event_id and event_id in event_counter:
        event_info = event_counter[event_id]
        send_backup_stage(
            job_name=event_info.get("job_name", ""),
            backup_type=event_info.get("backup_type", ""),
            run_id=event_info.get("run_id", event_id),
            backup_set_id=event_info.get("server_set_id", ""),
            backup_set_name=event_info.get("backup_set_name", ""),
            stage=event_message,
            encrypt=event_info.get("encrypt", False),
            sync=event_info.get("sync", False)
        )


def finalize_event(event_id="", status="completed", event_message="", backup_set_id=None, runtime=0,
                    files_backed_up=None, bytes_backed_up=None, bytes_compressed=None):
    """
    Finalize a backup event when complete or failed.
    """
    if event_id and event_id in event_counter:
        event_counter[event_id]["status"] = status
        event_counter[event_id]["message"] = event_message
        event_counter[event_id]["end_time"] = int(time.time())

    # Report final status to dashboard

    if event_id and event_id in event_counter:
        event_info = event_counter[event_id]
        job_name = event_info.get("job_name", "")
        backup_type = event_info.get("backup_type", "")
        encrypt = event_info.get("encrypt", False)
        sync = event_info.get("sync", False)

        run_id = event_info.get("run_id", event_id)
        final_backup_set_id = event_info.get("server_set_id", "")
        final_backup_set_name = event_info.get("backup_set_name", "")

        # Calculate duration if available
        start_time = event_info.get("start_time", 0)
        duration_seconds = (event_info.get("end_time", int(time.time())) - start_time) if start_time else runtime

        # Send appropriate completion event
        if status in ("completed", "success"):
            send_backup_complete(
                job_name=job_name,
                backup_type=backup_type,
                run_id=run_id,
                backup_set_id=final_backup_set_id,
                backup_set_name=final_backup_set_name,
                duration_seconds=duration_seconds,
                encrypt=encrypt,
                sync=sync,
                success=True,
                files_backed_up=files_backed_up or 0,
                bytes_backed_up=bytes_backed_up or 0,
                bytes_compressed=bytes_compressed or 0
            )
            process_email_event(
                "backup_complete",
                subject=f"JABS Backup Completed: {job_name} ({socket.gethostname()})",
                body=(
                    f"Job: {job_name}\n"
                    f"Type: {backup_type}\n"
                    f"Host: {socket.gethostname()}\n"
                    f"Status: {status}\n"
                    f"Duration: {duration_seconds}s\n"
                    f"Files backed up: {files_backed_up or 0}\n"
                    f"Bytes backed up: {bytes_backed_up or 0}\n"
                    f"Backup set: {final_backup_set_name}\n"
                    f"Message: {event_message}\n"
                )
            )
        elif status in ("error", "failed"):
            send_backup_complete(
                job_name=job_name,
                backup_type=backup_type,
                run_id=run_id,
                backup_set_id=final_backup_set_id,
                backup_set_name=final_backup_set_name,
                duration_seconds=duration_seconds,
                encrypt=encrypt,
                sync=sync,
                success=False,
                error_message=event_message,
                files_backed_up=files_backed_up or 0,
                bytes_backed_up=bytes_backed_up or 0,
                bytes_compressed=bytes_compressed or 0
            )
            process_email_event(
                "error",
                subject=f"JABS Backup FAILED: {job_name} ({socket.gethostname()})",
                body=(
                    f"Job: {job_name}\n"
                    f"Type: {backup_type}\n"
                    f"Host: {socket.gethostname()}\n"
                    f"Status: {status}\n"
                    f"Duration: {duration_seconds}s\n"
                    f"Backup set: {final_backup_set_name}\n"
                    f"Error: {event_message}\n"
                )
            )
        elif status == "skipped":
            # Still a terminal outcome — send as a completion event (event_type
            # "backup_complete") with status="skipped" so the dashboard finalizes
            # the job instead of leaving it stuck as "running".
            send_event(
                event_type="backup_complete",
                message=event_message,
                run_id=run_id,
                backup_set_id=final_backup_set_id,
                job_name=job_name,
                backup_type=backup_type,
                backup_set_name=final_backup_set_name,
                encrypt=encrypt,
                sync=sync,
                stage="Skipped",
                status="skipped",
                duration_seconds=duration_seconds
            )
        else:
            send_event(
                event_type="warning",
                message=event_message,
                run_id=run_id,
                backup_set_id=final_backup_set_id,
                job_name=job_name,
                backup_type=backup_type,
                encrypt=encrypt,
                sync=sync,
                stage=event_message
            )


def get_event_status(event_id=""):
    """
    Get the status of a backup event.
    """
    if event_id in event_counter:
        return event_counter[event_id].get("status", "unknown")
    return None


def event_exists(event_id=""):
    """
    Check if an event exists.
    """
    return event_id in event_counter

try:
    def merge_dicts(global_dict, job_dict):
        """Merge two dicts, with job_dict taking precedence."""
        merged = (global_dict or {}).copy()
        merged.update(job_dict or {})
        return merged

    def _resolve_hook_script_path(script_path: str) -> str:
        """Resolve a hook script path and restrict it to the repo's scripts/ directory."""
        if not script_path or not isinstance(script_path, str):
            raise ValueError("Hook script path must be a non-empty string")

        scripts_dir = Path(BASE_DIR) / "scripts"

        raw = Path(script_path)
        # Allow bare filenames (e.g. "pre.sh") -> scripts/pre.sh
        if not raw.is_absolute() and len(raw.parts) == 1:
            candidate = scripts_dir / raw
        else:
            candidate = raw if raw.is_absolute() else (Path(BASE_DIR) / raw)

        resolved = candidate.resolve()
        scripts_dir_resolved = scripts_dir.resolve()

        # Ensure the resolved path stays inside scripts/
        try:
            resolved.relative_to(scripts_dir_resolved)
        except ValueError as e:
            raise ValueError(f"Hook script must be under {scripts_dir_resolved}") from e

        if not resolved.exists() or not resolved.is_file():
            raise FileNotFoundError(f"Hook script not found: {resolved}")

        return str(resolved)

    def _log_subprocess_output(logger, prefix: str, output: str, level: str = "info"):
        if not output:
            return
        for line in output.splitlines():
            msg = f"{prefix}: {line}" if line else prefix
            if level == "error":
                logger.error(msg)
            else:
                logger.info(msg)

    def run_hook_script(*, hook: str, script_path: str, logger, event_id: int, job_name: str, backup_type: str) -> bool:
        """Run a pre/post bash script (stored under scripts/) and log status/output."""
        resolved_path = _resolve_hook_script_path(script_path)
        display_name = os.path.basename(resolved_path)

        if event_id and event_exists(event_id):
            update_event(
                event_id=event_id,
                event_message=f"Running {hook} script: {display_name}",
                status="running",
            )

        logger.info(f"Running {hook} script: {resolved_path}")
        start = time.time()

        env = os.environ.copy()
        env.update(
            {
                "JABS_HOOK": hook,
                "JABS_JOB_NAME": job_name,
                "JABS_BACKUP_TYPE": backup_type,
                "JABS_EVENT_ID": str(event_id or ""),
            }
        )

        result = subprocess.run(
            ["bash", resolved_path],
            cwd=BASE_DIR,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        elapsed = time.time() - start
        _log_subprocess_output(logger, f"{hook} stdout", result.stdout, level="info")
        _log_subprocess_output(logger, f"{hook} stderr", result.stderr, level="error")

        if result.returncode == 0:
            logger.info(f"{hook} script succeeded ({display_name}) in {elapsed:.2f}s")
            if event_id and event_exists(event_id):
                update_event(
                    event_id=event_id,
                    event_message=f"{hook.capitalize()} script succeeded: {display_name}",
                    status="running",
                )
            return True

        logger.error(f"{hook} script failed ({display_name}) exit={result.returncode} in {elapsed:.2f}s")
        if event_id and event_exists(event_id):
            update_event(
                event_id=event_id,
                event_message=f"{hook.capitalize()} script failed: {display_name} (exit {result.returncode})",
                status="running",
            )
        return False

    def run_job(config_path, backup_type, encrypt=False, sync=False):
        """Run a backup job with the given configuration."""
        lock_file = None
        try:
            # Load job configuration
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            
            # Load global configuration and merge with job config
            global_config = {}
            try:
                with open(GLOBAL_CONFIG_PATH, encoding='utf-8') as f:
                    global_config = yaml.safe_load(f)
            except (OSError, yaml.YAMLError) as e:
                cli_logger.warning(f"Could not load global config: {e}")
                
            # Merge nested dicts for aws and encryption
            config["aws"] = merge_dicts(global_config.get("aws"), config.get("aws"))
            config["encryption"] = merge_dicts(global_config.get("encryption"), config.get("encryption"))

            # Merge all missing flat values from global config (including destination)
            for key, value in global_config.items():
                # Skip keys that are already processed as nested dicts (aws, encryption)
                if key in ["aws", "encryption"]:
                    continue
                    
                # Apply global config values if the key is missing or None in job config
                if key not in config or config[key] is None:
                    config[key] = value

            # Determine if encryption and sync should be enabled
            # Either from command line or from config
            encrypt_effective = encrypt or config.get("encryption", {}).get("enabled", False)
            sync_effective = sync or config.get("aws", {}).get("enabled", False)

            # Get the job name from the config
            job_name = config.get("job_name", "unknown")

            # Set up the logger with the job name
            logger = setup_logger(job_name)
            logger.info(f"###### Starting {backup_type.upper()} backup ######")

            # Optional hook scripts (must live under scripts/)
            pre_script = config.get("pre_script")
            post_script = config.get("post_script")

            # ACQUIRE LOCK FIRST before doing anything ELSE
            os.makedirs(LOCK_DIR, exist_ok=True)
            job_name_sanitized = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in job_name)
            lock_file_path = os.path.join(LOCK_DIR, f"{job_name_sanitized}.lock")
            
            # Try to acquire the lock, catching any lock exceptions
            try:
                lock_file = acquire_lock(lock_file_path)
            except RuntimeError as e:
                # This catches the lock acquisition error from common.py
                lock_error_msg = f"ERROR: {str(e)}"
                
                # Log using error level for file logs, but with cleaner output for terminal
                logger.error(lock_error_msg)
                
                # Exit directly from CLI
                if __name__ == "__main__":
                    print(lock_error_msg)
                    sys.exit(2)  # Exit with error code 2 for lock errors
                    
                return "locked"  # Simple return for programmatic use
    
            # Debug the config object
            logger.debug(f"DEBUG: create_event: config type: {type(config)}")
            logger.debug(f"DEBUG: create_event: config empty? {not bool(config)}")
            logger.debug(f"DEBUG: create_event: config keys: {list(config.keys()) if isinstance(config, dict) and config else 'None'}")

            # --- Initialize the event and capture the event_id ---
            event_id = create_event(
                job_name=job_name,
                event_message=f"Starting {backup_type} backup",
                backup_type=backup_type,
                encrypt=encrypt_effective,
                sync=sync_effective,
                config=config  # Pass the config here
            )

            # --- Pre-backup hook ---
            if pre_script:
                ok = run_hook_script(
                    hook="pre",
                    script_path=pre_script,
                    logger=logger,
                    event_id=event_id,
                    job_name=job_name,
                    backup_type=backup_type,
                )
                if not ok:
                    finalize_event(
                        event_id=event_id,
                        status="error",
                        event_message=f"Pre script failed: {os.path.basename(str(pre_script))}",
                        backup_set_id=None,
                        runtime=int(time.time()) - event_counter.get(event_id, {}).get("start_time", int(time.time()))
                    )
                    return False
    
            # --- Run the backup operation ---
            backup_result = run_backup(
                config,
                backup_type,
                encrypt=encrypt_effective,  # Pass effective flags for event reporting
                sync=sync_effective,         # Actual encryption/sync happens after backup
                event_id=event_id,  # Pass our event_id to be updated, not finalized
                server_set_id=event_counter.get(event_id, {}).get("server_set_id"),
                job_config_path=config_path,
                global_config=global_config
            )
            
            # Unpack the backup result
            # The result could be:
            # - For successful backups: (backup_set_dir, event_id, backup_set_id_str, tarball_paths, error_detail, stats)
            # - For skipped backups: ("skipped", event_id, None, None, None, None)
            # - For failed backups: (None, event_id, None, None, error_msg, None)

            if isinstance(backup_result, tuple) and len(backup_result) >= 6:
                latest_backup_set, event_id, backup_set_id_str, tarball_paths, error_detail, backup_stats = backup_result
            elif isinstance(backup_result, tuple) and len(backup_result) >= 5:
                latest_backup_set, event_id, backup_set_id_str, tarball_paths, error_detail = backup_result
                backup_stats = None
            elif isinstance(backup_result, tuple) and len(backup_result) >= 4:
                latest_backup_set, event_id, backup_set_id_str, tarball_paths = backup_result
                error_detail = None
                backup_stats = None
            else:
                # Handle old-style return values for backward compatibility
                latest_backup_set, event_id, backup_set_id_str = backup_result
                tarball_paths = []
                error_detail = None
                backup_stats = None

            files_backed_up = (backup_stats or {}).get("files", 0)
            bytes_backed_up = (backup_stats or {}).get("bytes", 0)

            # Track desired final outcome; post_script may adjust status.
            desired_status = None
            desired_message = None

            # --- Check for backup failure (backup function returned None without raising) ---
            if latest_backup_set is None:
                desired_status = "error"
                desired_message = error_detail or "Backup failed — check agent logs for details"

            # --- Check for skipped diff or incremental backup ---
            elif backup_type in ["diff", "differential", "incremental"] and latest_backup_set == "skipped":
                logger.info("No files modified. Backup skipped.")
                desired_status = "skipped"
                desired_message = "No files modified. Backup skipped."
                
            # --- Encryption if requested ---
            if encrypt_effective and tarball_paths:
                logger.debug("Starting encryption of backup files")
                update_event(
                    event_id=event_id,
                    event_message="Encrypting backup files",
                    status="running"
                )
                try:
                    tarball_paths = encrypt_tarballs(tarball_paths, config, logger)
                    update_event(
                        event_id=event_id,
                        event_message="Encryption completed",
                        status="running"
                    )
                except Exception as e:
                    logger.error(f"Encryption failed: {e}", exc_info=True)
                    update_event(
                        event_id=event_id,
                        event_message=f"Encryption failed: {e}",
                        status="running"
                    )
                    # Continue with the backup process, don't fail the entire job

            # --- S3 sync if requested - KEEP LOCK DURING SYNC ---
            if sync_effective and latest_backup_set:
                logger.debug("Starting sync to S3")
                update_event(
                    event_id=event_id,  # Always use our original event_id
                    event_message="Sync to S3 started",
                    status="running"
                )
                sync_result = sync_to_s3(latest_backup_set, config, event_id)  # Pass our event_id
                
                if not sync_result:
                    logger.warning("S3 sync was skipped or failed but continuing with backup process")
                    # The sync_to_s3 function will have updated the event with the reason

            # --- Rotate Backups ---
            # RESOLVE keep_sets
            keep_sets = config.get("keep_sets", None)
            if keep_sets is None and global_config is not None:
                keep_sets = global_config.get("keep_sets", None)
            if keep_sets is None:
                keep_sets = 5  # fallback default
            keep_sets = int(keep_sets)
 
            # RECREATE job_dst path
            machine_name = socket.gethostname()
            sanitized_job_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in job_name)
            sanitized_machine_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in machine_name)
            
            # Get destination from config
            dest = config.get("destination")
            if not dest:
                logger.error("No destination specified in config")
                return False
                
            job_dst = os.path.join(dest, sanitized_machine_name, sanitized_job_name)
            
            # call rotate_backups
            rotate_backups(job_dst, keep_sets, logger, config)

            # --- Post-backup hook (runs after backup + optional encrypt/sync/rotate) ---
            if post_script:
                ok = run_hook_script(
                    hook="post",
                    script_path=post_script,
                    logger=logger,
                    event_id=event_id,
                    job_name=job_name,
                    backup_type=backup_type,
                )
                if not ok:
                    # Warn-only: do not fail the backup due to post hook.
                    logger.warning(
                        "Post script failed (warn-only); backup artifacts were created successfully"
                    )
                    # Preserve the primary outcome message but surface the warning in the UI.
                    warning_msg = f"WARN: Post script failed (warn-only): {os.path.basename(str(post_script))}"
                    if event_id and event_exists(event_id):
                        update_event(
                            event_id=event_id,
                            event_message=warning_msg,
                            status="running",
                        )
                    if desired_message:
                        desired_message = f"{desired_message} | {warning_msg}"
                    else:
                        desired_message = warning_msg

            # If no explicit status was selected (e.g. full backup), default to success.
            if desired_status is None:
                desired_status = "success"
                desired_message = f"Backup Set ID: {backup_set_id_str}" if backup_set_id_str is not None else "Backup completed"

            # Calculate runtime for final event
            runtime = int(time.time()) - event_counter.get(event_id, {}).get("start_time", int(time.time()))

            # For skipped backups, use 0 runtime to display "-" instead of a duration
            if desired_status == "skipped":
                runtime = 0

            # Compressed footprint: sum the on-disk size of the final tarballs
            # (post-encryption if encryption was applied) for reporting to the dashboard.
            bytes_compressed = 0
            if tarball_paths:
                for tar_path in tarball_paths:
                    try:
                        bytes_compressed += os.path.getsize(tar_path)
                    except OSError:
                        pass

            # --- Finalize the event as successful ---
            if event_exists(event_id):
                current_status = get_event_status(event_id)
                # DB/view status uses values like: running, completed, failed, skipped.
                if current_status not in ("error", "failed", "completed", "skipped", "success"):
                    finalize_event(
                        event_id=event_id,
                        status=desired_status,
                        event_message=desired_message,
                        runtime=runtime,
                        files_backed_up=files_backed_up,
                        bytes_backed_up=bytes_backed_up,
                        bytes_compressed=bytes_compressed
                    )

            logger.info("Backup operation completed successfully")

            return True  # Successful completion

        except Exception as e:
            logger = setup_logger("cli_error", log_file="cli_error.log")
            logger.error(f"Fatal error during execution: {e}", exc_info=True)

            # Always notify the dashboard so the job doesn't stay stuck in "running"
            if event_id and event_id in event_counter:
                try:
                    runtime = int(time.time()) - event_counter.get(event_id, {}).get("start_time", int(time.time()))
                    finalize_event(
                        event_id=event_id,
                        status="error",
                        event_message=f"Fatal error: {e}",
                        runtime=runtime
                    )
                except Exception as fe:
                    logger.error(f"Failed to send error event to dashboard: {fe}")

            if __name__ == "__main__":
                print(f"ERROR: {e}")

            return None  # Indicate failure
        finally:
            # Always release the lock file if we acquired it
            if lock_file:
                try:
                    logger.debug("Released lock file for job")
                    release_lock(lock_file)
                except Exception as e:  # pylint: disable=broad-except
                    logger.error(f"Error releasing lock file: {e}")
                    # Even if we fail to release the lock, don't raise an exception
                    # as this would obscure the original error

    if __name__ == "__main__":
        parser = argparse.ArgumentParser(description="JABS CLI")
        parser.add_argument("--job", required=True, help="Path to job config file or job name")
        parser.add_argument("--type", choices=["full", "diff", "differential", "incremental", "dry_run", "dryrun"],
                            default="incremental", help="Type of backup to perform")
        parser.add_argument("--encrypt", action="store_true", help="Encrypt the backup")
        parser.add_argument("--sync", action="store_true", help="Sync to S3 after backup")

        args = parser.parse_args()

        # If the job argument is not a path to a config file, assume it's a job name
        job_config_path = args.job
        if not job_config_path.endswith((".yaml", ".yml")) or not os.path.exists(job_config_path):
            # Try to find the config file in the jobs directory
            job_name = args.job
            job_config_path = os.path.join(CONFIG_DIR, "jobs", f"{job_name}.yaml")
            if not os.path.exists(job_config_path):
                cli_logger.error(f"Config file not found: {job_config_path}")
                print(f"ERROR: Config file not found: {job_config_path}")
                sys.exit(1)

        backup_type = args.type
        if backup_type in ["diff"]:
            backup_type = "differential"
        if backup_type in ["dry_run"]:
            backup_type = "dryrun"

        run_job(job_config_path, backup_type, args.encrypt, args.sync)
except Exception as e:
    cli_logger.error(f"Fatal error: {e}", exc_info=True)
    print(f"ERROR: {e}")
    sys.exit(1)


