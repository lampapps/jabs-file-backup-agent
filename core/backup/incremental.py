"""Incremental backup logic for JABS."""

from models.backup_files import get_files_for_backup_set
from .common import run_partial_backup

def run_incremental_backup(config, backup_type="incremental", encrypt=False, sync=False, event_id=None, server_set_id=None, job_config_path=None, global_config=None):
    """
    Run an incremental backup which compares against all files in the backup set.
    Incremental backups only contain files that have changed since the last backup (full or incremental).
    """
    # Source getter function for incremental backup
    def incremental_source_getter(backup_set_id):
        return get_files_for_backup_set(backup_set_id)

    # Use the shared partial backup logic with incremental-specific parameters
    return run_partial_backup(
        config=config,
        backup_type=backup_type,
        source_getter_fn=incremental_source_getter,
        encrypt=encrypt,
        sync=sync,
        event_id=event_id,
        server_set_id=server_set_id,
        job_config_path=job_config_path,
        global_config=global_config
    )
