#!/bin/bash
# WhatsApp sync on the producer: sb-whatsapp-sync.timer, :55 at 01 and 07-22.
#
# Lives at ~/.local/bin/sb-whatsapp-sync.sh, started by the host-local shim
# ~/scripts/run-sb-whatsapp-sync.sh (which sets PATH and sources the Vertex
# environment, as run-sb-teams-sync.sh does). Same shape as sb-teams-sync.sh,
# without the Teams auth steps: the input is a file the bridge's Mac pushed at
# :50, not an API.
#
# :55 is the widest gap on the brain.db write lock: calendar (:48) is done by
# about :51, outlook starts at :10, and sb-teams-sync (:30, up to 11 minutes) is
# long finished. On even hours news (:50) now loads what it stages too (since
# 2026-10-21) and can still be writing at :55; what covers that overlap is the
# store's 60 s busy timeout, so a "database is locked" in this log means it did not.
#
# Exit codes are the CLI's: 0 ok, 66 the snapshot is missing or unreadable (a
# broken push, which should page), 1 anything else.

set -uo pipefail

# Env setup: sources $HOME files only. Step 3 (extraction) needs
# VERTEX_SDK_PROJECT or ANTHROPIC_VERTEX_PROJECT_ID.
[ -f "$HOME/.zprofile" ] && source "$HOME/.zprofile" 2>/dev/null || true

PROJECT="$HOME/SourceCode/plessas-second-brain"
PYTHON="$HOME/.venvs/second-brain/bin/python"
STATE="$HOME/.second-brain/whatsapp_sync_wrapper.json"
LOG_DIR="$HOME/.second-brain/logs"
LOG="$LOG_DIR/whatsapp-sync.log"

mkdir -p "$LOG_DIR" "$(dirname "$STATE")"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

read_failures() {
  [ -f "$STATE" ] || { echo 0; return; }
  sed -n 's/.*"consecutive_failures": *\([0-9][0-9]*\).*/\1/p' "$STATE" 2>/dev/null | head -1 | grep . || echo 0
}

write_state() {
  printf '{"consecutive_failures": %d, "last_run_at": "%s", "last_status": "%s"}\n' \
    "$1" "$(ts)" "$2" > "$STATE"
}

if [ "${SB_WHATSAPP_SYNC_DISABLED:-0}" = "1" ]; then
  echo "$(ts) opted out via SB_WHATSAPP_SYNC_DISABLED" >> "$LOG"
  exit 0
fi

# gcloud ADC expired: extraction would fail, and sb-auth-watch owns the sentinel
# and clears it on its next good probe. The ingest does not need it: the messages are
# stored and searchable without the model, and the embeddings go through the Gemini
# API. So the run ingests and tries one session (--limit 1), which defers to
# 'pending' on the dead credential instead of every session failing in turn, and
# ends 69, not 0: on 2026-10-20 this skipped whole and pinged hc-success@ green.
limit_args=""
if [ -f "$HOME/.second-brain/needs_gcloud_reauth" ]; then
  echo "$(ts) gcloud reauth sentinel present: ingest only, extraction owed" >> "$LOG"
  limit_args="--limit 1"
fi

cd "$PROJECT" || { echo "$(ts) abort (cannot cd to $PROJECT)" >> "$LOG"; exit 1; }
echo "$(ts) start" >> "$LOG"
# The CLI prints counts only, never a message, so its output is safe in this log.
# shellcheck disable=SC2086  # empty, or the one option and its value
"$PYTHON" -m src.cli whatsapp-sync $limit_args >> "$LOG" 2>&1
rc=$?
if [ "$rc" -eq 0 ] && [ -n "$limit_args" ]; then
  rc=69
fi

failures=$(read_failures)
if [ "$rc" -eq 0 ]; then
  write_state 0 ok
  echo "$(ts) ok" >> "$LOG"
else
  failures=$((failures + 1))
  write_state "$failures" "fail rc=$rc"
  echo "$(ts) fail rc=$rc consecutive=$failures" >> "$LOG"
fi

exit "$rc"
