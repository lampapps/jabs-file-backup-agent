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
| --- | --- |
| `scheduler.py` | Cron-style loop: reads `config/jobs/*.yaml`, checks each job's schedules against `croniter`, and calls `backup.py`'s job runner in-process for due jobs. Invoked directly by the host's cron (see `./jabs-agent.sh help` for a ready-to-paste crontab line), not by `jabs-agent.sh` itself. |
| `backup.py` | Runs a single backup job. Used by the scheduler and can also be run manually. |

There is no `cli.py` or `run_agent.py` in this codebase — `backup.py` and `scheduler.py` are the real entry points (older docs referencing those names are stale).

## Setup

```bash
cd file_backup_agent
./jabs-agent.sh setup    # creates venv, installs requirements.txt (boto3, croniter, etc.)
./jabs-agent.sh status   # shows whether a scheduler.py run is currently in progress
./jabs-agent.sh logs     # follow data/logs/*.log
./jabs-agent.sh reset    # clear local database, logs, and locks
./jabs-agent.sh help     # host-specific copy-paste commands + CLI reference
```

`jabs-agent.sh` is fully self-contained (no shared code with the dashboard launcher). Unlike the dashboard, this agent has no long-running background server to `start`/`stop`/`restart` — `scheduler.py` is invoked directly by cron (see Manual runs below and `./jabs-agent.sh help` for a ready-to-paste crontab line).

### Manual runs (after `./jabs-agent.sh setup`)

```bash
# Run the scheduler loop directly
venv/bin/python scheduler.py

# Run one backup job manually
venv/bin/python backup.py --job "Jim Home" --type full --encrypt --sync
# --type: full | incremental | differential | dryrun
```

## Directory Structure

```text
file_backup_agent/
├── backup.py              # runs a single backup job (entry point)
├── scheduler.py           # cron-style loop, calls backup.py logic in-process
├── monitoring_client.py   # reports events to JABS Dashboard (send_event, send_backup_start/stage/complete, sync_job_backup_sets)
├── emailer.py             # immediate email notifications (error / backup_complete), independent of the dashboard's digest
├── uptime_kuma_client.py  # optional Uptime Kuma push-monitor heartbeat
├── settings.py            # BASE_DIR, ENV_PATH, CONFIG_DIR, DB_PATH, LOG_DIR, VERSION, AGENT_KEY
├── logger.py              # setup_logger, trim_all_logs
├── jabs-agent.sh          # standalone setup/status/logs/reset/help launcher
├── core/
│   ├── backup/            # full.py, incremental.py, diff.py, dryrun.py, common.py (locking, rotation)
│   ├── encrypt.py         # GPG encryption of tarballs
│   └── sync_s3.py         # AWS CLI based S3 sync
├── models/                # backup_jobs.py, backup_sets.py, backup_files.py, db_core_agent.py
├── scripts/
│   └── restore.py         # copied alongside archives for disaster recovery
├── config/
│   ├── global.yaml        # local settings (source/destination defaults, encryption, aws, email, dashboard URL)
│   └── jobs/*.yaml        # per-job config (source, destination, exclude, keep_sets, schedules)
├── data/
│   ├── jabs_agent.sqlite  # local backup metadata
│   └── logs/              # agent.log, scheduler.log, cli.log
└── locks/                 # per-job lock files (portalocker), incl. restore_status/
```

## Configuration

### `.env`

```text
JABS_ENCRYPT_PASSPHRASE=...
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_PROFILE=...
JABS_DASHBOARD_URL=http://central-jabs-dashboard:5001   # optional, read by monitoring_client.py
JABS_AGENT_KEY=...                                       # required to report to the dashboard (see Reporting section below)
UPTIME_KUMA_URL=...                                      # optional, read by uptime_kuma_client.py
JABS_SMTP_USERNAME=...                                   # optional, required if email.notify_on is enabled
JABS_SMTP_PASSWORD=...
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

email:
  smtp_server: smtp.example.com
  smtp_port: 587
  to_addrs:
    - admin@example.com
  use_tls: true
  notify_on:
    error:
      enabled: true
    backup_complete:
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
- **Set reconciliation** — `sync_job_backup_sets(job_name, active_backup_set_ids)`, called at the end of local rotation so the dashboard purges any backup_jobs rows for sets this agent has already rotated out locally

The agent must first be **registered on the dashboard** (Agents page) to obtain a unique `agent_key`, which is set as `JABS_AGENT_KEY` in `.env` and sent as the `X-API-Key` header on every request — not validated by hostname/IP. Requests with a missing/invalid/disabled key are rejected (`401`/`403`). See [dashboard README](../dashboard/README.md#registering-an-agent).

Reporting is best-effort: if the dashboard is unreachable, the backup still runs and completes normally.

## Email Notifications

Independent of the dashboard, the agent can send **immediate** email notifications for its own events — unlike the dashboard's digest (which batches activity from all agents on its own schedule), this fires right away for the agent's own backup runs. Controlled by `email.notify_on.<error|backup_complete>.enabled` in `config/global.yaml` (see example above); SMTP server/port/recipients also live under `email:`, with credentials read from `JABS_SMTP_USERNAME`/`JABS_SMTP_PASSWORD` in `.env`. Implemented in `emailer.py`, wired into `backup.py`'s success/error finalization paths. A send failure is logged and never fails the backup job.

## Uptime Kuma push monitor

Independent of the JABS dashboard, `scheduler.py` can send a push heartbeat
to a self-hosted [Uptime Kuma](https://github.com/louislam/uptime-kuma) push
monitor every time it runs a check — i.e. on the cron cadence that invokes
`scheduler.py` (typically every 15 minutes), **not** on individual backup
job schedules. A daily/weekly job schedule would ping far too infrequently
for Uptime Kuma to reliably detect "the scheduler stopped running" before a
backup is actually missed; pinging on every scheduler check instead lets you
set the push monitor's expected interval to match your cron cadence and get
alerted the moment the scheduler itself stops firing.

Create a Push monitor in Uptime Kuma, set its expected heartbeat interval to
match your cron schedule (e.g. every 15 minutes), then set `UPTIME_KUMA_URL`
in `.env` to its push URL. This is a single agent-wide setting (env var, not
YAML) — consistent with `JABS_DASHBOARD_URL`, since the push URL embeds a
secret token in its path the same way an API key would. Disabled by default
(empty `UPTIME_KUMA_URL`). Ping failures (monitor unreachable, bad URL) are
logged as warnings and never fail the scheduler run.

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

## Logging

- `data/logs/scheduler.log` — scheduler runs, trimmed to `MAX_LOG_LINES` each run
- `data/logs/cli.log` — individual backup job runs (`backup.py`)
- Log level follows `ENV_MODE` (`development` → DEBUG, else INFO)
