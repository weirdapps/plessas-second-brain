#!/bin/bash
# Daily document curation — launchd surface for com.plessas.second-brain.curate-docs.
# Classifies new second-brain attachments via Vertex AI Claude and places them
# under ~/Documents/{National,Personal}, refreshing READMEs + INDEX.md.
#
# Lives at ~/.local/bin/ (kept here from the OneDrive era for consistency with
# other second-brain wrappers — see sb-daily-sync.sh for the rationale).

set -uo pipefail

# Env setup — sources $HOME files only; brings Vertex AI creds via .zprofile.
[ -f "$HOME/.zprofile" ] && source "$HOME/.zprofile" 2>/dev/null || true
eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null || true
export PATH="$HOME/.local/bin:$PATH"

PROJECT="$HOME/SourceCode/plessas-second-brain"
PYTHON="$HOME/.venvs/second-brain/bin/python"
LOG_DIR="$HOME/.second-brain/logs"
LOG_FILE="$LOG_DIR/curate-docs.log"
mkdir -p "$LOG_DIR"

# --- Concurrency guard (PID-aware mkdir lock), the same as sb-daily-sync.sh's,
# where the reasons are, but for the traps below. Overridable so the wrapper
# tests never touch a lock a real run may hold; it is removed with rm -rf, so
# the override must be an absolute path to a *.lock directory.
LOCK_DIR="${SB_CURATE_DOCS_LOCK:-/tmp/sb-curate-docs.lock}"
case "$LOCK_DIR" in
  /*.lock) ;;
  *)
    echo "SB_CURATE_DOCS_LOCK must name an absolute *.lock directory, got: $LOCK_DIR" >&2
    exit 64
    ;;
esac
if [ -d "$LOCK_DIR" ]; then
  stored_pid=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")
  if [ -z "$stored_pid" ] || ! kill -0 "$stored_pid" 2>/dev/null; then
    rm -rf "$LOCK_DIR"  # stale (previous run crashed without trap cleanup)
    [ -e "$LOCK_DIR" ] && { echo "cannot remove the stale lock $LOCK_DIR" >&2; exit 73; }
  else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] SKIP: already running (pid=$stored_pid)" >> "$LOG_FILE"
    exit 0
  fi
fi
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  if [ -d "$LOCK_DIR" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] SKIP: another run took the lock" >> "$LOG_FILE"
    exit 0
  fi
  echo "cannot create the lock directory $LOCK_DIR" >&2
  exit 73
fi
echo $$ > "$LOCK_DIR/pid"
# A stop signal removes the lock and then dies of that signal. Exiting with
# status 143 would mark the unit failed, where a death by SIGTERM, which is what
# systemd saw while the job was exec'd, is a clean stop.
trap 'rm -rf "$LOCK_DIR"' EXIT
trap 'rm -rf "$LOCK_DIR"; trap - TERM; kill -TERM $$' TERM
trap 'rm -rf "$LOCK_DIR"; trap - INT; kill -INT $$' INT
trap 'rm -rf "$LOCK_DIR"; trap - HUP; kill -HUP $$' HUP

# No needs_reauth gate here on purpose. Curation reads brain.db and calls
# Vertex; curate_documents_daily.py names neither outlook-cli nor sharepoint-cli,
# so a dead M365 session cannot affect it. The check used to be here, copied from
# the mail wrappers, and on 2026-09-02 it stopped six days of curation because
# Outlook was down, while curation's own SharePoint session was healthy
# throughout. The real dependency is guarded immediately below.

# Skip if gcloud ADC expired — Vertex AI classification would fail.
if [ -f "$HOME/.second-brain/needs_gcloud_reauth" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] SKIP: needs_gcloud_reauth sentinel present" >> "$LOG_FILE"
  exit 0
fi

# Skip if Vertex AI creds are missing — we don't want silent regex fallback.
# Either project name will do; claude_extract reads VERTEX_SDK_PROJECT first.
if [ -z "${VERTEX_SDK_PROJECT:-}${ANTHROPIC_VERTEX_PROJECT_ID:-}" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] SKIP: no Vertex project (VERTEX_SDK_PROJECT or ANTHROPIC_VERTEX_PROJECT_ID)" >> "$LOG_FILE"
  exit 0
fi

echo "=== Daily curate started: $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"
# Not exec: the EXIT trap has to outlive the job to remove the lock. Under exec
# every run left its lock behind, and only the dead-pid check let the next one in.
"$PYTHON" "$PROJECT/scripts/curate_documents_daily.py" --max-new "${CURATE_MAX_NEW:-30}"
exit $?
