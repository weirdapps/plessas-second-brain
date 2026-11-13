#!/bin/zsh
# Fetch the Teams meeting transcripts not fetched before and push them to the producer's
# transcript inbox. Hourly via LaunchAgent com.plessas.meeting-transcript-push-vps (2026-11-13).
#
#   1. `brain meetings-fetch` (src/export/teams_transcripts.py) finds every recording and
#      transcript file the owner can open through Microsoft Search, reads each new transcript
#      from the organizer's OneDrive with the M365 CLI session, and writes one JSON record a
#      transcript to the outbox. It runs here because only this Mac holds an M365 session
#      (the producer cannot renew one).
#   2. The outbox goes to ~/.second-brain/meeting-transcript-inbox/ on the producer, each record
#      leaving the Mac once sent (--remove-source-files); the nightly sb-attachment-pass
#      stores them as documents (`brain meetings-ingest --remove-ingested`) before Phase 2,
#      which summarises them and takes their decisions and action items.
#
# Stamp and failure marker on both hosts, as the document push: the producer's health check
# reads them for its Meetings line (scripts/health_check.py check_meetings). An expired
# M365 CLI session (fetch exit 2) writes the marker with the fix in it; a search that fails
# for a moment (exit 4) or one transcript that fails (exit 1) does not, since the next run
# asks again.

set -uo pipefail
umask 077
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

VPS="${SB_TX_VPS:-vps}"
# By name so a test can stand in for each (a PATH stub would be passed over: see above).
SSH="${SB_TX_SSH:-ssh}"
# Homebrew rsync by name: the system openrsync failed deltas past 4 GiB on this Mac.
RSYNC="${SB_TX_RSYNC:-/opt/homebrew/bin/rsync}"
BRAIN="${SB_TX_BRAIN:-$HOME/SourceCode/plessas-second-brain/brain}"
LOG="${SB_TX_LOG:-$HOME/Library/Logs/meeting-transcript-push-vps.log}"
STATE="${SB_TX_STATE:-$HOME/.second-brain}"
OUTBOX="$STATE/meeting-transcript-outbox"
STAMP="$STATE/meeting-transcript-push.stamp"
FAIL="$STATE/meeting-transcript-push.fail"
REMOTE_DIR=".second-brain/meeting-transcript-inbox"
SSH_OPTS=(-o ConnectTimeout=10 -o BatchMode=yes)
LOCK="${TMPDIR:-/tmp}/.meeting-transcript-push-vps.lock"
# A laptop off the network is normal: the marker is written only once the last push is
# this old.
UNREACHABLE_MAX_AGE=86400

# BRAIN_M365_TOKEN_CACHE, the M365 CLI's token cache, lives in the owner-only env file, as
# run_mcp.sh reads it: launchd starts this with no shell profile.
if [ -f "$STATE/env" ]; then
  set -a
  . "$STATE/env"
  set +a
fi

mkdir -p "$STATE" "$OUTBOX" "$(dirname "$LOG")"
log() { echo "[meeting-transcript-push] $(date '+%Y-%m-%d %H:%M:%S') $*" >>"$LOG"; }

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
trap 'rm -rf "$LOCK"' EXIT INT TERM

now=$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')

if ! "$SSH" "${SSH_OPTS[@]}" "$VPS" true 2>/dev/null; then
  last_ok=$(/usr/bin/stat -f %m "$STAMP" 2>/dev/null || echo 0)
  if [ $(($(date +%s) - last_ok)) -lt "$UNREACHABLE_MAX_AGE" ]; then
    log "producer unreachable: skipping (pages 24h after the last successful push)"
    exit 0
  fi
  printf '%s\n%s\n' "$now" "unreachable: ssh to $VPS failed for over 24h" >"$FAIL"
  log "producer unreachable for over 24h; failure marker written"
  exit 1
fi

# Reasons are fixed strings: they go inside single quotes on the producer.
fail() {
  printf '%s\n%s\n' "$now" "$1" >"$FAIL"
  "$SSH" "${SSH_OPTS[@]}" "$VPS" \
    "umask 077; mkdir -p ~/.second-brain && printf '%s\n%s\n' '$now' '$1' > ~/.second-brain/meeting-transcript-push.fail" 2>>"$LOG" ||
    log "WARN: failure marker write to the producer failed"
  log "FAIL: $1; failure marker written"
  exit 1
}

out=$("$BRAIN" meetings-fetch --outbox "$OUTBOX" --state "$STATE/meeting-meetings-fetched.json" 2>&1)
rc=$?
log "$(printf '%s\n' "$out" | grep -v ' INFO ' | tail -6 | tr '\n' ' ')"
case "$rc" in
  0) ;;
  1) log "some transcripts failed; they are asked again next run" ;;
  2) fail "M365 CLI session expired: log in to M365 again on the Mac" ;;
  4)
    log "search failed for now (rc 4); next run asks again"
    exit 0
    ;;
  *) fail "meetings-fetch exited $rc" ;;
esac

count=$(find "$OUTBOX" -maxdepth 1 -name '*.json' | wc -l | tr -d ' ')
if [ "$count" -gt 0 ]; then
  "$SSH" "${SSH_OPTS[@]}" "$VPS" "umask 077; mkdir -p ~/$REMOTE_DIR" 2>>"$LOG" ||
    fail "could not create the inbox on the producer"
  sent=$("$RSYNC" -t --remove-source-files --partial --chmod=Fu=rw --include='*.json' --exclude='*' \
    -e "$SSH ${SSH_OPTS[*]}" "$OUTBOX/" "$VPS:$REMOTE_DIR/" 2>&1)
  rrc=$?
  if [ "$rrc" -ne 0 ]; then
    log "rsync failed (rc=$rrc): $(printf '%s\n' "$sent" | head -3 | tr '\n' ' ')"
    fail "rsync failed for $count transcript records"
  fi
fi

rm -f "$FAIL"
touch "$STAMP"
"$SSH" "${SSH_OPTS[@]}" "$VPS" \
  "mkdir -p ~/.second-brain && printf '%s\n' '$now' > ~/.second-brain/meeting-transcript-push.stamp && rm -f ~/.second-brain/meeting-transcript-push.fail" 2>>"$LOG" ||
  log "WARN: stamp write to the producer failed"
log "pushed $count transcript record(s)"
exit 0
