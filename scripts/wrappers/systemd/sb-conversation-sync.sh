#!/bin/bash
# Conversation sync: exports and extracts Claude Code conversations.
set -uo pipefail

REPO_DIR="$HOME/SourceCode/plessas-second-brain"
PYTHON="$HOME/.venvs/second-brain/bin/python"
LOG_DIR="$HOME/.second-brain/logs"
LOG_FILE="$LOG_DIR/conversation-sync.log"
mkdir -p "$LOG_DIR"

LOCK_DIR="/tmp/sb-conversation-sync.lock"
if [ -d "$LOCK_DIR" ]; then
  stored_pid=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")
  if [ -z "$stored_pid" ] || ! kill -0 "$stored_pid" 2>/dev/null; then
    rm -rf "$LOCK_DIR"
  else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] SKIP: already running (pid=$stored_pid)" >> "$LOG_FILE"
    exit 0
  fi
fi
mkdir -p "$LOCK_DIR" && echo $$ > "$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT INT TERM

[ -f "$HOME/.second-brain/needs_gcloud_reauth" ] && exit 0

echo "=== Conversation sync started: $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"

cd "$REPO_DIR" || exit 1
"$PYTHON" -m src.cli export-conversations --days 7 >> "$LOG_FILE" 2>&1
"$PYTHON" -m src.cli extract-conversations --workers 2 >> "$LOG_FILE" 2>&1

echo "$(date '+%Y-%m-%d %H:%M:%S') ok" >> "$LOG_FILE"
