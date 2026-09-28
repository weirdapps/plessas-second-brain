#!/bin/bash
# Push a minimized WhatsApp snapshot to the producer.
# Runs hourly at :50 via LaunchAgent com.plessas.whatsapp-sync-vps, on the Mac
# that runs the WhatsApp bridge (com.plessas.whatsapp-bridge).
#
# The bridge's store lives only on that Mac, and the producer builds brain.db.
# So this job builds a snapshot with scripts/whatsapp_snapshot.py, which copies
# chats and messages from an allowlist of columns and never the media URL, key
# or file hashes, and hands it to the producer, where `brain whatsapp-sync`
# ingests it on its own timer.
#
# Privacy, on purpose:
#   - umask 077: the snapshot, the log and every marker are owner-only here, and
#     the directory and file on the producer are made 0700 and 0600 before the
#     snapshot takes its final name.
#   - the snapshot is built in a private temp directory and removed on exit, so
#     the only copies are the bridge's own store and the producer's.
#   - the log carries counts, never a message, a name or a number.
#
# Liveness follows sync-documents-to-vps.sh: a stamp on both hosts after every
# successful push, a failure marker on both hosts when a run fails, and an
# unreachable producer skipped quietly until the gap outlives either threshold.

set -uo pipefail
umask 077
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

VPS="${SB_WHATSAPP_VPS:-vps}"
# The transport, overridable so a test can stand in for it by name. A stub put on
# PATH is not enough: the PATH line above puts /usr/bin first, so a test once
# reached the real producer through the real ssh.
SSH="${SB_WHATSAPP_SSH:-ssh}"
SCP="${SB_WHATSAPP_SCP:-scp}"
# Where the bridge keeps its store is host configuration, set in the installed
# plist, not in this public archive.
SOURCE="${SB_WHATSAPP_SOURCE:-}"
HELPER="${SB_WHATSAPP_HELPER:-$HOME/SourceCode/plessas-second-brain/scripts/whatsapp_snapshot.py}"
PYTHON="${SB_WHATSAPP_PYTHON:-/usr/bin/python3}"
LOG="${SB_WHATSAPP_LOG:-$HOME/Library/Logs/whatsapp-sync-vps.log}"
STATE="$HOME/.second-brain"
STAMP="$STATE/whatsapp-sync.stamp"
FAIL="$STATE/whatsapp-sync.fail"
REMOTE_DIR=".second-brain/whatsapp"
REMOTE_PART="$REMOTE_DIR/.whatsapp-snapshot.db.part"
REMOTE_FILE="$REMOTE_DIR/whatsapp-snapshot.db"
SSH_OPTS=(-o ConnectTimeout=10 -o BatchMode=yes)
LOCK="${TMPDIR:-/tmp}/.whatsapp-sync-vps.lock"

mkdir -p "$STATE" "$(dirname "$LOG")"
log() { echo "[whatsapp-sync] $(date '+%Y-%m-%d %H:%M:%S') $*" >>"$LOG"; }

# One run at a time.
if ! mkdir "$LOCK" 2>/dev/null; then
  pid=$(cat "$LOCK/pid" 2>/dev/null || echo "")
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    log "already running (pid $pid), skipping"
    exit 0
  fi
  rm -rf "$LOCK"
  mkdir "$LOCK" 2>/dev/null || exit 0
fi
echo $$ >"$LOCK/pid"
WORK=""
cleanup() {
  [ -n "$WORK" ] && rm -rf "$WORK"
  rm -rf "$LOCK"
}
trap cleanup EXIT INT TERM

# mtime of a file as epoch seconds, GNU or BSD stat, empty when absent. GNU
# first: BSD rejects -c without printing anything, while GNU's -f prints
# filesystem details before failing, which would land in the value.
mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null; }

# Hourly, and a laptop off the network is this job's normal state, so a skip or
# a night away stays exit 0. Once the last successful push is 24h old, or after
# 12 consecutive unreachable runs, the failure marker is written and it pages.
# ---- UNREACHABLE-GATE-BEGIN ----
UNREACHABLE_RUNS_FILE="$STATE/whatsapp-sync.unreachable-runs"
UNREACHABLE_MAX_RUNS=12
UNREACHABLE_MAX_AGE=86400

unreachable_verdict() {
  local runs last_ok since=-1 ago reason
  runs=$(cat "$UNREACHABLE_RUNS_FILE" 2>/dev/null)
  case "$runs" in '' | *[!0-9]*) runs=0 ;; esac
  runs=$((runs + 1))
  printf '%s\n' "$runs" >"$UNREACHABLE_RUNS_FILE"
  last_ok=$(mtime "$STAMP")
  [ -n "$last_ok" ] && since=$(($(date +%s) - last_ok))
  if [ "$runs" -lt "$UNREACHABLE_MAX_RUNS" ] && [ "$since" -lt "$UNREACHABLE_MAX_AGE" ]; then
    log "producer unreachable: skipping ($runs consecutive; pages at $UNREACHABLE_MAX_RUNS, or 24h after the last successful push)"
    return 0
  fi
  if [ "$since" -ge 0 ]; then ago="$((since / 3600))h ago"; else ago="never recorded here"; fi
  reason="unreachable: ssh to $VPS failed on $runs consecutive runs, last successful push $ago"
  printf '%s\n%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')" "$reason" >"$FAIL"
  log "producer unreachable: $reason; failure marker written"
  return 1
}
# ---- UNREACHABLE-GATE-END ----
if ! "$SSH" "${SSH_OPTS[@]}" "$VPS" true 2>/dev/null; then
  unreachable_verdict
  exit $?
fi
rm -f "$UNREACHABLE_RUNS_FILE"

now=$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')

# A reason is interpolated into the remote command below, so it never carries a
# single quote; every reason here is a fixed string plus a number.
fail() {
  local reason="$1"
  printf '%s\n%s\n' "$now" "$reason" >"$FAIL"
  if ! "$SSH" "${SSH_OPTS[@]}" "$VPS" \
    "umask 077; mkdir -p ~/.second-brain && printf '%s\n%s\n' '$now' '$reason' > ~/.second-brain/whatsapp-sync.fail" 2>>"$LOG"; then
    log "WARN: failure marker write to the producer failed"
  fi
  log "FAIL: $reason; failure marker written"
  exit 1
}

if [ -z "$SOURCE" ]; then
  fail "SB_WHATSAPP_SOURCE is not set: point it at the bridge store in the LaunchAgent"
fi
WORK=$(mktemp -d "${TMPDIR:-/tmp}/whatsapp-sync.XXXXXX") || fail "could not create a private temp directory"
SNAPSHOT="$WORK/whatsapp-snapshot.db"

counts=$("$PYTHON" "$HELPER" "$SOURCE" "$SNAPSHOT" 2>>"$LOG")
rc=$?
if [ "$rc" -ne 0 ]; then
  fail "snapshot build failed (rc=$rc)"
fi
log "snapshot built: $counts"
size=$(wc -c <"$SNAPSHOT" | tr -d ' ')

if ! "$SSH" "${SSH_OPTS[@]}" "$VPS" "umask 077; mkdir -p $REMOTE_DIR && chmod 700 $REMOTE_DIR" 2>>"$LOG"; then
  fail "could not prepare $REMOTE_DIR on the producer"
fi
if ! "$SCP" -q "${SSH_OPTS[@]}" "$SNAPSHOT" "$VPS:$REMOTE_PART" 2>>"$LOG"; then
  fail "scp of the snapshot failed"
fi
# Size-checked, then renamed in one step, so the ingest never reads a half-copied file.
if ! "$SSH" "${SSH_OPTS[@]}" "$VPS" \
  "chmod 600 $REMOTE_PART && [ \$(wc -c < $REMOTE_PART) -eq $size ] && mv -f $REMOTE_PART $REMOTE_FILE" 2>>"$LOG"; then
  fail "the snapshot did not arrive whole on the producer"
fi

if "$SSH" "${SSH_OPTS[@]}" "$VPS" \
  "umask 077; printf '%s\n' '$now' > ~/.second-brain/whatsapp-sync.stamp && rm -f ~/.second-brain/whatsapp-sync.fail" 2>>"$LOG"; then
  log "pushed $size bytes; stamp written ($now)"
else
  log "WARN: pushed $size bytes, but the stamp write on the producer failed"
fi
printf '%s\n' "$now" >"$STAMP"
rm -f "$FAIL"
exit 0
