#!/bin/bash
# News-reader ingest — stages news syntheses + relevant articles into
# data/staging, where the hourly sb-outlook-sync drain (src.cli sync
# --skip-export) extracts and loads them like any other source.
#
# Staging only: this script never extracts or loads, so it stays fast and
# cannot collide with a running sync.

set -uo pipefail

[ -f "$HOME/.zprofile" ] && source "$HOME/.zprofile" 2>/dev/null || true

PROJECT="$HOME/SourceCode/plessas-second-brain"
PYTHON="$HOME/.venvs/second-brain/bin/python"
NEWS_DB="$HOME/SourceCode/news/data/news.db"
LOG_DIR="$HOME/.second-brain/logs"
LOG="$LOG_DIR/news-sync.log"

mkdir -p "$LOG_DIR"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

echo "=== news-sync started: $(ts) ===" >> "$LOG"

# Nothing to do if the upstream news DB is not there — exit clean rather than
# failing the unit (the health check reports the source side separately).
if [ ! -f "$NEWS_DB" ]; then
  echo "$(ts) — news db missing at $NEWS_DB; skipping" >> "$LOG"
  echo "" >> "$LOG"
  exit 0
fi

cd "$PROJECT"
"$PYTHON" -m src.cli news-sync >> "$LOG" 2>&1
EXIT_CODE=$?

echo "=== news-sync finished (exit $EXIT_CODE): $(ts) ===" >> "$LOG"
echo "" >> "$LOG"
exit $EXIT_CODE
