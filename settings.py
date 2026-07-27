"""Agent-specific settings and configuration constants."""

import os
import sys
from datetime import timedelta
import yaml
from dotenv import load_dotenv



VERSION = "0.10.0"

# Type of agent, reported to the dashboard so it can distinguish agent kinds
# (e.g. "File Backup", "Docker Backup", "Raspberry Pi Image") on the Hosts page.
AGENT_TYPE = "File Backup"

# --- Environment Configuration ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# Path to the .env file (shared at project root, or local if agent is standalone)
# First check for .env in agent directory, fall back to parent if agent is in a project
if os.path.exists(os.path.join(BASE_DIR, ".env")):
    ENV_PATH = os.path.join(BASE_DIR, ".env")
else:
    ENV_PATH = os.path.abspath(os.path.join(BASE_DIR, '..', '..', '.env'))

# Load environment variables
load_dotenv(ENV_PATH)

# Environment mode (development/production)
ENV_MODE = os.environ.get("ENV_MODE", "production")

# --- Application Configuration (Agent-specific) ---
LOCK_DIR = os.path.join(BASE_DIR, 'locks')
CLI_SCRIPT = os.path.join(BASE_DIR, 'backup.py')
PYTHON_EXECUTABLE = sys.executable or "python3"

# --- CONFIG Configuration ---
CONFIG_DIR = os.path.join(BASE_DIR, 'config')
JOBS_DIR = os.path.join(CONFIG_DIR, 'jobs')
GLOBAL_CONFIG_PATH = os.path.join(CONFIG_DIR, "global.yaml")

# --- Data Configuration (Agent uses local DB) ---
DATA_DIR = os.path.join(BASE_DIR, 'data')
DB_PATH = os.path.join(DATA_DIR, "jabs_agent.sqlite")

# --- Logging Configuration ---
LOG_DIR = os.path.join(DATA_DIR, 'logs')
MAX_LOG_LINES = 10000

RESTORE_SCRIPT_SRC = os.path.join(BASE_DIR, 'scripts/restore.py') # script that is copied to repositories with archives

#--- Scheduler Configuration ---
MAX_SCHEDULER_EVENTS = 300      # How many event bars show in the dashboard
SCHEDULE_TOLERANCE = timedelta(seconds=15)      # buffer for cron job execution
SCHEDULER_STATUS_FILE = os.path.join(LOG_DIR, "scheduler.status")

# --- SMTP Configuration ---
with open(GLOBAL_CONFIG_PATH, "r", encoding="utf-8") as f:
    GLOBAL_CONFIG = yaml.safe_load(f)

EMAIL_CONFIG = GLOBAL_CONFIG.get("email", {})
