#!/bin/bash
# Conversation sync: exports and extracts Claude Code conversations.
set -uo pipefail

REPO_DIR="$HOME/SourceCode/plessas-second-brain"
PYTHON="$HOME/.venvs/second-brain/bin/python"
LOG_DIR="$HOME/.second-brain/logs"
LOG_FILE="$LOG_DIR/conversation-sync.log"
mkdir -p "$LOG_DIR"

# Overridable so the wrapper tests never touch a lock a real run may hold. The
# lock is removed with rm -rf, so the override must name a *.lock directory.
LOCK_DIR="${SB_CONVERSATION_SYNC_LOCK:-/tmp/sb-conversation-sync.lock}"
case "$LOCK_DIR" in
  *.lock) ;;
  *)
    echo "SB_CONVERSATION_SYNC_LOCK must name a *.lock directory, got: $LOCK_DIR" >&2
    exit 64
    ;;
esac
if [ -d "$LOCK_DIR" ]; then
  stored_pid=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")
  if [ -z "$stored_pid" ] || ! kill -0 "$stored_pid" 2>/dev/null; then
    rm -rf "$LOCK_DIR"
  else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] SKIP: already running (pid=$stored_pid)" >> "$LOG_FILE"
    exit 0
  fi
fi
mkdir -p "$LOCK_DIR" && echo $$ > "$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT INT TERM

[ -f "$HOME/.second-brain/needs_gcloud_reauth" ] && exit 0

echo "=== Conversation sync started: $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"

cd "$REPO_DIR" || exit 1
# Both steps always run: extraction drains what earlier exports staged, so a
# failed export must not stop it. Either command exiting non-zero fails the run,
# with the export's code first; this used to log "ok" and exit 0 whatever
# happened. Per-session errors inside a command are counted in its output and do
# not fail it: a corrupt session file stays in the 7-day window for a week.
"$PYTHON" -m src.cli export-conversations --days 7 >> "$LOG_FILE" 2>&1
export_rc=$?
"$PYTHON" -m src.cli extract-conversations --workers 2 >> "$LOG_FILE" 2>&1
extract_rc=$?

if [ "$export_rc" -ne 0 ] || [ "$extract_rc" -ne 0 ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') FAILED (export exit $export_rc, extract exit $extract_rc)" >> "$LOG_FILE"
  if [ "$export_rc" -ne 0 ]; then exit "$export_rc"; fi
  exit "$extract_rc"
fi
echo "$(date '+%Y-%m-%d %H:%M:%S') ok" >> "$LOG_FILE"
