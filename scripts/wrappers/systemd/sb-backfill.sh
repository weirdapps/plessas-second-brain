#!/bin/bash
# Backfill, nightly and TEMPORARY: one capped batch of the topic registry (issue 180) and
# one of the vectors backfill (issue 171), until both report nothing left; then it
# disables its own timer. Each command's dry run is read first, so a finished one is
# not run again, and the summaries wait until no email or copy chunk is left. Started
# by sb-backfill.timer through its host-local shim, which sources the Vertex environment.

set -uo pipefail

REPO_DIR="$HOME/SourceCode/plessas-second-brain"
PYTHON="$HOME/.venvs/second-brain/bin/python"
LOG_DIR="$HOME/.second-brain/logs"
LOG_FILE="$LOG_DIR/backfill.log"
mkdir -p "$LOG_DIR"
ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "$(ts) $*" >> "$LOG_FILE"; }

echo "=== Backfill started: $(ts) ===" >> "$LOG_FILE"

# Cluster names and embeddings are model calls. 69, not 0: a skip is work not done.
if [ -f "$HOME/.second-brain/needs_gcloud_reauth" ]; then
  log "SKIP: needs_gcloud_reauth sentinel present"
  exit 69
fi

cd "$REPO_DIR" || exit 1

# Runs the CLI, logs its output and leaves it in $OUT.
cli() {
  OUT=$("$PYTHON" -m src.cli "$@" 2>&1)
  local rc=$?
  printf '%s\n' "$OUT" >> "$LOG_FILE"
  return "$rc"
}

# The number after a pattern in $OUT, or nothing. An empty answer is unreadable, never 0.
count() { printf '%s\n' "$OUT" | sed -n "s/.*$1\([0-9][0-9]*\).*/\1/p" | tail -1; }

failed=0

# 1. Topic registry: what is left is the dry run's `'unplaced': N`.
unplaced=""
if cli maint topic-registry --limit 1 --llm-calls 0 --show 0; then
  unplaced=$(count "'unplaced': ")
fi
if [ -z "$unplaced" ]; then
  log "FAILED: could not read what the topic registry has left"
  failed=1
elif [ "$unplaced" -gt 0 ]; then
  log "topic registry: $unplaced unplaced; one batch"
  cli maint topic-registry --limit 25000 --llm-calls 20 --apply || failed=1
fi

# 2. Vectors: email chunks and copy chunks first, then the 768 summaries.
emails="" copies="" sums=""
if cli maint vectors-backfill --summaries --limit 1; then
  emails=$(count "email chunks: ")
  copies=$(count "copy chunks to drop: ")
  sums=$(count "summaries at [0-9]*: ")
fi
if [ -z "$emails" ] || [ -z "$copies" ] || [ -z "$sums" ]; then
  log "FAILED: could not read what the vectors backfill has left"
  failed=1
elif [ "$emails" -gt 0 ] || [ "$copies" -gt 0 ]; then
  log "vectors: $emails emails, $copies copy chunks left; one batch"
  cli maint vectors-backfill --apply --limit 2000 || failed=1
elif [ "$sums" -gt 0 ]; then
  log "vectors: summaries left; one batch"
  cli maint vectors-backfill --apply --summaries --limit 5000 || failed=1
fi

# 3. Both read and both at zero: the backfill is done, and the timer goes with it.
if [ "$failed" -eq 0 ] && [ "$unplaced" = 0 ] && [ "$emails" = 0 ] \
  && [ "$copies" = 0 ] && [ "$sums" = 0 ]; then
  log "nothing left in the topic registry or the vectors backfill"
  if systemctl --user disable --now sb-backfill.timer >> "$LOG_FILE" 2>&1; then
    log "disabled sb-backfill.timer"
  else
    log "FAILED: could not disable sb-backfill.timer"
    failed=1
  fi
fi

if [ "$failed" -eq 0 ]; then
  log "ok"
else
  log "FAILED"
fi
exit "$failed"
