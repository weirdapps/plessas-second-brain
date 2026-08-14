#!/bin/bash
# Midday second-brain catchup — launchd surface for com.plessas.second-brain.noon-catchup.
#
# Lightweight counterpart to sb-daily-sync.sh: skips the slow AppleScript Mail.app
# export step and processes whatever the hourly outlook-cli sync has already staged.
# Closes the staleness window from 24h to ~6h between morning and noon runs.

set -uo pipefail

[ -f "$HOME/.zprofile" ] && source "$HOME/.zprofile" 2>/dev/null || true
eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null || true
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

REPO_DIR="$HOME/SourceCode/plessas-second-brain"
PYTHON="$HOME/.venvs/second-brain/bin/python"
LOG_DIR="$HOME/.second-brain/logs"
LOG_FILE="$LOG_DIR/noon-catchup.log"
SENTINEL="$HOME/.second-brain/needs_reauth"
mkdir -p "$LOG_DIR"

echo "=== Noon catchup started: $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"

if [ -f "$SENTINEL" ]; then
  echo "Skipping (needs_reauth sentinel present)" >> "$LOG_FILE"
  exit 0
fi

cd "$REPO_DIR"
"$PYTHON" -m src.cli sync --engine claude --workers 8 --skip-export >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

echo "=== Noon catchup finished (exit $EXIT_CODE): $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"
echo "" >> "$LOG_FILE"
exit $EXIT_CODE
