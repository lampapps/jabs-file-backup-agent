# JABS Backup Agent

Standalone backup execution module for JABS (Just Another Backup System).

Runs independently on each backup host. It:
- Executes backups on cron schedules (full/incremental/differential/dry-run)
- Manages its own local SQLite metadata (`data/jabs_agent.sqlite`)
- Optionally encrypts (GPG) and syncs archives to S3
- Optionally reports events to the central [JABS Dashboard](../dashboard/README.md)
- Works fully offline if the dashboard is unavailable — reporting failures never block a backup

This directory is fully standalone: its own `.env`, `.gitignore`, `venv/`, `requirements.txt`, and config.

## Entry Points

| Script | Purpose |
|---|---|
| `scheduler.py` | Cron-style loop: reads `config/jobs/*.yaml`, checks each job's schedules against `croniter`, and calls `backup.py`'s job runner in-process for due jobs. This is what `jabs-agent.sh start` runs. |
| `backup.py` | Runs a single backup job. Used by the scheduler and can also be run manually. |

There is no `cli.py` or `run_agent.py` in this codebase — `backup.py` and `scheduler.py` are the real entry points (older docs referencing those names are stale).

## Setup

```bash
cd agents/backup_agent
./jabs-agent.sh setup    # creates venv, installs requirements.txt (boto3, croniter, etc.)
./jabs-agent.sh start    # runs scheduler.py
./jabs-agent.sh status
./jabs-agent.sh logs
./jabs-agent.sh stop
```

`jabs-agent.sh` is fully self-contained (no shared code with the dashboard launcher).

### Manual runs (after `./jabs-agent.sh setup`)

```bash
# Run the scheduler loop directly
venv/bin/python scheduler.py

# Run one backup job manually
venv/bin/python backup.py --job "Jim Home" --type full --encrypt --sync
# --type: full | incremental | differential | dryrun
```

## Directory Structure

```
backup_agent/
├── backup.py              # runs a single backup job (entry point)
├── scheduler.py           # cron-style loop, calls backup.py logic in-process
├── monitoring_client.py   # reports events to JABS Dashboard (send_event, send_backup_start/stage/complete)
├── settings.py            # BASE_DIR, ENV_PATH, CONFIG_DIR, DB_PATH, LOG_DIR, VERSION
├── logger.py              # setup_logger, trim_all_logs
├── jabs-agent.sh          # standalone setup/start/stop/status launcher
├── core/
│   ├── backup/            # full.py, incremental.py, diff.py, dryrun.py, common.py (locking, rotation)
│   ├── encrypt.py         # GPG encryption of tarballs
│   └── sync_s3.py         # AWS CLI based S3 sync
├── models/                # backup_jobs.py, backup_sets.py, backup_files.py, db_core_agent.py
├── scripts/
│   └── restore.py         # copied alongside archives for disaster recovery
├── config/
│   ├── global.yaml        # local settings (source/destination defaults, encryption, aws, dashboard URL)
│   └── jobs/*.yaml        # per-job config (source, destination, exclude, keep_sets, schedules)
├── data/
│   ├── jabs_agent.sqlite  # local backup metadata
│   └── logs/              # agent.log, scheduler.log, cli.log
└── locks/                 # per-job lock files (portalocker), incl. restore_status/
```

## Configuration

### `.env`
```
JABS_ENCRYPT_PASSPHRASE=...
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_PROFILE=...
JABS_DASHBOARD_URL=http://central-jabs-dashboard:5001   # optional, read by monitoring_client.py
ENV_MODE=production
```

### `config/global.yaml`
```yaml
destination: /mnt/backups
keep_sets: 5
max_tarball_size: 2048
use_common_exclude: true

aws:
  enabled: false
  region: us-east-1
  bucket: example-jabs
  storage_class: STANDARD

encryption:
  enabled: true
```

### `config/jobs/<job>.yaml`
```yaml
job_name: daily_backup
source: /home/user
destination: /mnt/backups     # optional, falls back to global
exclude:
  - "*.log"
  - ".cache/"
keep_sets: 5
schedules:
  - cron: "0 2 * * *"    # 2 AM daily
    type: full
    enabled: true
```

Config merge rule (see `backup.py`): job-level `aws`/`encryption` dicts merge over global; flat keys (e.g. `destination`, `keep_sets`) fall back to global only if missing from the job file.

## Reporting to the JABS Dashboard

If `JABS_DASHBOARD_URL` is set, the agent reports:
- **Scheduler heartbeat** — periodic "I'm alive" signal (no backup context)
- **Backup lifecycle events** — `send_backup_start`, `send_backup_stage`, `send_backup_complete` (see `monitoring_client.py` and `backup.py:create_event`)

The agent's hostname + IP must first be **registered on the dashboard** (Hosts page) or events are rejected with `403`. See [dashboard README](../dashboard/README.md#registering-a-host-agent).

Reporting is best-effort: if the dashboard is unreachable, the backup still runs and completes normally.

## Disaster Recovery

The agent works without the dashboard:
1. **Dashboard down?** Backups continue on schedule; reporting is skipped/retried, never blocking.
2. **Restore needed?** Each backup set includes a `restore.py` script and manifest alongside the archives — no dependency on this codebase or the dashboard.

## Storage Structure

```text
(destination_path/ | s3://<bucket>/)
└── (machine_name)/
    └── (job_name)/
        └── backup_set_YYYYMMDD_HHMMSS/
            ├── full_part_1_YYYYMMDD_HHMMSS.tar.gz[.gpg]
            ├── ... (other tarballs)
            ├── restore.py
            └── manifest_YYYYMMDD_HHMMSS.html
```

## Testing

```bash
cd /home/jim/jabs_dev
python3 -m unittest tests.test_agent_database -v
```

## Logging

- `data/logs/scheduler.log` — scheduler runs, trimmed to `MAX_LOG_LINES` each run
- `data/logs/cli.log` — individual backup job runs (`backup.py`)
- Log level follows `ENV_MODE` (`development` → DEBUG, else INFO)
