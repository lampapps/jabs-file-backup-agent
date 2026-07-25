"""Backup dispatcher module for JABS."""

def run_backup(config, backup_type, **kwargs):
    """Dispatches backup jobs to the appropriate backup type handler."""
    event_id = kwargs.get("event_id")
    job_name = config.get("job_name", "unknown_job")
    encrypt_flag = kwargs.pop("encrypt", False)
    sync_flag = kwargs.pop("sync", False)

    try:
        # Pass backup_type and other needed parameters to the appropriate backup module
        if backup_type == "full":
            from .full import run_full_backup
            return run_full_backup(config, backup_type=backup_type, encrypt=encrypt_flag, sync=sync_flag, **kwargs)
        if backup_type in ("diff", "differential"):
            from .diff import run_diff_backup
            return run_diff_backup(config, backup_type=backup_type, encrypt=encrypt_flag, sync=sync_flag, **kwargs)
        if backup_type == "incremental":
            from .incremental import run_incremental_backup
            return run_incremental_backup(config, backup_type=backup_type, encrypt=encrypt_flag, sync=sync_flag, **kwargs)
        if backup_type in ("dry_run", "dryrun"):
            from .dryrun import run_dryrun_backup
            return run_dryrun_backup(config, backup_type=backup_type, encrypt=encrypt_flag, sync=sync_flag, **kwargs)
        raise ValueError(f"Unsupported backup type: {backup_type}")
    except Exception as e:
        # Error handling is done in the individual modules
        raise
