#!/bin/bash
# News-reader ingest — stages news syntheses + relevant articles into
# data/staging, then loads them (src.cli sync --skip-export) like any other source.
#
# It used to stage only and leave the load to the hourly sb-outlook-sync, which
# loads after its mail exports and exits before them on a dead Outlook session.
# So News waited on Outlook auth, and even on a good night the batch staged at
# 22:51 waited for the 01:10 run (76 items were waiting at the 2026-10-20 audit).
# Extracting news needs no model and loading needs no Outlook, so the batch is
# loaded here, through sync's own lock: --lock-wait waits for a load already
# running, which may have started before this batch was staged, then skips with 0.

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

# The load. sync extracts the staged mail with the model before it loads anything,
# so with the gcloud credential dead it would spend its extraction slice on calls
# that cannot succeed: the batch waits for the first load after auth-watch clears
# the sentinel, and the unit ends 69 (EX_UNAVAILABLE) for the load it owes. 600 s of
# lock wait fits TimeoutStartSec=1800 beside sync's 300 s extraction slice and the
# stages after it, as in sb-noon-catchup.sh.
if [ "$EXIT_CODE" -eq 0 ]; then
  if [ -f "$HOME/.second-brain/needs_gcloud_reauth" ]; then
    echo "$(ts) load skipped (needs_gcloud_reauth sentinel present); staged batch waits" >> "$LOG"
    EXIT_CODE=69
  else
    echo "$(ts) start load (sync --skip-export)" >> "$LOG"
    "$PYTHON" -m src.cli sync --engine claude --workers 8 --skip-export --lock-wait 600 >> "$LOG" 2>&1
    EXIT_CODE=$?
    echo "$(ts) load done (rc=$EXIT_CODE)" >> "$LOG"
  fi
fi

echo "=== news-sync finished (exit $EXIT_CODE): $(ts) ===" >> "$LOG"
echo "" >> "$LOG"
exit $EXIT_CODE
