#!/bin/bash

#################################################
# JABS Agent Standalone Launcher
#
# This script handles setup, validation, and running
# of the JABS Agent (backup executor) with proper
# environment management.
#
# Usage:
#   jabs-agent.sh {setup|start|stop|restart|status|logs}
#   jabs-agent.sh help
#################################################

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../../.env"
if [[ -f "$ENV_FILE" ]]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

# Agent configuration
VENV_PATH="$SCRIPT_DIR/venv"
PYTHON_VENV="$VENV_PATH/bin/python"
RUN_SCRIPT="$SCRIPT_DIR/scheduler.py"
CLI_SCRIPT="$SCRIPT_DIR/backup.py"
PID_FILE="$SCRIPT_DIR/jabs_agent.pid"
LOG_FILE="$SCRIPT_DIR/data/logs/agent.log"

# Color output (ANSI-C quoting so these are raw escape bytes, not literal
# backslash text — needed since show_help's heredoc uses `cat`, not `echo -e`)
GREEN=$'\033[0;32m'
RED=$'\033[0;31m'
YELLOW=$'\033[1;33m'
BLUE=$'\033[0;34m'
CYAN=$'\033[0;36m'
BOLD=$'\033[1m'
DIM=$'\033[2m'
NC=$'\033[0m' # No Color

# Helper functions
print_status() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_header() {
    echo -e "${BLUE}[JABS Agent]${NC} $1"
}

print_section() {
    echo -e "${CYAN}[SECTION]${NC} $1"
}

print_success() {
    echo -e " ${GREEN}✓${NC} $1"
}

# Check Python version
check_python() {
    if command -v python3 &>/dev/null; then
        PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')
        PYTHON_OK=$(python3 -c 'import sys; print(sys.version_info >= (3,8))')
        if [ "$PYTHON_OK" = "True" ]; then
            print_success "Python 3.8+ found: $PYTHON_VERSION"
            return 0
        else
            print_error "Python version $PYTHON_VERSION found, but 3.8+ is required."
            return 1
        fi
    else
        print_error "Python3 not found."
        return 1
    fi
}

# Check venv module
check_venv_module() {
    if python3 -c "import venv" &>/dev/null; then
        print_success "python3 venv module is available."
        return 0
    else
        print_error "python3 venv module is missing."
        return 1
    fi
}

# Check pip
check_pip() {
    if python3 -m pip --version &>/dev/null; then
        print_success "pip found."
        return 0
    else
        print_error "pip not found."
        return 1
    fi
}

# Setup virtual environment
setup_virtual_env() {
    if [ -d "$VENV_PATH" ] && [ -f "$VENV_PATH/bin/python" ]; then
        print_success "Virtual environment already exists."
        return 0
    fi

    print_header "Setting up virtual environment..."
    if python3 -m venv "$VENV_PATH"; then
        print_success "Virtual environment created."
        return 0
    else
        print_error "Failed to create virtual environment."
        return 1
    fi
}

# Install requirements
install_requirements() {
    local req_file="$SCRIPT_DIR/requirements.txt"
    if [ ! -f "$req_file" ]; then
        print_error "requirements.txt not found at $req_file"
        return 1
    fi

    if "$PYTHON_VENV" -c "import boto3" &>/dev/null; then
        print_success "Requirements already installed."
        return 0
    fi

    print_header "Installing requirements..."
    if "$PYTHON_VENV" -m pip install --upgrade pip && "$PYTHON_VENV" -m pip install -r "$req_file"; then
        print_success "Requirements installed."
        return 0
    else
        print_error "Failed to install requirements."
        return 1
    fi
}

# Validate setup
validate_setup() {
    if [[ ! -f "$PYTHON_VENV" ]]; then
        print_error "Virtual environment not found at: $PYTHON_VENV"
        return 1
    fi
    if [[ ! -f "$RUN_SCRIPT" ]]; then
        print_error "Run script not found at: $RUN_SCRIPT"
        return 1
    fi
    if ! "$PYTHON_VENV" -c "import boto3" &>/dev/null; then
        print_error "Agent requirements not properly installed."
        return 1
    fi
    print_success "Setup validation complete."
    return 0
}

# Ensure log directory
ensure_log_dir() {
    if [[ ! -d "$(dirname "$LOG_FILE")" ]]; then
        mkdir -p "$(dirname "$LOG_FILE")"
    fi
}

# Ensure config/global.yaml and a starter config/jobs/job.yaml exist,
# copying from the tracked example/template files. Never overwrites
# existing files.
ensure_config_files() {
    local config_dir="$SCRIPT_DIR/config"
    local jobs_dir="$config_dir/jobs"

    if [[ ! -f "$config_dir/global.yaml" ]]; then
        if [[ -f "$config_dir/global-example.yaml" ]]; then
            cp "$config_dir/global-example.yaml" "$config_dir/global.yaml"
            print_success "Created config/global.yaml from global-example.yaml"
        else
            print_error "config/global-example.yaml not found; cannot create global.yaml"
            return 1
        fi
    else
        print_success "config/global.yaml already exists."
    fi

    if [[ ! -d "$jobs_dir" ]]; then
        mkdir -p "$jobs_dir"
        print_success "Created config/jobs/ directory."

        local job_template="$config_dir/templates/job.yaml"
        if [[ -f "$job_template" ]]; then
            cp "$job_template" "$jobs_dir/job.yaml"
            print_success "Created config/jobs/job.yaml from templates/job.yaml"
        else
            print_warning "config/templates/job.yaml not found; skipped seeding an example job."
        fi
    else
        print_success "config/jobs/ directory already exists."
    fi

    return 0
}

# Ensure .env exists, copying from the tracked .env.example if needed.
# Never overwrites an existing .env.
ensure_env_file() {
    local env_file="$SCRIPT_DIR/.env"
    local env_example="$SCRIPT_DIR/.env.example"

    if [[ -f "$env_file" ]]; then
        print_success ".env already exists."
        return 0
    fi

    if [[ ! -f "$env_example" ]]; then
        print_warning ".env.example not found; skipping .env creation. Create $env_file manually."
        return 0
    fi

    cp "$env_example" "$env_file"
    print_success "Created .env from .env.example — edit it before running the agent."
    return 0
}

# Check if running
is_running() {
    if [[ -f "$PID_FILE" ]]; then
        local pid=$(cat "$PID_FILE")
        if ps -p "$pid" > /dev/null 2>&1; then
            echo "$pid"
            return 0
        else
            rm -f "$PID_FILE"
            return 1
        fi
    fi
    return 1
}

# Status agent
status_agent() {
    if running_pid=$(is_running); then
        print_success "Agent is running (PID: $running_pid)"
        if [[ -f "$LOG_FILE" ]]; then
            echo ""
            echo "Recent logs:"
            tail -5 "$LOG_FILE"
        fi
    else
        print_warning "Agent is not running"
    fi
}

# Show logs
show_logs() {
    if [[ -f "$LOG_FILE" ]]; then
        print_status "Showing agent logs (Press Ctrl+C to exit):"
        tail -f "$LOG_FILE"
    else
        print_error "Log file not found: $LOG_FILE"
        return 1
    fi
}

# Setup agent
setup_agent() {
    print_section "JABS Agent Setup"

    if [[ ! -d "$SCRIPT_DIR" ]]; then
        print_error "Agent directory not found at: $SCRIPT_DIR"
        return 1
    fi

    check_python || return 1
    check_venv_module || return 1
    check_pip || return 1
    setup_virtual_env || return 1
    install_requirements || return 1

    ensure_log_dir
    ensure_config_files || return 1
    ensure_env_file

    # Initialize database
    print_status "Initializing agent database..."
    if "$PYTHON_VENV" -c "from models.db_core_agent import init_db; init_db()" &>/dev/null; then
        print_success "Agent database initialized."
    else
        print_error "Failed to initialize agent database."
        return 1
    fi

    if validate_setup; then
        print_success "Agent setup complete!"
        echo ""
        echo -e "${BOLD}Next steps:${NC}"
        echo -e "  1. Edit secrets/connection settings: ${CYAN}$SCRIPT_DIR/.env${NC}"
        echo -e "  2. Configure: ${CYAN}$SCRIPT_DIR/config/global.yaml${NC}"
        echo -e "  3. Create jobs: ${CYAN}$SCRIPT_DIR/config/jobs/*.yaml${NC}"
        echo -e "  4. Run scheduler: ${CYAN}python $SCRIPT_DIR/scheduler.py${NC} (or via CRON: @hourly)"
        echo -e "  5. Monitor logs: ${CYAN}$0 logs${NC}"
        return 0
    else
        print_error "Agent setup validation failed."
        return 1
    fi
}

# Reset app (clear database, logs, locks)
reset_app() {
    print_section "JABS Agent Reset"

    # Clear database
    print_status "Clearing database..."
    if [ -f "$SCRIPT_DIR/data/jabs_agent.sqlite" ]; then
        rm -f "$SCRIPT_DIR/data/jabs_agent.sqlite"
        print_success "Database cleared"
    else
        print_status "No database found (skipped)"
    fi

    # Clear logs
    print_status "Clearing logs..."
    if [ -d "$SCRIPT_DIR/data/logs" ]; then
        rm -f "$SCRIPT_DIR/data/logs"/*.log
        print_success "Logs cleared"
    else
        print_status "No logs directory found (skipped)"
    fi

    # Clear PID file
    print_status "Clearing lock files..."
    if [ -f "$PID_FILE" ]; then
        rm -f "$PID_FILE"
        print_success "Lock file cleared"
    else
        print_status "No lock file found (skipped)"
    fi

    # Summary
    echo ""
    print_success "Agent reset complete!"
    echo ""
    echo -e "${BOLD}Reset items:${NC}"
    echo -e "  ${GREEN}✓${NC} Database cleared"
    echo -e "  ${GREEN}✓${NC} Logs cleared"
    echo -e "  ${GREEN}✓${NC} Lock files cleared"
    echo ""
    echo -e "${BOLD}Preserved items:${NC}"
    echo -e "  ${GREEN}✓${NC} Configuration files"
    echo -e "  ${GREEN}✓${NC} Application code"
    echo -e "  ${GREEN}✓${NC} Virtual environment"
    echo ""
    echo -e "${BOLD}Next steps:${NC}"
    echo -e "  ${CYAN}$0 setup${NC}   - Re-initialize if needed"
    echo -e "  ${CYAN}$0 logs${NC}    - View scheduler logs"
    return 0
}

# Print ready-to-copy commands for running the scheduler and each configured
# backup job on this host, using this machine's actual paths (including the
# venv interpreter) so they can be pasted directly into a terminal.
print_copy_paste_commands() {
    echo -e "${BOLD}COPY/PASTE COMMANDS${NC} ${DIM}(this host)${NC}:"
    echo ""
    echo -e "  ${DIM}CLI syntax reference (backup.py):${NC}"
    echo -e "    ${CYAN}$PYTHON_VENV $CLI_SCRIPT --job JOB_NAME --type TYPE [--encrypt] [--sync]${NC}"
    echo ""
    echo -e "      ${YELLOW}--job${NC}      Job name (matches a file in config/jobs/, without .yaml)"
    echo -e "      ${YELLOW}--type${NC}     full | incremental | differential | dryrun"
    echo -e "      ${YELLOW}--encrypt${NC}  Encrypt the backup (optional)"
    echo -e "      ${YELLOW}--sync${NC}     Sync the backup to S3 after completion (optional)"
    echo ""
    echo -e "  ${DIM}Run scheduler manually:${NC}"
    echo -e "    ${CYAN}$PYTHON_VENV $RUN_SCRIPT${NC}"
    echo ""

    local jobs_dir="$SCRIPT_DIR/config/jobs"
    local found=false
    if [[ -d "$jobs_dir" ]]; then
        for job_file in "$jobs_dir"/*.yaml; do
            [[ -e "$job_file" ]] || continue
            found=true
            local job_name
            job_name="$(basename "$job_file" .yaml)"
            echo -e "  ${DIM}Run job '$job_name' (dry run example — swap --type/flags per the syntax above):${NC}"
            echo -e "    ${CYAN}$PYTHON_VENV $CLI_SCRIPT --job \"$job_name\" --type dryrun${NC}"
            echo ""
        done
    fi

    if ! $found; then
        echo -e "  ${YELLOW}(No job configs found in $jobs_dir yet — create one first, e.g. from config/templates/job.yaml)${NC}"
        echo ""
    fi
}


# Show help
show_help() {
    cat << EOF
${BOLD}JABS Agent Launcher${NC}

${BOLD}USAGE:${NC}
  $0 {setup|status|logs|reset}
  $0 help

${BOLD}COMMANDS:${NC}
  ${CYAN}setup${NC}   Setup agent environment
  ${CYAN}status${NC}  Show agent status (if running via CRON)
  ${CYAN}logs${NC}    Follow agent logs
  ${CYAN}reset${NC}   Reset app (clear database, logs, locks)
  ${CYAN}help${NC}    Show this help message

${BOLD}DIRECTORIES:${NC}
  Agent:       $SCRIPT_DIR
  Venv:        $VENV_PATH
  Config:      $SCRIPT_DIR/config
  Log file:    $LOG_FILE

${BOLD}SETUP:${NC}
  1. Run: ${CYAN}$0 setup${NC}
  2. Edit: $SCRIPT_DIR/.env
  3. Edit: $SCRIPT_DIR/config/global.yaml
  4. Create backup jobs in: $SCRIPT_DIR/config/jobs/
  5. Add CRON job: crontab -e
     ${DIM}0 * * * * $PYTHON_VENV $RUN_SCRIPT > /dev/null 2>&1${NC}

${BOLD}EXAMPLES:${NC}
  ${DIM}# Initial setup${NC}
  $0 setup

  ${DIM}# Check logs${NC}
  $0 logs

  ${DIM}# View status${NC}
  $0 status

  ${DIM}# Reset app state${NC}
  $0 reset

EOF
    print_copy_paste_commands
}

# Main function
main() {
    local command="${1:-help}"

    case "$command" in
        setup)
            setup_agent
            ;;
        status)
            status_agent
            ;;
        logs)
            show_logs
            ;;
        reset)
            reset_app
            ;;
        help|--help|-h)
            show_help
            ;;
        *)
            print_error "Unknown command: $command"
            show_help
            exit 1
            ;;
    esac
}

# Run main with all arguments
main "$@"
