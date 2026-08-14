#!/bin/bash
# Pull second-brain DB + embeddings from VPS → Mac (hourly)
#
# VPS is the primary producer (all sync jobs run there via systemd).
# Mac is the consumer (MCP server + ChatWatch read the local DB).
# This script bridges the gap: WAL checkpoint on VPS, then rsync delta.
#
# Deployed to ~/.local/bin/sb-db-pull.sh
# Triggered by LaunchAgent com.plessas.second-brain.db-pull (:45 past each hour)

set -uo pipefail

LOG_DIR="$HOME/.second-brain/logs"
LOG_FILE="$LOG_DIR/db-pull.log"
mkdir -p "$LOG_DIR"

VPS="vps"
LOCAL_DATA="$HOME/SourceCode/plessas-second-brain/data"
REMOTE_DATA="SourceCode/plessas-second-brain/data"
REMOTE_PYTHON="~/.venvs/second-brain/bin/python"
SSH_OPTS="-o ConnectTimeout=10 -o BatchMode=yes -o ServerAliveInterval=15"

log() { echo "[db-pull] $(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"; }

# --- Concurrency guard (PID-aware, same pattern as other sb-* wrappers) ---
LOCK_DIR="/tmp/sb-db-pull.lock"
if [ -d "$LOCK_DIR" ]; then
  stored_pid=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")
  if [ -n "$stored_pid" ] && kill -0 "$stored_pid" 2>/dev/null; then
    log "SKIP: another instance running (PID $stored_pid)"
    exit 0
  fi
  rm -rf "$LOCK_DIR"
fi
mkdir "$LOCK_DIR" && echo $$ > "$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT

# --- Skip if VPS is unreachable ---
if ! ssh $SSH_OPTS "$VPS" true 2>/dev/null; then
  log "SKIP: VPS unreachable"
  exit 0
fi

# --- Skip if a VPS sync job is actively writing (avoid mid-write copy) ---
ACTIVE=$(ssh $SSH_OPTS "$VPS" 'pgrep -f "[s]rc\.cli (sync|teams-sync|calendar-sync|load)" 2>/dev/null' || true)
if [ -n "$ACTIVE" ]; then
  log "DEFER: VPS sync job running (PIDs: $(echo $ACTIVE | tr '\n' ' ')), will retry next cycle"
  exit 0
fi

# --- WAL checkpoint on VPS (flush all pending writes to main DB file) ---
ssh $SSH_OPTS "$VPS" "$REMOTE_PYTHON -c \"
import sqlite3, os
db = os.path.expanduser('~/$REMOTE_DATA/brain.db')
c = sqlite3.connect(db)
rows = c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
c.close()
print(f'checkpoint: busy={rows[0]} log={rows[1]} checkpointed={rows[2]}')
\"" >> "$LOG_FILE" 2>&1
CP_RC=$?
if [ $CP_RC -ne 0 ]; then
  log "WARN: WAL checkpoint failed (rc=$CP_RC), proceeding with rsync anyway"
fi

# --- Rsync brain.db (delta transfer; default temp-file + rename = atomic) ---
RSYNC_OPTS="-az --timeout=180"

rsync $RSYNC_OPTS "$VPS:~/$REMOTE_DATA/brain.db" "$LOCAL_DATA/brain.db" 2>> "$LOG_FILE"
DB_RC=$?
if [ $DB_RC -eq 0 ]; then
  DB_SIZE=$(du -h "$LOCAL_DATA/brain.db" | cut -f1)
  log "brain.db synced OK ($DB_SIZE)"
else
  log "ERROR: brain.db sync FAILED (rc=$DB_RC)"
fi

# --- Rsync embeddings.npz ---
rsync $RSYNC_OPTS "$VPS:~/$REMOTE_DATA/embeddings.npz" "$LOCAL_DATA/embeddings.npz" 2>> "$LOG_FILE"
EMB_RC=$?
if [ $EMB_RC -eq 0 ]; then
  EMB_SIZE=$(du -h "$LOCAL_DATA/embeddings.npz" | cut -f1)
  log "embeddings.npz synced OK ($EMB_SIZE)"
else
  log "ERROR: embeddings.npz sync FAILED (rc=$EMB_RC)"
fi

# --- Sanity check: compare email counts VPS vs local ---
LOCAL_COUNT=$("$HOME/SourceCode/plessas-second-brain/.venv/bin/python3" -c "
import sqlite3
c = sqlite3.connect('$LOCAL_DATA/brain.db')
count = c.execute('SELECT COUNT(*) FROM emails').fetchone()[0]
max_date = c.execute('SELECT MAX(date_received) FROM emails WHERE message_id > 0').fetchone()[0]
c.close()
print(f'{count} emails, latest: {max_date}')
" 2>/dev/null || echo "?")
log "Local: $LOCAL_COUNT"

# --- Overall status ---
if [ $DB_RC -eq 0 ] && [ $EMB_RC -eq 0 ]; then
  log "DONE: all files synced"
else
  log "DONE: completed with errors (db=$DB_RC emb=$EMB_RC)"
fi

# Truncate log to last 1000 lines
tail -1000 "$LOG_FILE" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE" 2>/dev/null
