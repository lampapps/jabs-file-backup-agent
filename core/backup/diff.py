"""Differential backup logic for JABS."""

from models.backup_files import get_files_for_last_full_backup
from .common import run_partial_backup

def run_diff_backup(config, backup_type="differential", encrypt=False, sync=False, event_id=None, server_set_id=None, job_config_path=None, global_config=None):
    """
    Run a differential backup which compares only against the last full backup.
    Differential backups contain all files that have changed since the last full backup.
    """
    # Source getter function for differential backup
    def differential_source_getter(job_name):
        return get_files_for_last_full_backup(job_name)

    # Use the shared partial backup logic with differential-specific parameters
    return run_partial_backup(
        config=config,
        backup_type=backup_type,
        source_getter_fn=differential_source_getter,
        encrypt=encrypt,
        sync=sync,
        event_id=event_id,
        server_set_id=server_set_id,
        job_config_path=job_config_path,
        global_config=global_config
    )