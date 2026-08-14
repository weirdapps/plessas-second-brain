#!/bin/bash
# Nightly attachment pass — launchd surface for com.plessas.second-brain.attachments.
# At ~/.local/bin/ (legacy from OneDrive/TCC era; constraint no longer applies post-migration).

set -euo pipefail

# Vertex AI credentials for Claude LLM (Phase 2 + image vision)
[ -f "$HOME/.zprofile" ] && source "$HOME/.zprofile" 2>/dev/null || true

PROJECT="$HOME/SourceCode/plessas-second-brain"
SENTINEL="$HOME/.second-brain/needs_reauth"
GCLOUD_SENTINEL="$HOME/.second-brain/needs_gcloud_reauth"
[ -f "$SENTINEL" ] && exit 0
# gcloud ADC expired → Vertex AI extraction would fail. Skip until auth-watch
# clears the sentinel (its hourly probe restores it on first successful refresh).
[ -f "$GCLOUD_SENTINEL" ] && exit 0

cd "$PROJECT"
PYTHON="$HOME/.venvs/second-brain/bin/python"
LOG_DIR="$HOME/.second-brain/logs"
LOG_FILE="$LOG_DIR/attachments.log"
mkdir -p "$LOG_DIR"

# Phase 2 = LLM summary pass. Phase 1 (binary fetch + local text extract) runs
# inside the hourly outlook-sync get-mail step; this nightly job only catches
# up the LLM-summary backlog. Workers=4 mirrors the teams-sync default.
echo "$(date '+%Y-%m-%d %H:%M:%S') — starting attachment summary" >> "$LOG_FILE"
"$PYTHON" -m src.cli process-attachments --phase 2 --workers 4 >> "$LOG_FILE" 2>&1

# Image classification backfill (Stage 1 + Stage 3 vision)
echo "$(date '+%Y-%m-%d %H:%M:%S') — starting image classification" >> "$LOG_FILE"
"$PYTHON" -m src.cli process-images --limit 500 >> "$LOG_FILE" 2>&1

# SharePoint URL fetch backfill
echo "$(date '+%Y-%m-%d %H:%M:%S') — starting SharePoint fetch" >> "$LOG_FILE"
"$PYTHON" -m src.cli process-sharepoint --limit 200 >> "$LOG_FILE" 2>&1
