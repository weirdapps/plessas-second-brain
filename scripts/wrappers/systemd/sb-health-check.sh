#!/bin/bash
# Nightly health check: launchd/systemd surface for the second-brain health-check job.
# Runs daily. Checks all components, auto-fixes where possible, emails a status report.
set -uo pipefail

PYTHON="$HOME/.venvs/second-brain/bin/python"
REPO_DIR="$HOME/SourceCode/plessas-second-brain"
LOG_DIR="$HOME/.second-brain/logs"
LOG="$LOG_DIR/health-check.log"
PING_ENV="$HOME/.config/healthchecks-ping.env"

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
# tee, so the report reaches both the log (as before) and the freshness parser
# below. PIPESTATUS[0] keeps EXIT_CODE exactly what `$?` used to be: the
# interpreter's status, not tee's.
REPORT="$("$PYTHON" scripts/health_check.py --fix --email-if-issues 2>&1 | tee -a "$LOG")"
EXIT_CODE=${PIPESTATUS[0]}

# --- sb-brain-freshness (defect D4) ------------------------------------------
# The check existed in Healthchecks since 2026-08-09 with exactly one ping in
# its life and no code anywhere that pinged it, so it sat red for 14 days and
# taught the eye to ignore red. Wire it here, keyed on the DATA SOURCES block of
# the report health_check.py just printed.
#
# STALE or FAIL only. WARN is deliberately excluded: it covers slow-moving
# backlog thresholds (LLM failures over 200, image queue over 500, disabled
# Teams chats) that are tracked elsewhere and would leave this check permanently
# red, which is the failure mode being fixed. SCHEDULED JOBS and JOB LOGS are
# also excluded: every one of those units already has its own Healthchecks check.
#
# Three-way on purpose. If the report is unparseable no ping is sent at all,
# because health_check.py crashing already fails this unit and fires
# hc-fail@sb-health-check; a second alert for the same incident is noise.
#
# EXIT_CODE is captured above and never touched here. This reports, it does not
# gate.
if [ -f "$PING_ENV" ]; then
  # shellcheck source=/dev/null
  . "$PING_ENV"
fi
if [ -n "${HC_PING_URL:-}" ]; then
  _fresh_block="$(printf '%s\n' "$REPORT" \
    | awk '/^DATA SOURCES/{b=1;next} /^SCHEDULED JOBS/{b=0} b')"
  if [ -z "$_fresh_block" ]; then
    echo "$(ts) sb-brain-freshness: no DATA SOURCES block in report, no ping sent" >> "$LOG"
  elif printf '%s\n' "$_fresh_block" | grep -qE '(^|[[:space:]])(STALE|FAIL)([[:space:]]|$)'; then
    echo "$(ts) sb-brain-freshness: STALE/FAIL present, sending fail ping" >> "$LOG"
    curl -fsS -m 10 --retry 2 -o /dev/null "${HC_PING_URL}/sb-brain-freshness/fail" 2>/dev/null || true
  else
    curl -fsS -m 10 --retry 2 -o /dev/null "${HC_PING_URL}/sb-brain-freshness" 2>/dev/null || true
  fi
fi
# -----------------------------------------------------------------------------

echo "=== Health check finished (exit $EXIT_CODE): $(ts) ===" >> "$LOG"
echo "" >> "$LOG"
exit $EXIT_CODE
