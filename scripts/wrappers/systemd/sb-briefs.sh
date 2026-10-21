#!/bin/bash
# Pre-meeting brief pilot, every 15 minutes (src/pipeline/briefs.py): a brief mailed to
# the owner, and to no one else, before each meeting 30 to 45 minutes ahead that has an
# outside attendee or one he has not met in 90 days. Started by sb-briefs.timer through
# its host-local shim, which sources the Vertex environment.
#
# The pilot's settings live in one owner-only file, sourced here:
#   BRAIN_BRIEF_PILOT_UNTIL=YYYY-MM-DD   the last day; set at install to install + 14 days
#   HEALTH_EMAIL_TO=...                  the owner's address, as the health check mails it
#   BRAIN_BRIEFS_OFF=1                   the kill switch
# Once the date has passed the command says "pilot ended", and this wrapper disables its
# own timer: the pilot stops by itself, and moving the date and enabling the timer again
# is the only way to extend it.

set -uo pipefail

REPO_DIR="$HOME/SourceCode/plessas-second-brain"
PYTHON="$HOME/.venvs/second-brain/bin/python"
LOG_DIR="$HOME/.second-brain/logs"
LOG_FILE="$LOG_DIR/briefs.log"
SETTINGS="$HOME/.config/second-brain/briefs.env"
mkdir -p "$LOG_DIR"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

if [ -f "$SETTINGS" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$SETTINGS"
  set +a
fi

# Whether the pilot sends today, as src/pipeline/briefs.py pilot_state decides it: no
# kill switch, and a date set and not passed (UTC, as the command reads it). When it
# does not, the command runs whatever the sessions, says why nothing is sent, and on
# "pilot ended" this wrapper stops the timer below.
pilot_sends_today() {
  case "${BRAIN_BRIEFS_OFF:-}" in "" | 0) ;; *) return 1 ;; esac
  case "${BRAIN_BRIEF_PILOT_UNTIL:-}" in
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ;;
    *) return 1 ;;
  esac
  [[ ! "$(date -u +%F)" > "$BRAIN_BRIEF_PILOT_UNTIL" ]]
}

# The brief is a model call, and the mail goes through outlook-cli: either session
# expired means nothing can be sent. Logged, so a skip is not a silence, and 69, not 0:
# on 2026-10-20 this exited 0 on 38 slots in a row, four meetings got no brief, and
# every slot pinged hc-success@ green. Only while the pilot sends, so a pilot that is
# off or over is never red for a brief it was not going to send.
if pilot_sends_today; then
  for sentinel in needs_gcloud_reauth needs_reauth; do
    if [ -f "$HOME/.second-brain/$sentinel" ]; then
      echo "$(ts) SKIP: $sentinel sentinel present" >> "$LOG_FILE"
      exit 69
    fi
  done
fi

cd "$REPO_DIR" || exit 1
out=$("$PYTHON" -m src.cli briefs 2>&1)
rc=$?
{
  echo "=== $(ts) (exit $rc) ==="
  printf '%s\n' "$out"
} >> "$LOG_FILE"

case "$out" in
  *"briefs: pilot ended on"*)
    echo "$(ts) the pilot is over: disabling sb-briefs.timer" >> "$LOG_FILE"
    systemctl --user disable --now sb-briefs.timer >> "$LOG_FILE" 2>&1 || true
    ;;
esac
exit "$rc"
