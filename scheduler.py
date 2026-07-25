"""Scheduler for running backup jobs based on cron schedules."""

import os
import glob
import time
from datetime import datetime
import importlib.util

import yaml
from croniter import croniter
from dotenv import load_dotenv

from logger import setup_logger, trim_all_logs
from settings import CONFIG_DIR, LOG_DIR, CLI_SCRIPT, SCHEDULER_STATUS_FILE, SCHEDULE_TOLERANCE, VERSION, GLOBAL_CONFIG_PATH, ENV_PATH
from monitoring_client import send_scheduler_check

# --- Load .env file ---
load_dotenv(ENV_PATH)

# --- Check for AWS credentials ---
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")

# Set AWS credentials as environment variables if they exist in .env
# This makes them available to the AWS CLI when run by any user (including root)
if AWS_ACCESS_KEY_ID:
    os.environ["AWS_ACCESS_KEY_ID"] = AWS_ACCESS_KEY_ID
if AWS_SECRET_ACCESS_KEY:
    os.environ["AWS_SECRET_ACCESS_KEY"] = AWS_SECRET_ACCESS_KEY

# Get AWS profile from environment
AWS_PROFILE = os.getenv("AWS_PROFILE")
if AWS_PROFILE:
    os.environ["AWS_PROFILE"] = AWS_PROFILE

# --- Logging Setup ---
os.makedirs(LOG_DIR, exist_ok=True)
logger = setup_logger("scheduler", log_file="scheduler.log")

def merge_dicts(global_dict, job_dict):
    """Merge two dicts, with job_dict taking precedence."""
    merged = (global_dict or {}).copy()
    merged.update(job_dict or {})
    return merged

def load_yaml_config(path):
    """Load a YAML configuration file and return its contents."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.error(f"Config file not found: {path}")
        return None
    except yaml.YAMLError as e:
        logger.error(f"Error parsing YAML file {path}: {e}")
        return None
    except OSError as e:
        logger.error(f"Error loading config {path}: {e}")
        return None

def get_job_configs():
    """Return a list of job config file paths."""
    jobs_dir = os.path.join(CONFIG_DIR, "jobs")
    config_files = glob.glob(os.path.join(jobs_dir, "*.yaml"))
    return config_files

def should_trigger(cron_expr, now):
    """Return True if the cron expression matches the current time within tolerance."""
    try:
        cron = croniter(cron_expr, now)
        prev_run_time = cron.get_prev(datetime)
        next_run_time = cron.get_next(datetime)

        # We need to check if now is close to the previous execution time
        # AND that it's not just the next scheduled time minus our window
        time_since_prev = now - prev_run_time
        time_until_next = next_run_time - now

        # Only match if we're within tolerance of the previous time
        # AND we're closer to the previous time than the next time
        return (time_since_prev < SCHEDULE_TOLERANCE) and (time_since_prev < time_until_next), prev_run_time
    except (ValueError, TypeError) as e:
        logger.error(f"Invalid cron expression '{cron_expr}': {e}")
        return False, None

def update_status_file():
    """Update the scheduler status file with the current timestamp."""
    try:
        with open(SCHEDULER_STATUS_FILE, 'w', encoding='utf-8') as f:
            f.write(str(time.time()))
        logger.debug(f"Updated status file: {SCHEDULER_STATUS_FILE}")
    except OSError as e:
        logger.error(f"Failed to update status file {SCHEDULER_STATUS_FILE}: {e}")

def send_digest_email(global_config, now):
    """Return True if it's time to send the digest email based on cron syntax in config."""
    # Digest email is server-side only
    return False


def call_cli_run_job(config_path, backup_type, encrypt=False, sync=False):
    """
    Directly call the run_job function from cli.py without starting a subprocess.
    
    Args:
        config_path: Path to the job config file
        backup_type: Type of backup to perform (full, differential, incremental)
        encrypt: Whether to encrypt the backup
        sync: Whether to sync to S3 after backup
        
    Returns:
        The result from the run_job function or None if an error occurred
    """
    try:
        # Import cli.py using importlib to avoid circular imports
        spec = importlib.util.spec_from_file_location("cli", CLI_SCRIPT)
        cli_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli_module)

        # Call the run_job function directly
        logger.info(f"Calling cli.run_job({config_path}, {backup_type}, encrypt={encrypt}, sync={sync})")
        result = cli_module.run_job(config_path, backup_type, encrypt=encrypt, sync=sync)
        return result
    except Exception as e:
        logger.error(f"Error calling cli.run_job: {e}", exc_info=True)
        return None

def main():
    """Checks configurations, schedules, and calls cli.py directly if needed."""
    logger.info("--- Scheduler Check Started ---")
    now = datetime.now()

    config_files = get_job_configs()
    if not config_files:
        logger.info("No configuration files found in %s", os.path.join(CONFIG_DIR, "jobs"))
        logger.info("--- Scheduler Check Finished ---")
        return

    global_config = load_yaml_config(GLOBAL_CONFIG_PATH)

    triggered_jobs_count = 0
    triggered_jobs_info = []  # List to keep track of jobs that actually ran

    try:
        # Process each job config
        for config_path in config_files:
            job_name_from_file = os.path.splitext(os.path.basename(config_path))[0]

            # Load the job config
            config = load_yaml_config(config_path)
            if not config:
                logger.warning(f"Config file is empty or invalid: {config_path}")
                continue

            job_name = config.get("job_name", job_name_from_file)
            schedules = config.get('schedules', [])
            if not schedules:
                logger.debug(f"No schedules defined in {config_path}")
                continue

            # Check if any schedule matches
            for schedule in schedules:
                cron_expr = schedule.get('cron')
                enabled = schedule.get('enabled', False)
                backup_type = schedule.get('type', 'full')

                if not enabled or not cron_expr:
                    continue

                matched, prev_run_time = should_trigger(cron_expr, now)
                if matched:
                    # We have a match! Let's call the CLI directly
                    backup_type = backup_type.lower()

                    # Normalize backup type
                    if backup_type in ["diff", "differential"]:
                        backup_type = "differential"
                    elif backup_type in ["inc", "incremental"]:
                        backup_type = "incremental"
                    else:
                        backup_type = "full"

                    # Properly merge configs the same way the CLI does
                    # First, copy the nested dictionaries
                    merged_aws = merge_dicts(global_config.get("aws"), config.get("aws"))
                    merged_encryption = merge_dicts(global_config.get("encryption"), config.get("encryption"))

                    # Determine if encryption and sync should be enabled from the merged configs
                    encrypt_enabled = merged_encryption.get("enabled", False)
                    sync_enabled = merged_aws.get("enabled", False)

                    logger.info(
                        f"MATCH FOUND for job '{job_name}' (config: {os.path.basename(config_path)}): "
                        f"Schedule '{cron_expr}', type: {backup_type}, encrypt: {encrypt_enabled}, sync: {sync_enabled}"
                    )

                    # Call cli.py's run_job function directly
                    try:
                        result = call_cli_run_job(
                            config_path,
                            backup_type,
                            encrypt=encrypt_enabled,
                            sync=sync_enabled
                        )

                        if result == "locked":
                            logger.info(f"Job '{job_name}' is already running or locked. Skipping.")
                            continue

                        triggered_jobs_count += 1
                        triggered_jobs_info.append({
                            "name": job_name,
                            "backup_type": backup_type,
                            "error": False if result else True
                        })

                    except Exception as e:
                        logger.error(f"Error running job '{job_name}': {e}", exc_info=True)
                        triggered_jobs_info.append({
                            "name": job_name,
                            "backup_type": backup_type,
                            "error": True
                        })

                    # Only trigger one schedule per job per check
                    break

        logger.info(f"Triggered {triggered_jobs_count} job(s) during this check.")
        logger.info("--- Scheduler Check Finished ---")
        update_status_file()

        # Send scheduler check event for mini-chart display
        send_scheduler_check(running_jobs=triggered_jobs_count)
    except Exception as e:
        logger.error(f"An unexpected error occurred in the main scheduler loop: {e}", exc_info=True)
    finally:
        trim_all_logs()

if __name__ == "__main__":
    main()
