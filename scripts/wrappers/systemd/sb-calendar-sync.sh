#!/bin/bash
# Daily calendar sync — launchd surface for com.plessas.second-brain.calendar-sync.

set -uo pipefail

[ -f "$HOME/.zprofile" ] && source "$HOME/.zprofile" 2>/dev/null || true
eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null || true
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

REPO_DIR="$HOME/SourceCode/plessas-second-brain"
PYTHON="$HOME/.venvs/second-brain/bin/python"
LOG_DIR="$HOME/.second-brain/logs"
LOG_FILE="$LOG_DIR/calendar-sync.log"
SENTINEL="$HOME/.second-brain/needs_reauth"
GCLOUD_SENTINEL="$HOME/.second-brain/needs_gcloud_reauth"
mkdir -p "$LOG_DIR"

echo "=== Calendar sync started: $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"

if [ -f "$SENTINEL" ]; then
  echo "Skipping (needs_reauth sentinel present)" >> "$LOG_FILE"
  exit 0
fi

if [ -f "$GCLOUD_SENTINEL" ]; then
  echo "Skipping (needs_gcloud_reauth sentinel present)" >> "$LOG_FILE"
  exit 0
fi

cd "$REPO_DIR"
"$PYTHON" -m src.cli calendar-sync >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') — ok" >> "$LOG_FILE"
else
  echo "$(date '+%Y-%m-%d %H:%M:%S') — FAILED (exit $EXIT_CODE)" >> "$LOG_FILE"
fi
