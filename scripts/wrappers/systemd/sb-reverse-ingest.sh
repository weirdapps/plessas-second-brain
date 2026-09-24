#!/bin/bash
# Daily local-file reverse ingest — launchd surface for
# com.plessas.second-brain.reverse-ingest.
# Walks ~/Documents/{National,Personal}, dedups latest-version-per-
# logical-name, ingests via brain reverse-ingest. Sequenced after
# sb-curate-docs (05:07) so newly-curated files are filtered cleanly.

set -uo pipefail

# Env setup — sources $HOME files only; brings Vertex AI creds via .zprofile.
[ -f "$HOME/.zprofile" ] && source "$HOME/.zprofile" 2>/dev/null || true
eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null || true
export PATH="$HOME/.local/bin:$PATH"

PROJECT="$HOME/SourceCode/plessas-second-brain"
PYTHON="$HOME/.venvs/second-brain/bin/python"
# The data home as src/config.py resolves it: BRAIN_DATA_DIR, else the repo's.
DB="${BRAIN_DATA_DIR:-$PROJECT/data}/brain.db"
LOG_DIR="$HOME/.second-brain/logs"
LOG_FILE="$LOG_DIR/reverse-ingest.log"
SENTINEL="$HOME/.second-brain/needs_reauth"
mkdir -p "$LOG_DIR"

if [ -f "$SENTINEL" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] SKIP: needs_reauth sentinel present" >> "$LOG_FILE"
  exit 0
fi

if [ -f "$HOME/.second-brain/needs_gcloud_reauth" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] SKIP: needs_gcloud_reauth sentinel present" >> "$LOG_FILE"
  exit 0
fi

# Either project name will do; claude_extract reads VERTEX_SDK_PROJECT first.
if [ -z "${VERTEX_SDK_PROJECT:-}${ANTHROPIC_VERTEX_PROJECT_ID:-}" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] SKIP: no Vertex project (VERTEX_SDK_PROJECT or ANTHROPIC_VERTEX_PROJECT_ID)" >> "$LOG_FILE"
  exit 0
fi

echo "=== reverse-ingest started: $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"
cd "$PROJECT"  # without this, `python -m src.cli` fails under launchd (CWD=/)
"$PYTHON" -m src.cli reverse-ingest --workers "${REVERSE_INGEST_WORKERS:-4}" >> "$LOG_FILE" 2>&1
EXIT_CODE=$?
# Retry once on DB-lock contention (mirrors sb-daily-sync.sh). More likely now
# that the hourly sync also writes to brain.db each hour.
if [ "$EXIT_CODE" -ne 0 ] && tail -20 "$LOG_FILE" | grep -qi "database is locked"; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Retrying reverse-ingest after database-lock failure..." >> "$LOG_FILE"
  sleep 10
  "$PYTHON" -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()" "$DB" 2>/dev/null || true
  "$PYTHON" -m src.cli reverse-ingest --workers "${REVERSE_INGEST_WORKERS:-4}" >> "$LOG_FILE" 2>&1
  EXIT_CODE=$?
fi
echo "=== reverse-ingest finished (exit $EXIT_CODE): $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"
exit $EXIT_CODE
