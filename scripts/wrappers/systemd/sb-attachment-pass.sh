#!/bin/bash
# Nightly attachment pass — launchd surface for com.plessas.second-brain.attachments.
# At ~/.local/bin/ (legacy from OneDrive/TCC era; constraint no longer applies post-migration).

# No -e. The stages below are independent, and under set -e the first one to
# fail aborted the rest: a single poison attachment in registration or Phase 1
# starved the image and SharePoint passes every night. Each stage runs through
# run_stage, and the first failure becomes the exit status.
set -uo pipefail

# Vertex AI credentials for Claude LLM (Phase 2 + image vision)
[ -f "$HOME/.zprofile" ] && source "$HOME/.zprofile" 2>/dev/null || true

PROJECT="$HOME/SourceCode/plessas-second-brain"
SENTINEL="$HOME/.second-brain/needs_reauth"
GCLOUD_SENTINEL="$HOME/.second-brain/needs_gcloud_reauth"
[ -f "$SENTINEL" ] && exit 0
# gcloud ADC expired → Vertex AI extraction would fail. Skip until auth-watch
# clears the sentinel (its hourly probe restores it on first successful refresh).
[ -f "$GCLOUD_SENTINEL" ] && exit 0

cd "$PROJECT" || exit 1
PYTHON="$HOME/.venvs/second-brain/bin/python"
LOG_DIR="$HOME/.second-brain/logs"
LOG_FILE="$LOG_DIR/attachments.log"
mkdir -p "$LOG_DIR"

overall_rc=0
run_stage() {
  local name="$1"
  shift
  "$@" >> "$LOG_FILE" 2>&1
  local rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S'): $name FAILED (exit $rc)" >> "$LOG_FILE"
    [ "$overall_rc" -eq 0 ] && overall_rc=$rc
  fi
  return 0
}

# Bulk registration. outlook-cli downloads attachment binaries hourly but does
# not record them, and the only writer that did was macOS-only, so the VPS
# accumulated ~9k unregistered files after the 2026-06-30 cutover. The hourly
# sync registers just a recent window because it runs under a 10-minute
# TimeoutStartSec; the whole backlog belongs here, where the budget is an hour.
echo "$(date '+%Y-%m-%d %H:%M:%S') — starting attachment registration" >> "$LOG_FILE"
run_stage "attachment registration" "$PYTHON" -m src.cli register-attachments

# Phase 1 = local text extraction. Previously left entirely to the hourly sync,
# which cannot absorb a backlog: cost per attachment ranges from ~0.1 s for a
# text part to minutes for OCR or a 30 MB workbook.
# 900 s of this unit's 1 h. Same reasoning as the hourly sync: per-attachment
# cost ranges from ~0.1 s to minutes, so only a wall-clock bound keeps the run
# inside TimeoutStartSec. Leftovers stay unprocessed and are picked up tomorrow.
echo "$(date '+%Y-%m-%d %H:%M:%S') — starting text extraction" >> "$LOG_FILE"
run_stage "text extraction" "$PYTHON" -m src.cli process-attachments --phase 1 --deadline-s 900

# Phase 2 = LLM summary pass. Workers=4 mirrors the teams-sync default.
#
# The llm_policy budget (3390 s) caps each CALL, not the loop, so on 2026-08-18
# this ran from 20:16 until systemd killed the unit at 21:00. Budget for the
# hour: registration + Phase 1 take ~16 min, leaving ~44; 30 here keeps room for
# the image and SharePoint passes below.
echo "$(date '+%Y-%m-%d %H:%M:%S') — starting attachment summary" >> "$LOG_FILE"
run_stage "attachment summary" \
  "$PYTHON" -m src.cli process-attachments --phase 2 --workers 4 --deadline-s 1800

# Image classification backfill (Stage 1 + Stage 3 vision)
echo "$(date '+%Y-%m-%d %H:%M:%S') — starting image classification" >> "$LOG_FILE"
run_stage "image classification" "$PYTHON" -m src.cli process-images --limit 500

# SharePoint URL fetch backfill
echo "$(date '+%Y-%m-%d %H:%M:%S') — starting SharePoint fetch" >> "$LOG_FILE"
run_stage "SharePoint fetch" "$PYTHON" -m src.cli process-sharepoint --limit 200

exit "$overall_rc"
