#!/bin/bash
# Nightly health check: launchd/systemd surface for the second-brain health-check job.
# Runs daily. Checks all components, auto-fixes where possible, emails a status report.
set -uo pipefail

PYTHON="$HOME/.venvs/second-brain/bin/python"
REPO_DIR="$HOME/SourceCode/plessas-second-brain"
LOG_DIR="$HOME/.second-brain/logs"
LOG="$LOG_DIR/health-check.log"

mkdir -p "$LOG_DIR"

# Portable PATH: pick up whatever fnm-managed node version is installed on this
# host (Mac and VPS pin different versions), plus Homebrew if present (Mac).
for _nodebin in "$HOME"/.local/share/fnm/node-versions/*/installation/bin; do
  [ -d "$_nodebin" ] && PATH="$_nodebin:$PATH"
done
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

echo "=== Health check started: $(ts) ===" >> "$LOG"

cd "$REPO_DIR" || exit 1
"$PYTHON" scripts/health_check.py --fix --email-if-issues >> "$LOG" 2>&1
EXIT_CODE=$?

echo "=== Health check finished (exit $EXIT_CODE): $(ts) ===" >> "$LOG"
echo "" >> "$LOG"
exit $EXIT_CODE
