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

# --- Concurrency guard (PID-aware mkdir lock). See sb-daily-sync.sh for rationale.
LOCK_DIR="/tmp/sb-curate-docs.lock"
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
if [ -z "${ANTHROPIC_VERTEX_PROJECT_ID:-}" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] SKIP: no ANTHROPIC_VERTEX_PROJECT_ID" >> "$LOG_FILE"
  exit 0
fi

echo "=== Daily curate started: $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"
exec "$PYTHON" "$PROJECT/scripts/curate_documents_daily.py" \
  --max-new "${CURATE_MAX_NEW:-30}"
