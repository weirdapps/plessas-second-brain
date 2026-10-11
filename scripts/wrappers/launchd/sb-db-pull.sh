#!/bin/bash
# Pull second-brain DB + embeddings from VPS → Mac (hourly)
#
# VPS is the primary producer (all sync jobs run there via systemd).
# Mac is the consumer (MCP server + ChatWatch read the local DB).
# This script bridges the gap: WAL checkpoint on VPS, then rsync delta.
# Each file is pulled under a new name, checked, and only then renamed over the
# replica, so a bad copy never replaces a good one.
#
# Deployed to ~/.local/bin/sb-db-pull.sh
# Triggered by LaunchAgent com.plessas.second-brain.db-pull (:45 past each hour)
#
# The SB_PULL_* variables exist so a test can stand in for the producer, the
# transport and the readiness wait (tests/test_db_pull_swap.py). A scheduled run
# sets none of them.

set -uo pipefail

LOG_DIR="$HOME/.second-brain/logs"
LOG_FILE="$LOG_DIR/db-pull.log"
mkdir -p "$LOG_DIR"

VPS="${SB_PULL_VPS:-vps}"
LOCAL_DATA="$HOME/SourceCode/plessas-second-brain/data"
REMOTE_DATA="SourceCode/plessas-second-brain/data"
# rsync will not create a two-level destination, and `data/` is gitignored, so a
# fresh clone does not have it: the Pro rebuilt on 2026-09-05 had the plist, both
# scripts and a reachable VPS, and the pull would still have failed on every run
# with "No such file or directory". Same omission the offsite block below already
# records, one directory earlier in the same script.
mkdir -p "$LOCAL_DATA"
REMOTE_PYTHON="~/.venvs/second-brain/bin/python"
SSH_OPTS="-o ConnectTimeout=10 -o BatchMode=yes -o ServerAliveInterval=15"

log() { echo "[db-pull] $(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"; }

# --- Concurrency guard (PID-aware, same pattern as other sb-* wrappers) ---
LOCK_DIR="${SB_PULL_LOCK:-/tmp/sb-db-pull.lock}"
if [ -d "$LOCK_DIR" ]; then
  stored_pid=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")
  if [ -n "$stored_pid" ] && kill -0 "$stored_pid" 2>/dev/null; then
    log "SKIP: another instance running (PID $stored_pid)"
    exit 0
  fi
  rm -rf "$LOCK_DIR"
fi
mkdir "$LOCK_DIR" && echo $$ > "$LOCK_DIR/pid"
# Where each file lands before it is checked. Whatever a run leaves under these
# names was never checked, so it goes when the run ends, however it ends.
NEW_DB="$LOCAL_DATA/brain.db.new"
NEW_EMB="$LOCAL_DATA/embeddings.npz.new"
trap 'rm -rf "$LOCK_DIR"; rm -f "$NEW_DB" "$NEW_DB-wal" "$NEW_DB-shm" "$NEW_EMB"' EXIT

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
# a sleeping radio from an expired key. The budget and interval are the gate's
# own defaults.
if ! "$WAIT_GATE" "$VPS" "${SB_PULL_WAIT_BUDGET:-180}" "${SB_PULL_WAIT_INTERVAL:-10}" 2>> "$LOG_FILE"; then
  log "SKIP: VPS unreachable"
  exit 0
fi

# --- Skip if a VPS sync job is actively writing (avoid mid-write copy) ---
# The conversation sync's export and extraction write brain.db too. They were
# missing here, and the 08:45 conversation sync starts in the same minute as the
# pull.
ACTIVE=$(ssh $SSH_OPTS "$VPS" 'pgrep -f "[s]rc\.cli (sync|teams-sync|calendar-sync|whatsapp-sync|load|export-conversations|extract-conversations)" 2>/dev/null' || true)
if [ -n "$ACTIVE" ]; then
  log "DEFER: VPS sync job running (PIDs: $(echo $ACTIVE | tr '\n' ' ')), will retry next cycle"
  exit 0
fi

# Two spellings of one path, and they MUST stay in step. SNAP_NAME is the bare
# filename; REMOTE_SNAP is it with an escaped $HOME, because that string is
# expanded by the REMOTE shell inside the ssh below, not by this one. Making the
# snapshot per-host without also changing the rsync source left this script
# writing brain.snapshot.<host>.db and then pulling the stale shared
# brain.snapshot.db, so every pull "succeeded" against a file nothing updated any
# more. Derive both from one variable so they cannot diverge again.
SNAP_NAME="brain.snapshot.$(hostname -s).db"
REMOTE_SNAP="\$HOME/.second-brain/$SNAP_NAME"

# --- WAL checkpoint on VPS (flush all pending writes to main DB file) ---
# The same call reports what the checkpoint left behind, as "source: <mtime ns>
# <bytes> <-wal bytes> <bytes of this host's snapshot>", which decides below
# whether this run needs a new snapshot at all. The checkpoint line goes to
# stderr, and so to the log, as it always has.
SOURCE=$(ssh $SSH_OPTS "$VPS" "$REMOTE_PYTHON -c \"
import sqlite3, os, sys
db = os.path.expanduser('~/$REMOTE_DATA/brain.db')
c = sqlite3.connect(db)
rows = c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
c.close()
print(f'checkpoint: busy={rows[0]} log={rows[1]} checkpointed={rows[2]}', file=sys.stderr)
st = os.stat(db)
wal = db + '-wal'
snap = os.path.expanduser('~/.second-brain/$SNAP_NAME')
w = os.path.getsize(wal) if os.path.exists(wal) else 0
s = os.path.getsize(snap) if os.path.exists(snap) else 0
print(f'source: {st.st_mtime_ns} {st.st_size} {w} {s}')
\"" 2>> "$LOG_FILE")
CP_RC=$?
if [ $CP_RC -ne 0 ]; then
  log "WARN: WAL checkpoint failed (rc=$CP_RC), proceeding with rsync anyway"
fi

# --- Has the producer's brain.db changed since the last good pull? ---
# The .backup below rewrites the whole database on the producer, every hour, for a
# file that a quiet hour leaves exactly as it was. When nothing has written to it
# since the last pull that passed, this host's snapshot on the producer already IS
# that database, so it is reused. Unchanged means all of: the same mtime and size
# as recorded then, an empty -wal (so no write is waiting outside the main file),
# and the snapshot still there. Anything else, or no answer at all, snapshots.
#
# The record is written beside the stamp, on the all-clear path only, and removed
# by every failed run, so the run after a failure always snapshots afresh. It is
# its own file because db-pull.stamp is read as a single ISO timestamp, by the
# health check among others, and a second line would break that.
SOURCE_FILE="$HOME/.second-brain/db-pull.source"
read -r SRC_MTIME SRC_SIZE SRC_WAL SRC_SNAP <<< "$(printf '%s\n' "$SOURCE" | sed -n 's/^source: //p' | tail -1)"
PRODUCER=""
if [ $CP_RC -eq 0 ] && [ -n "${SRC_SIZE:-}" ]; then
  PRODUCER="$SRC_MTIME $SRC_SIZE"
fi
REUSE=0
if [ -n "$PRODUCER" ] && [ "${SRC_WAL:-}" = 0 ] && [ "${SRC_SNAP:-0}" != 0 ] &&
  [ "$(cat "$SOURCE_FILE" 2>/dev/null)" = "$PRODUCER" ]; then
  REUSE=1
fi

# --- Rsync brain.db (delta transfer into a new name, checked, then renamed) ---
# No --chmod: /usr/bin/rsync on macOS is openrsync, which rejects it, and the first
# pull that carried it failed every transfer. -a carries the producer's modes
# across, the producer's pull snapshot and offsite copies are 0600, the data
# directory is 0700 so nothing inside it is reachable by another account in the
# meantime, and the modes block after the transfers makes the result owner-only.
RSYNC_OPTS="-az --timeout=180"

# openrsync also fails a delta transfer once the local copy passes 4 GiB: brain.db
# crossed it on 2026-10-01 and every pull after that died at the same byte
# ("connection unexpectedly closed (1705084 bytes received so far)", rc=12), while
# embeddings.npz (1.5 GB) kept syncing. launchd's PATH has no Homebrew, so name a
# real rsync when one is installed; without one, copy brain.db whole.
RSYNC=/usr/bin/rsync
for candidate in /opt/homebrew/bin/rsync /usr/local/bin/rsync; do
  if [ -x "$candidate" ]; then
    RSYNC=$candidate
    break
  fi
done
RSYNC="${SB_PULL_RSYNC:-$RSYNC}"
DB_RSYNC_OPTS=$RSYNC_OPTS
[ "$RSYNC" = /usr/bin/rsync ] && DB_RSYNC_OPTS="$RSYNC_OPTS --whole-file"

# rsync builds its delta against the file already at the destination, and a new
# name has none. So a new name starts as a copy of the file it is to replace:
# `cp -c` clones on APFS, which writes no data, and a plain copy elsewhere still
# costs less than sending the whole file again. -p keeps the mtime, so rsync's
# quick check still skips a source that has not changed.
seed_from() {
  [ -f "$1" ] || return 0
  cp -c -p "$1" "$2" 2>/dev/null || cp -p "$1" "$2" 2>/dev/null || rm -f "$2"
}

# Snapshot on the VPS FIRST, then copy the snapshot. rsync'ing brain.db directly
# copied a live WAL-mode database that the VPS kept writing during the ~60s
# transfer, so the local file was a mix of pages from different points in time.
# It was silently corrupt for a long time: `PRAGMA integrity_check` on the Mac
# copy reported freelist and out-of-order-rowid damage while the VPS original
# returned `ok`, and this log said "synced OK" 386 times without one warning.
# The DEFER guard and the WAL checkpoint above both narrow the window; neither
# closes it, because nothing stops a write starting mid-rsync.
#
# `.backup` takes SQLite's own consistent snapshot and retries around writers,
# which is exactly what a plain file copy cannot do. It lives outside the repo
# data dir and PERSISTS between runs on purpose: a stable page layout is what
# lets rsync keep sending deltas instead of 3 GB every hour.
# Per consumer, not one shared file. Two Macs pull from this VPS (one at :15,
# one at :45) and both ran `sqlite3 .backup` into the SAME path, guarded only by
# a /tmp lock that is local to each machine and therefore guards nothing across
# them. A pull that overruns its 30-minute gap means two .backup runs writing one
# file, and both hosts then rsync whatever that produced. The DEFER check above
# looks for VPS sync jobs and would not see the other Mac.
# The extra disk is one snapshot per consumer, and rsync deltas are unaffected
# because each host keeps its own stable page layout, which is the whole reason
# this file persists between runs.
DB_RC=0
INTEGRITY_RC=0
EMB_RC=0
if [ $REUSE -eq 1 ]; then
  log "producer brain.db unchanged since the last good pull, reusing its snapshot"
  SNAP_RC=0
else
  # umask and chmod: the pull snapshot is a whole copy of brain.db, and .backup into
  # an existing file keeps that file's mode, so an old 0644 copy stays 0644 unless
  # it is set here. Double quotes around the path, so the producer's shell expands the
  # escaped $HOME in it; single quotes left chmod a literal path and failed the step.
  ssh $SSH_OPTS "$VPS" "umask 077; mkdir -p \$HOME/.second-brain && sqlite3 \$HOME/$REMOTE_DATA/brain.db \".backup '$REMOTE_SNAP'\" && chmod 600 \"$REMOTE_SNAP\"" 2>> "$LOG_FILE"
  SNAP_RC=$?
fi
if [ $SNAP_RC -ne 0 ]; then
  log "ERROR: VPS snapshot FAILED (rc=$SNAP_RC), NOT copying a live database; keeping the existing local file"
  DB_RC=$SNAP_RC
else
  rm -f "$NEW_DB" "$NEW_DB-wal" "$NEW_DB-shm"
  seed_from "$LOCAL_DATA/brain.db" "$NEW_DB"
  "$RSYNC" $DB_RSYNC_OPTS "$VPS:~/.second-brain/$SNAP_NAME" "$NEW_DB" 2>> "$LOG_FILE"
  DB_RC=$?
  if [ $DB_RC -ne 0 ]; then
    log "ERROR: brain.db sync FAILED (rc=$DB_RC), keeping the existing local file"
  else
    chmod 600 "$NEW_DB" 2>/dev/null
    # --- Integrity check: does the copy actually open and read cleanly? ---
    # It runs on the new copy BEFORE the rename. The check used to run after it, so
    # a bad copy had already replaced the good replica by the time it failed: the
    # stamp froze, but every session went on reading the bad file.
    #
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
    # file anyway, so the check reads the copy the way its only consumer does.
    INTEGRITY=$(sqlite3 "$NEW_DB" 'PRAGMA quick_check;' 2>&1 | head -3 | tr '\n' ' ')
    # The check's own open made these, for a name that is about to go.
    rm -f "$NEW_DB-wal" "$NEW_DB-shm"
    if [ "${INTEGRITY% }" != "ok" ]; then
      log "ERROR: new brain.db FAILED integrity check, keeping the existing local file: ${INTEGRITY}"
      INTEGRITY_RC=1
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
      rm -f "$LOCAL_DATA/brain.db-wal" "$LOCAL_DATA/brain.db-shm"
      mv -f "$NEW_DB" "$LOCAL_DATA/brain.db"
      DB_RC=$?
      # A reader that had the old inode open can recreate a -wal against the NEW
      # file between the two lines above.
      rm -f "$LOCAL_DATA/brain.db-wal" "$LOCAL_DATA/brain.db-shm"
      if [ $DB_RC -eq 0 ]; then
        DB_SIZE=$(du -h "$LOCAL_DATA/brain.db" | cut -f1)
        log "brain.db synced OK ($DB_SIZE)"
      else
        log "ERROR: brain.db could not be renamed into place (rc=$DB_RC)"
      fi
    fi
  fi
  rm -f "$NEW_DB" "$NEW_DB-wal" "$NEW_DB-shm"
fi

# --- Rsync embeddings.npz, the same way: a new name, a check, then the rename ---
# Only beside a database that was just put in place. The index is keyed by row
# id, and a newer index over an older database answers with rows that database
# does not hold. The check is the archive's own: `unzip -t` reads every array
# against its CRC, which is what loading the index would trip on.
if [ $DB_RC -eq 0 ] && [ $INTEGRITY_RC -eq 0 ]; then
  rm -f "$NEW_EMB"
  seed_from "$LOCAL_DATA/embeddings.npz" "$NEW_EMB"
  "$RSYNC" $RSYNC_OPTS "$VPS:~/$REMOTE_DATA/embeddings.npz" "$NEW_EMB" 2>> "$LOG_FILE"
  EMB_RC=$?
  if [ $EMB_RC -ne 0 ]; then
    log "ERROR: embeddings.npz sync FAILED (rc=$EMB_RC)"
  else
    unzip -tqq "$NEW_EMB" > /dev/null 2>&1
    EMB_RC=$?
    if [ $EMB_RC -ne 0 ]; then
      log "ERROR: new embeddings.npz FAILED its zip check (unzip rc=$EMB_RC), keeping the existing index"
    else
      chmod 600 "$NEW_EMB" 2>/dev/null
      mv -f "$NEW_EMB" "$LOCAL_DATA/embeddings.npz"
      EMB_RC=$?
      if [ $EMB_RC -eq 0 ]; then
        EMB_SIZE=$(du -h "$LOCAL_DATA/embeddings.npz" | cut -f1)
        log "embeddings.npz synced OK ($EMB_SIZE)"
      else
        log "ERROR: embeddings.npz could not be renamed into place (rc=$EMB_RC)"
      fi
    fi
  fi
  rm -f "$NEW_EMB"
else
  log "embeddings.npz not pulled: the index stays paired with the database already here"
fi

# ---- REPLICA-MODES-BEGIN ----
chmod 700 "$LOCAL_DATA" 2>/dev/null
chmod 600 "$LOCAL_DATA/brain.db" "$LOCAL_DATA/embeddings.npz" 2>/dev/null
# ---- REPLICA-MODES-END ----

# --- Pull encrypted offsite DB snapshots + GFS prune (14 daily + 8 weekly) ---
# The VPS writes these daily from sb-daily-sync via backup_db.py, but they land in
# data/backups/offsite on the SAME /dev/sdb as brain.db itself, so "offsite" is a
# misnomer until they are pulled here. This is the only hop that makes them real.
# Point OFFSITE_LOCAL at a OneDrive-synced folder for a free second offsite hop.
#
# This block was missing from the deployed copy while living in the repo copy, so
# the pull silently never ran and the directory below never existed. Hence mkdir -p
# rather than assuming: rsync will not create a two-level destination on its own.
OFFSITE_LOCAL="$HOME/second-brain-backups/offsite"
mkdir -p "$OFFSITE_LOCAL" && chmod 700 "$OFFSITE_LOCAL"
"$RSYNC" $RSYNC_OPTS "$VPS:~/$REMOTE_DATA/backups/offsite/brain-*.db.zst.enc" \
  "$OFFSITE_LOCAL/" 2>> "$LOG_FILE"
OFF_RC=$?
if [ "$OFF_RC" -eq 0 ]; then
  "$HOME/SourceCode/plessas-second-brain/.venv/bin/python3" \
    "$HOME/SourceCode/plessas-second-brain/scripts/backup_db.py" \
    --prune-only --offsite-dir "$OFFSITE_LOCAL" --gfs-daily 14 --gfs-weekly 8 >> "$LOG_FILE" 2>&1 || true
  OFF_COUNT=$(ls "$OFFSITE_LOCAL"/brain-*.db.zst.enc 2>/dev/null | wc -l | tr -d ' ')
  log "offsite snapshots synced + pruned (kept $OFF_COUNT)"
else
  # Report the code, do not guess the cause. The repo copy of this block asserted
  # "backup key unprovisioned on VPS?", which was wrong: the key was provisioned
  # the whole time and the real fault was the missing local directory above.
  log "ERROR: offsite snapshot pull FAILED (rsync rc=$OFF_RC)"
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
  # And the producer's brain.db as this copy found it, for the next run's
  # unchanged test above.
  if [ -n "$PRODUCER" ]; then
    printf '%s\n' "$PRODUCER" > "$SOURCE_FILE"
  else
    rm -f "$SOURCE_FILE"
  fi
else
  rm -f "$SOURCE_FILE"
  log "DONE: completed with errors (db=$DB_RC emb=$EMB_RC integrity=$INTEGRITY_RC)"
  tail -1000 "$LOG_FILE" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE" 2>/dev/null
  exit 1
fi

# Truncate log to last 1000 lines
tail -1000 "$LOG_FILE" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "$LOG_FILE" 2>/dev/null
