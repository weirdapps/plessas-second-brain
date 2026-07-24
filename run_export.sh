#!/bin/bash
# Overnight unattended export — auto-resumes on any stop
#
# Usage: nohup ./run_export.sh > data/export.log 2>&1 &
#
# Safety measures:
# - Auto-resumes if export stops (memory, error, timeout)
# - Restarts Mail.app if memory gets critical (>6GB)
# - Logs everything to data/export.log
# - Stops only when all emails are exported
# - Won't hang — each run has its own timeout guard

set -uo pipefail

REPO_DIR="$HOME/SourceCode/second-brain"
cd "$REPO_DIR"

# Activate virtualenv
source .venv/bin/activate

LOG_PREFIX() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')]"
}

restart_mail() {
    echo "$(LOG_PREFIX) Restarting Mail.app to free memory..."
    osascript -e 'tell application "Mail" to quit' 2>/dev/null || true
    sleep 10
    # Wait for Mail to fully quit
    for i in {1..30}; do
        if ! pgrep -x Mail > /dev/null 2>&1; then
            break
        fi
        sleep 2
    done
    echo "$(LOG_PREFIX) Launching Mail.app..."
    open -a Mail
    # Wait for Mail to start and connect to Exchange
    sleep 30
    echo "$(LOG_PREFIX) Mail.app restarted"
}

check_and_restart_mail_if_needed() {
    local rss_kb
    rss_kb=$(ps -o rss= -p "$(pgrep -x Mail 2>/dev/null)" 2>/dev/null | tr -d ' ' || echo "0")
    local rss_gb
    rss_gb=$(echo "scale=1; ${rss_kb:-0} / 1048576" | bc 2>/dev/null || echo "0")

    if (( $(echo "$rss_gb > 6.0" | bc -l 2>/dev/null || echo 0) )); then
        echo "$(LOG_PREFIX) Mail.app at ${rss_gb}GB — restarting preemptively"
        restart_mail
    fi
}

# Ensure Mail.app is running
if ! pgrep -x Mail > /dev/null 2>&1; then
    echo "$(LOG_PREFIX) Mail.app not running — launching..."
    open -a Mail
    sleep 30
fi

echo "$(LOG_PREFIX) === OVERNIGHT EXPORT STARTED ==="
echo "$(LOG_PREFIX) PID: $$"
echo "$(LOG_PREFIX) Log: $REPO_DIR/data/export.log"

MAX_RUNS=200  # Safety: max 200 restart cycles (way more than needed)
PAUSE_BETWEEN_RUNS=120  # 2 minutes between runs to let Mail.app settle

for run in $(seq 1 $MAX_RUNS); do
    echo ""
    echo "$(LOG_PREFIX) === Run $run ==="

    # Check if already complete
    STATE_FILE="$REPO_DIR/data/state/export_state.json"
    if [ -f "$STATE_FILE" ]; then
        EXPORTED=$(python3 -c "import json; print(json.load(open('$STATE_FILE'))['total_exported'])" 2>/dev/null || echo 0)
        echo "$(LOG_PREFIX) Progress: $EXPORTED emails exported so far"

        if [ "$EXPORTED" -ge 41000 ]; then
            echo "$(LOG_PREFIX) Export looks complete ($EXPORTED emails). Verifying..."
            # Do one more run to confirm
            python -m src.cli export 2>&1 || true
            echo "$(LOG_PREFIX) === EXPORT COMPLETE ==="
            break
        fi
    fi

    # Preemptive Mail.app restart every 5000 emails
    if [ -f "$STATE_FILE" ]; then
        if (( EXPORTED % 5000 < 100 && EXPORTED > 0 )); then
            check_and_restart_mail_if_needed
        fi
    fi

    # Run export (no limit = continue from where we left off)
    # Use a background process with wait + kill for timeout (macOS has no `timeout` command)
    echo "$(LOG_PREFIX) Starting export..."
    PYTHONUNBUFFERED=1 python -m src.cli export 2>&1 &
    EXPORT_PID=$!

    # Wait up to 2 hours (7200s), check every 30s
    ELAPSED=0
    while kill -0 $EXPORT_PID 2>/dev/null; do
        sleep 30
        ELAPSED=$((ELAPSED + 30))
        if [ $ELAPSED -ge 7200 ]; then
            echo "$(LOG_PREFIX) Export timed out after 2 hours — killing"
            kill $EXPORT_PID 2>/dev/null || true
            sleep 5
            kill -9 $EXPORT_PID 2>/dev/null || true
            break
        fi
    done
    wait $EXPORT_PID 2>/dev/null || true

    # Clean up lock file in case it wasn't released
    rm -f "$REPO_DIR/data/state/export.lock"

    # Check progress
    if [ -f "$STATE_FILE" ]; then
        NEW_EXPORTED=$(python3 -c "import json; print(json.load(open('$STATE_FILE'))['total_exported'])" 2>/dev/null || echo 0)
        DELTA=$((NEW_EXPORTED - ${EXPORTED:-0}))
        echo "$(LOG_PREFIX) Run $run exported $DELTA emails (total: $NEW_EXPORTED)"

        if [ "$DELTA" -eq 0 ]; then
            echo "$(LOG_PREFIX) No progress — restarting Mail.app before retry"
            restart_mail
        fi
    fi

    # Pause between runs
    echo "$(LOG_PREFIX) Pausing ${PAUSE_BETWEEN_RUNS}s before next run..."
    sleep $PAUSE_BETWEEN_RUNS
done

echo ""
echo "$(LOG_PREFIX) === OVERNIGHT EXPORT FINISHED ==="
if [ -f "$STATE_FILE" ]; then
    FINAL=$(python3 -c "import json; print(json.load(open('$STATE_FILE'))['total_exported'])" 2>/dev/null || echo "?")
    echo "$(LOG_PREFIX) Total emails exported: $FINAL"
fi
