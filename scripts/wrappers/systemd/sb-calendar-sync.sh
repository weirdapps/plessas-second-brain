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

# A skip on a sentinel exits 69 (EX_UNAVAILABLE), as sb-outlook-sync.sh does: exit 0
# recorded success and pinged hc-success@ for runs that did nothing, and on 2026-10-20
# the calendar was one of ten jobs that read green through a ten-hour auth outage.
# Every stage of calendar-sync starts with outlook-cli, so a dead Outlook session
# leaves nothing to run.
if [ -f "$SENTINEL" ]; then
  echo "Skipping (needs_reauth sentinel present)" >> "$LOG_FILE"
  exit 69
fi

# A dead gcloud credential stops only the body extraction. The listing, the times,
# attendees and cancellations come from Outlook, and the briefs and meeting prep read
# them, so they still go in; --skip-extraction leaves each body worth reading 'pending'
# for the next run with a credential. The run stays red, because that part is owed.
EXTRACT_FLAG=""
if [ -f "$GCLOUD_SENTINEL" ]; then
  echo "Extraction skipped (needs_gcloud_reauth sentinel present); listing only" >> "$LOG_FILE"
  EXTRACT_FLAG="--skip-extraction"
fi

cd "$REPO_DIR"
# shellcheck disable=SC2086  # empty, or the one flag
"$PYTHON" -m src.cli calendar-sync $EXTRACT_FLAG >> "$LOG_FILE" 2>&1
EXIT_CODE=$?
if [ "$EXIT_CODE" -eq 0 ] && [ -n "$EXTRACT_FLAG" ]; then
  EXIT_CODE=69
fi

if [ $EXIT_CODE -eq 0 ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') — ok" >> "$LOG_FILE"
else
  echo "$(date '+%Y-%m-%d %H:%M:%S') — FAILED (exit $EXIT_CODE)" >> "$LOG_FILE"
fi

# Pass the command's status through. Without this the script ended on the echo
# above and exited 0 whatever calendar-sync did, so systemd recorded success.
exit "$EXIT_CODE"
