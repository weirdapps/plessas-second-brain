#!/bin/bash
# Midday second-brain catchup — launchd surface for com.plessas.second-brain.noon-catchup.
#
# The same sync as sb-daily-sync.sh, at midday: it extracts and loads whatever the
# hourly outlook-cli sync has staged. (--skip-export is kept for the schedules and
# ignored: sync stages no mail itself.)
# Closes the staleness window from 24h to ~6h between morning and noon runs.

set -uo pipefail

[ -f "$HOME/.zprofile" ] && source "$HOME/.zprofile" 2>/dev/null || true
eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null || true
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

REPO_DIR="$HOME/SourceCode/plessas-second-brain"
PYTHON="$HOME/.venvs/second-brain/bin/python"
LOG_DIR="$HOME/.second-brain/logs"
LOG_FILE="$LOG_DIR/noon-catchup.log"
mkdir -p "$LOG_DIR"

echo "=== Noon catchup started: $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"

# No needs_reauth gate here on purpose. sync never calls outlook-cli: it extracts
# and loads what the hourly export has already staged, so a dead Outlook session
# cannot affect it. The check used to be here, copied from the mail wrappers,
# and it skipped the catch-up, reporting success, five times from 2026-08-02 to
# 09-11 while the staged mail waited. The real dependency is guarded immediately
# below.

# Skip if gcloud ADC expired: extraction calls Vertex, as in sb-daily-sync.sh.
if [ -f "$HOME/.second-brain/needs_gcloud_reauth" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] SKIP: needs_gcloud_reauth sentinel present" >> "$LOG_FILE"
  exit 0
fi

# One sync at a time: sync takes a lock, and --lock-wait is how long to wait for
# the hourly load when it is still running. Then it skips with exit 0, because
# the sync holding the lock drains the same staged mail. 600 s fits the unit's
# TimeoutStartSec=1800: this job has taken at most 13 minutes, and the wait comes
# out of the extraction slice, which is capped by what is left of the unit.
cd "$REPO_DIR"
"$PYTHON" -m src.cli sync --engine claude --workers 8 --skip-export --lock-wait 600 >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

echo "=== Noon catchup finished (exit $EXIT_CODE): $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"
echo "" >> "$LOG_FILE"
exit $EXIT_CODE
