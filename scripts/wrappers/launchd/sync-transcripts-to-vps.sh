#!/bin/bash
# Push a Claude Code transcript to the producer, whose conversation sync reads it
# into conversation memory. scripts/hooks/session-end-push.sh starts it when a
# session ends, with the path of that session's transcript.
#
# The producer reads ~/.claude/projects/<project>/<session>.jsonl, the layout
# Claude Code writes, so the file lands at the same place there. That one file
# goes and nothing else: never the session's subagents/ directory, which the
# exporter does not read, and never a file outside a projects directory.
#
# Privacy, on purpose:
#   - umask 077: the transcript keeps its 0600 and lands in 0700 directories on
#     the producer; the log, the stamp and every marker are owner-only here.
#   - the log names files, never what is in them.
#
# Liveness follows the WhatsApp push's contract (sync-whatsapp-to-vps.sh): a stamp
# on both hosts after every successful push, a failure marker on both hosts when
# a push fails, and an unreachable producer skipped quietly until the gap
# outlives either threshold. A session ending starts a push, not a clock, so a
# transcript that could not go waits in a pending list and goes with the next push.

set -uo pipefail
umask 077
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

VPS="${SB_TRANSCRIPTS_VPS:-vps}"
# The transport, overridable so a test can stand in for it by name. A stub put on
# PATH is not enough: the PATH line above puts the real binaries first.
SSH="${SB_TRANSCRIPTS_SSH:-ssh}"
# Homebrew's rsync where there is one: /usr/bin/rsync on macOS is openrsync, which
# rejects --chmod (none is passed here) and fails deltas past 4 GiB (sb-db-pull.sh).
RSYNC="${SB_TRANSCRIPTS_RSYNC:-}"
if [ -z "$RSYNC" ]; then
  RSYNC=/usr/bin/rsync
  for candidate in /opt/homebrew/bin/rsync /usr/local/bin/rsync; do
    if [ -x "$candidate" ]; then
      RSYNC=$candidate
      break
    fi
  done
fi
LOG="${SB_TRANSCRIPTS_LOG:-$HOME/Library/Logs/transcripts-sync-vps.log}"
STATE="$HOME/.second-brain"
STAMP="$STATE/transcripts-sync.stamp"
FAIL="$STATE/transcripts-sync.fail"
PENDING="$STATE/transcripts-sync.pending"
REMOTE_DIR=".claude/projects"
SSH_OPTS=(-o ConnectTimeout=10 -o BatchMode=yes)
LOCK="${TMPDIR:-/tmp}/.transcripts-sync-vps.lock"
TARGET="${1:-}"

mkdir -p "$STATE" "$(dirname "$LOG")"
log() { echo "[transcripts-sync] $(date '+%Y-%m-%d %H:%M:%S') $*" >>"$LOG"; }

# One push at a time. Sessions often end together (a window of tabs closed), and
# each push has a transcript of its own, so a second push waits its turn instead
# of skipping; after two minutes it leaves its transcript to the next push.
locked=""
for _ in $(seq 1 120); do
  if mkdir "$LOCK" 2>/dev/null; then
    locked=1
    break
  fi
  pid=$(cat "$LOCK/pid" 2>/dev/null || echo "")
  if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
    rm -rf "$LOCK"
    continue
  fi
  sleep 1
done
if [ -z "$locked" ]; then
  [ -n "$TARGET" ] && printf '%s\n' "$TARGET" >>"$PENDING"
  log "another push held the lock for 2 minutes; $(basename "${TARGET:-none}") waits for the next push"
  exit 0
fi
echo $$ >"$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT INT TERM

# mtime of a file as epoch seconds, GNU or BSD stat, empty when absent. GNU
# first: BSD rejects -c without printing anything, while GNU's -f prints
# filesystem details before failing, which would land in the value.
mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null; }

# What may be sent: <projects>/<project>/<session>.jsonl, a session's own
# transcript. The names are checked because they go into a command the producer runs.
sendable() {
  local path="$1" name project
  name=$(basename "$path")
  project=$(basename "$(dirname "$path")")
  [ -f "$path" ] || return 1
  case "$path" in */subagents/*) return 1 ;; esac
  [ "$(basename "$(dirname "$(dirname "$path")")")" = projects ] || return 1
  [[ "$name" =~ ^[A-Za-z0-9._-]+\.jsonl$ ]] || return 1
  [[ "$project" =~ ^[A-Za-z0-9._-]+$ ]] || return 1
}

# The pending list and this push's transcript, once each, in order.
queue=$({
  cat "$PENDING" 2>/dev/null
  [ -n "$TARGET" ] && printf '%s\n' "$TARGET"
} | awk 'NF && !seen[$0]++')

# A session ends whenever the owner closes one, and a laptop off the network is
# this job's normal state, so a skip stays exit 0 and the transcript waits. Once
# the last successful push is 24h old, or after 12 consecutive unreachable runs,
# the failure marker is written and it pages.
# ---- UNREACHABLE-GATE-BEGIN ----
UNREACHABLE_RUNS_FILE="$STATE/transcripts-sync.unreachable-runs"
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
  if [ -n "$queue" ]; then
    printf '%s\n' "$queue" >"$PENDING.tmp" && mv -f "$PENDING.tmp" "$PENDING"
  fi
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
    "umask 077; mkdir -p ~/.second-brain && printf '%s\n%s\n' '$now' '$reason' > ~/.second-brain/transcripts-sync.fail" 2>>"$LOG"; then
    log "WARN: failure marker write to the producer failed"
  fi
  log "FAIL: $reason; failure marker written"
  exit 1
}

sent=0
total=0
failed=""
while IFS= read -r path; do
  [ -n "$path" ] || continue
  if ! sendable "$path"; then
    log "not sent, not a session transcript: $(basename "$path")"
    continue
  fi
  total=$((total + 1))
  project=$(basename "$(dirname "$path")")
  if "$SSH" "${SSH_OPTS[@]}" "$VPS" "umask 077; mkdir -p $REMOTE_DIR/$project" 2>>"$LOG" &&
    "$RSYNC" -t --timeout=120 -e "$SSH ${SSH_OPTS[*]}" "$path" "$VPS:$REMOTE_DIR/$project/" 2>>"$LOG"; then
    sent=$((sent + 1))
  else
    failed="$failed$path"$'\n'
  fi
done <<<"$queue"

if [ -n "$failed" ]; then
  printf '%s' "$failed" >"$PENDING.tmp" && mv -f "$PENDING.tmp" "$PENDING"
  fail "$((total - sent)) of $total transcripts did not reach the producer"
fi
rm -f "$PENDING"
if [ "$total" -eq 0 ]; then
  log "nothing to send"
  exit 0
fi

if "$SSH" "${SSH_OPTS[@]}" "$VPS" \
  "umask 077; mkdir -p ~/.second-brain && printf '%s\n' '$now' > ~/.second-brain/transcripts-sync.stamp && rm -f ~/.second-brain/transcripts-sync.fail" 2>>"$LOG"; then
  log "pushed $sent transcript(s); stamp written ($now)"
else
  log "WARN: pushed $sent transcript(s), but the stamp write on the producer failed"
fi
printf '%s\n' "$now" >"$STAMP"
rm -f "$FAIL"
exit 0
