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
# Bounded wait, not a single shot. On 2026-08-29 the machine DarkWoke at
# 13:52:18 and this job fired at 13:52:19, one second later, into a radio that
# had not associated: it logged "SKIP: VPS unreachable" while the 12:45 and
# 14:45 runs both succeeded. The gate polls for up to 180s and returns
# immediately when the network is already up, so a healthy run is unchanged.
WAIT_GATE="$HOME/.local/bin/wait-for-vps.sh"
if [ ! -x "$WAIT_GATE" ]; then
  # Not merged into the test below. A missing script makes bash exit 127, `!`
  # inverts that to success, and the job takes the SKIP branch and exits 0
  # forever: a dead pull that reports fine, with the "No such file" swallowed by
  # the redirect. Absent tooling is a fault, not a quiet afternoon.
  log "ERROR: readiness gate missing or not executable at $WAIT_GATE"
  exit 1
fi
# Errors to the log, not to /dev/null: the gate deliberately preserves the real
# diagnostic ("No route to host", "Permission denied") instead of a generic
# timeout, and discarding it here throws away the only thing that distinguishes
# a sleeping radio from an expired key.
if ! "$WAIT_GATE" "$VPS" 2>> "$LOG_FILE"; then
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

# Snapshot on the VPS FIRST, then copy the snapshot. rsync'ing brain.db directly
# copied a live WAL-mode database that the VPS kept writing during the ~60s
# transfer, so the local file was a mix of pages from different points in time.
# It was silently corrupt for a long time: `PRAGMA integrity_check` on the Mac
# copy reported freelist and out-of-order-rowid damage while the VPS original
# returned `ok`, and this log said "synced OK" 386 times without one warning.
# The DEFER guard above and the WAL checkpoint below both narrow the window;
# neither closes it, because nothing stops a write starting mid-rsync.
#
# `.backup` takes SQLite's own consistent snapshot and retries around writers,
# which is exactly what a plain file copy cannot do. It lives outside the repo
# data dir and PERSISTS between runs on purpose: a stable page layout is what
# lets rsync keep sending deltas instead of 3 GB every hour.
REMOTE_SNAP="\$HOME/.second-brain/brain.snapshot.db"
ssh $SSH_OPTS "$VPS" "mkdir -p \$HOME/.second-brain && sqlite3 \$HOME/$REMOTE_DATA/brain.db \".backup '$REMOTE_SNAP'\"" 2>> "$LOG_FILE"
SNAP_RC=$?
if [ $SNAP_RC -ne 0 ]; then
  log "ERROR: VPS snapshot FAILED (rc=$SNAP_RC), NOT copying a live database; keeping the existing local file"
  DB_RC=$SNAP_RC
else
  # Drop the local -wal/-shm BEFORE the rename lands. They belong to the file we
  # are about to replace, and SQLite will happily replay a leftover WAL onto the
  # new one: on 2026-08-29 a 4.5 MB WAL from a local `src.cli embed` survived the
  # rsync, the integrity check's own read-write open replayed it over freshly
  # copied pages, and quick_check then reported the damage the check had just
  # caused (`sender_signature_index` rowids out of order) while both the VPS
  # original and its snapshot verified `ok`. That is the whole failure: the
  # source was never the problem, the stale sidecar was. Dropping them costs
  # nothing because the local brain.db is a REPLICA that the next line replaces
  # wholesale, so any frames in the LOCAL wal describe a file that is about to
  # cease to exist. (The VPS checkpoint above is unrelated: it protects the
  # snapshot being copied and says nothing about what is safe to delete here.)
  # The one case where this is not free is an rsync that then fails, leaving the
  # previous replica short whatever an interrupted local checkpoint had not yet
  # folded in. That is acceptable: the next run re-copies the file entire.
  rm -f "$LOCAL_DATA/brain.db-wal" "$LOCAL_DATA/brain.db-shm"
  rsync $RSYNC_OPTS "$VPS:~/.second-brain/brain.snapshot.db" "$LOCAL_DATA/brain.db" 2>> "$LOG_FILE"
  DB_RC=$?
  # rsync renames its temp file into place, so a reader that had the old inode
  # open can recreate a -wal against the NEW file between the two lines above.
  rm -f "$LOCAL_DATA/brain.db-wal" "$LOCAL_DATA/brain.db-shm"
  if [ $DB_RC -eq 0 ]; then
    DB_SIZE=$(du -h "$LOCAL_DATA/brain.db" | cut -f1)
    log "brain.db synced OK ($DB_SIZE)"
  else
    log "ERROR: brain.db sync FAILED (rc=$DB_RC)"
  fi
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

# --- Integrity check: does the copy actually open and read cleanly? ---
# `SELECT COUNT(*) FROM emails` used to be the only check here, and it is the
# reason the corruption went unseen for 386 consecutive runs: it touched pages
# that happened to be intact and cheerfully returned 74537. A row count is not
# an integrity check. quick_check walks the b-trees and is the cheap form of
# integrity_check (it skips the slow freelist and index-content passes).
#
# Plain open: NOT `-readonly`, and NOT a `file:...?mode=ro` URI. Both of those
# return "unable to open database file (14)" on a perfectly healthy file, for two
# unrelated reasons:
#   * the URI form, because macOS ships /usr/bin/sqlite3 without SQLITE_USE_URI;
#   * `-readonly`, because brain.db is in WAL mode and the rsync above copies the
#     database file alone, no `-shm`. A read-only open may not create the `-shm`
#     it needs, so it cannot open a freshly copied WAL database at all.
# `-readonly` shipped on 2026-08-25 and failed all 21 runs it ever made: exactly
# the always-fails bug the URI note warns about, reached by a second road. Guard
# against the third by asserting on a healthy file, not by reasoning about flags.
# A read-write open recreates the `-shm`, and is how the MCP server opens this
# file anyway, so the check now reads the copy the way its only consumer does.
INTEGRITY=$(sqlite3 "$LOCAL_DATA/brain.db" 'PRAGMA quick_check;' 2>&1 | head -3 | tr '\n' ' ')
if [ "${INTEGRITY% }" != "ok" ]; then
  log "ERROR: local brain.db FAILED integrity check: ${INTEGRITY}"
  INTEGRITY_RC=1
else
  INTEGRITY_RC=0
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
# Exit non-zero on a bad copy. This job could not fail before: every path
# logged and returned 0, so launchd, the health report and the log all agreed
# everything was fine while the database was unreadable.
if [ $DB_RC -eq 0 ] && [ $EMB_RC -eq 0 ] && [ $INTEGRITY_RC -eq 0 ]; then
  log "DONE: all files synced"
  # When the replica was last known good. health_check.py ages every source
  # through this, because a copy cannot know anything that happened after it was
  # taken and measuring against wall clock instead made the overnight pull gap
  # (22:45 -> 07:45) look like a stale mail source every single night.
  #
  # It has to be its own file. brain.db's mtime is not the pull time: any reader
  # that opens the replica read-write bumps it, and the health check itself does
  # precisely that, so the mtime read 4h newer than the pull that produced it.
  # And it is written ONLY here, on the all-clear path, so that a failing pull
  # freezes it and the ages it feeds grow rather than quietly staying flat.
  date -u '+%Y-%m-%dT%H:%M:%S+00:00' > "$HOME/.second-brain/db-pull.stamp"
else
  log "DONE: completed with errors (db=$DB_RC emb=$EMB_RC integrity=$INTEGRITY_RC)"
  tail -1000 "$LOG_FILE" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE" 2>/dev/null
  exit 1
fi

# Truncate log to last 1000 lines
tail -1000 "$LOG_FILE" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE" 2>/dev/null
