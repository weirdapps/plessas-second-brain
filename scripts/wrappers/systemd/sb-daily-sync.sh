#!/bin/bash
# Daily second-brain sync — launchd surface for com.plessas.second-brain-sync.
#
# Lives at ~/.local/bin/ (kept here from the OneDrive era when launchd-bash
# couldn't read CloudStorage paths via TCC). Source is now local at
# ~/SourceCode/plessas-second-brain so the constraint no longer applies, but the
# wrapper location is unchanged to avoid touching the launchd plist.

set -uo pipefail

# Env setup (sources only $HOME files; brew/pyenv on PATH for python -m subprocess use).
[ -f "$HOME/.zprofile" ] && source "$HOME/.zprofile" 2>/dev/null || true
eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null || true
export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$HOME/.local/bin:$HOME/.bun/bin:$PATH"
eval "$(pyenv init --path)" 2>/dev/null || true
eval "$(pyenv init -)" 2>/dev/null || true

REPO_DIR="$HOME/SourceCode/plessas-second-brain"
PYTHON="$HOME/.venvs/second-brain/bin/python"
LOG_DIR="$HOME/.second-brain/logs"
LOG_FILE="$LOG_DIR/daily-sync.log"
mkdir -p "$LOG_DIR"

# --- Concurrency guard: a single run can take 30+ min when staging has backlog.
# Without this, auth-watch's restoration trigger can overlap with the 07:00 cron
# (or a manual run), doubling Vertex AI spend. Stale-lock recovery via PID check.
LOCK_DIR="/tmp/sb-daily-sync.lock"
if [ -d "$LOCK_DIR" ]; then
  stored_pid=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")
  if [ -z "$stored_pid" ] || ! kill -0 "$stored_pid" 2>/dev/null; then
    rm -rf "$LOCK_DIR"  # stale (previous run crashed without trap cleanup)
  else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] SKIP: already running (pid=$stored_pid)" >> "$LOG_FILE"
    exit 0
  fi
fi
mkdir -p "$LOCK_DIR" && echo $$ > "$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT INT TERM

# Skip extraction phase if gcloud ADC expired — the email loop would otherwise
# burn CONSECUTIVE_FAIL_THRESHOLD calls and enter a 1h QUOTA PAUSE for an issue
# that is not quota. auth-watch clears the sentinel on its next successful probe.
if [ -f "$HOME/.second-brain/needs_gcloud_reauth" ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] SKIP: needs_gcloud_reauth sentinel present" >> "$LOG_FILE"
  exit 0
fi

echo "=== Daily sync started: $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"

# WAL checkpoint: flush pending WAL pages before starting, to reduce lock
# contention with concurrent hourly-sync processes.
"$PYTHON" -c "
import sqlite3, os
db = os.path.expanduser('~/SourceCode/plessas-second-brain/data/brain.db')
if os.path.exists(db):
    c = sqlite3.connect(db)
    c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    c.close()
" >> "$LOG_FILE" 2>&1 || true

# MVCC-safe local snapshot (sqlite .backup) + key-gated encrypted offsite copy.
# Replaces the old shutil.copy2, which raw-copied a live WAL DB and could capture a
# torn/unopenable snapshot. The offsite copy is produced only where the backup key
# exists (the authoritative VPS), so the Mac replica skips it automatically.
#
# The step stays non-fatal on purpose: this script has no `set -e`, and making the
# backup fatal would let a transient snapshot failure abort the mail and Teams
# ingest that follows. But non-fatal used to mean invisible, because the `||` below
# returns 0 and hc-success@sb-daily-sync then fired whether or not a snapshot was
# taken. --hc-slug fixes that without changing the failure semantics: the backup
# now reports on its own check and the sync keeps its own verdict.
#
# HC_PING_URL is a secret, sourced from the same env file the other wrappers use.
# Sourced here rather than added to the unit because external/vps/systemd is a
# backup copy that does not deploy, so a unit edit would not reach the VPS.
PING_ENV="$HOME/.config/healthchecks-ping.env"
# shellcheck source=/dev/null
[ -f "$PING_ENV" ] && . "$PING_ENV"
export HC_PING_URL="${HC_PING_URL:-}"

"$PYTHON" "$REPO_DIR/scripts/backup_db.py" \
  --db "$REPO_DIR/data/brain.db" \
  --local-dir "$REPO_DIR/data/backups" --local-keep 7 \
  --offsite-dir "$REPO_DIR/data/backups/offsite" \
  --key-file "$HOME/.second-brain/backup.key" \
  --gfs-daily 14 --gfs-weekly 8 \
  --hc-slug brain-backup \
  >> "$LOG_FILE" 2>&1 \
  || echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARN: backup step failed (non-fatal)" >> "$LOG_FILE"

# Engine selection
if [ -n "${ANTHROPIC_API_KEY:-}" ] || [ -n "${ANTHROPIC_VERTEX_PROJECT_ID:-}" ]; then
  ENGINE="claude"
else
  ENGINE="${BRAIN_EXTRACT_ENGINE:-gemini}"
  echo "Warning: no Anthropic credentials, using $ENGINE" >> "$LOG_FILE"
fi

cd "$REPO_DIR"
"$PYTHON" -m src.cli sync --engine "$ENGINE" --workers 8 >> "$LOG_FILE" 2>&1
EXIT_CODE=$?
# Retry once on database-lock failures (exit=1 + "locked" in recent log lines)
if [ "$EXIT_CODE" -ne 0 ] && tail -20 "$LOG_FILE" | grep -qi "database is locked"; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Retrying sync after database-lock failure..." >> "$LOG_FILE"
  sleep 10
  "$PYTHON" -c "import sqlite3,os; c=sqlite3.connect(os.path.expanduser('~/SourceCode/plessas-second-brain/data/brain.db')); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()" 2>/dev/null || true
  "$PYTHON" -m src.cli sync --engine "$ENGINE" --workers 8 >> "$LOG_FILE" 2>&1
  EXIT_CODE=$?
fi
echo "=== Daily sync finished (exit $EXIT_CODE): $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"

# Style guide sync
STYLE_SYNC="$HOME/SourceCode/plessas-marketplace/plugins/mail/scripts/style-sync.py"
if [ "$EXIT_CODE" -eq 0 ] && "$PYTHON" -c "import os,sys; sys.exit(0 if os.path.isfile('$STYLE_SYNC') else 1)"; then
  echo "=== Style guide sync started: $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"
  "$PYTHON" "$STYLE_SYNC" --quiet >> "$LOG_FILE" 2>&1
  STYLE_EXIT=$?
  echo "=== Style guide sync finished (exit $STYLE_EXIT): $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"
fi

# Staleness check via python sqlite (kept from OneDrive era — shell sqlite3 now works fine).
STALE_REPORT=$("$PYTHON" - <<'PYEOF' 2>/dev/null
import sqlite3, os
db = os.path.expanduser('~/SourceCode/plessas-second-brain/data/brain.db')
conn = sqlite3.connect(db)
rows = conn.execute("""
SELECT mailbox_name || ': ' || MAX(date_received) || ' (' ||
       printf('%.1f', julianday('now') - julianday(MAX(date_received))) || ' days stale)'
FROM emails
WHERE mailbox_name IN ('Inbox', 'Archive')
GROUP BY mailbox_name
HAVING julianday('now') - julianday(MAX(date_received)) > 3
""").fetchall()
print('\n'.join(r[0] for r in rows))
PYEOF
)
if [ -n "$STALE_REPORT" ]; then
  echo "=== STALENESS WARNING ===" >> "$LOG_FILE"
  echo "$STALE_REPORT" >> "$LOG_FILE"
  osascript -e "display notification \"$(echo "$STALE_REPORT" | tr '\n' '; ')\" with title \"Second Brain: stale mailbox\" sound name \"Basso\"" 2>/dev/null || true
fi

echo "" >> "$LOG_FILE"
exit $EXIT_CODE
