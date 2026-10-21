#!/bin/bash
# Topic pages, nightly: writes the page of each live topic whose evidence is new
# (src/pipeline/topic_pages.py). At most 20 pages, one model call each, and only the
# ones with new items, so most nights a handful. Started by sb-topic-pages.timer
# through its host-local shim, which sources the Vertex environment.

set -uo pipefail

REPO_DIR="$HOME/SourceCode/plessas-second-brain"
PYTHON="$HOME/.venvs/second-brain/bin/python"
LOG_DIR="$HOME/.second-brain/logs"
LOG_FILE="$LOG_DIR/topic-pages.log"
mkdir -p "$LOG_DIR"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

echo "=== Topic pages started: $(ts) ===" >> "$LOG_FILE"

# Every page is a model call. Logged, because a skip that leaves no line cannot be
# told from a run that never started, and 69, not 0, because a night with no pages is
# missing work, not a quiet night: the 0 here pinged hc-success@ green.
if [ -f "$HOME/.second-brain/needs_gcloud_reauth" ]; then
  echo "$(ts) SKIP: needs_gcloud_reauth sentinel present" >> "$LOG_FILE"
  exit 69
fi

cd "$REPO_DIR" || exit 1
"$PYTHON" -m src.cli topic-pages >> "$LOG_FILE" 2>&1
rc=$?

if [ "$rc" -eq 0 ]; then
  echo "$(ts) ok" >> "$LOG_FILE"
else
  echo "$(ts) FAILED (exit $rc)" >> "$LOG_FILE"
fi
exit "$rc"
