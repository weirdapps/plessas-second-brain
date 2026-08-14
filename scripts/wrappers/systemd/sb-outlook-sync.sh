#!/bin/bash
# Hourly Outlook ingest — launchd surface for com.plessas.second-brain.sync.
#
# Lives at ~/.local/bin/ (legacy from the OneDrive era when launchd-spawned
# bash couldn't read CloudStorage paths via TCC). Source is now local at
# ~/SourceCode/plessas-second-brain so the constraint no longer applies.
#
# Notification logic lives here (not in scripts/outlook-sync.sh) so it fires
# even when the upstream sync script is unreachable.

set -uo pipefail

PROJECT="$HOME/SourceCode/plessas-second-brain"
PYTHON="$HOME/.venvs/second-brain/bin/python"
SENTINEL="$HOME/.second-brain/needs_reauth"
STATE="$HOME/.second-brain/outlook_sync_wrapper.json"
LOG_DIR="$HOME/.second-brain/logs"
LOG="$LOG_DIR/outlook-sync.log"
NOTIFY_THRESHOLD=3

mkdir -p "$LOG_DIR" "$(dirname "$STATE")"
# fnm 'default' alias (not a pinned version) so the path follows node upgrades and
# never goes stale — outlook-cli lives under the default node's bin, on Mac and VPS.
export PATH="$HOME/.local/share/fnm/aliases/default/bin:$PATH"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

read_failures() {
  [ -f "$STATE" ] || { echo 0; return; }
  /usr/bin/jq -r '.consecutive_failures // 0' "$STATE" 2>/dev/null || echo 0
}

write_state() {
  printf '{"consecutive_failures": %d, "last_run_at": "%s", "last_status": "%s"}\n' \
    "$1" "$(ts)" "$2" > "$STATE"
}

notify() {
  /usr/bin/osascript -e "display notification \"\" with title \"$1\" sound name \"Basso\"" 2>/dev/null || true
  command -v terminal-notifier >/dev/null 2>&1 && \
    terminal-notifier -title "$1" -message " " -sound Basso 2>/dev/null || true
}

# Self-heal: ensure auth is valid. auth-watch handles silent renewal when
# possible and only sets the sentinel when interactive login is truly required.
if [ -x "$HOME/.local/bin/sb-auth-watch.sh" ]; then
  "$HOME/.local/bin/sb-auth-watch.sh" >> "$LOG" 2>&1 || true
fi
if [ -f "$SENTINEL" ]; then
  echo "$(ts) — skip (reauth sentinel present after auth-watch)" >> "$LOG"
  exit 0
fi

cd "$PROJECT"

# Four passes — separate cursors per folder so none blocks the others:
#   1. Inbox   — fresh arrivals
#   2. Archive — direct-Archive arrivals + server-side rules
#   3. Sent Items — authoritative record of outbound mail. External replies sent
#      without self-CC never land in Inbox→Archive, so they were previously missed
#      entirely; Sent also carries the commitments the user makes.
#   4. Reconcile — list current Inbox via outlook-cli; any DB row still
#      labelled 'Inbox' but no longer there has been moved (assume → Archive,
#      matches /triage-inbox flow). Without this, mailbox_name goes stale
#      forever the moment the user triages.
overall_rc=0

echo "$(ts) — start folder=Inbox" >> "$LOG"
"$PYTHON" -m src.export.outlook_export \
  --mode hourly --concurrency 2 \
  --folder Inbox --state-path "$PROJECT/data/state/outlook_sync.json" >> "$LOG" 2>&1
rc1=$?
[ "$rc1" -ne 0 ] && overall_rc=$rc1

echo "$(ts) — start folder=Archive" >> "$LOG"
"$PYTHON" -m src.export.outlook_export \
  --mode hourly --concurrency 2 --bootstrap \
  --folder Archive --state-path "$PROJECT/data/state/outlook_sync_archive.json" >> "$LOG" 2>&1
rc2=$?
[ "$rc2" -ne 0 ] && overall_rc=$rc2

echo "$(ts) — start folder=Sent Items" >> "$LOG"
"$PYTHON" -m src.export.outlook_export \
  --mode hourly --concurrency 2 --bootstrap \
  --folder "Sent Items" --state-path "$PROJECT/data/state/outlook_sync_sent.json" >> "$LOG" 2>&1
rc_sent=$?
[ "$rc_sent" -ne 0 ] && overall_rc=$rc_sent

echo "$(ts) — start reconcile (Inbox→Archive moves)" >> "$LOG"
"$PYTHON" -m src.export.inbox_reconcile >> "$LOG" 2>&1
rc3=$?
[ "$rc3" -ne 0 ] && overall_rc=$rc3

# 4. Drain staged batches into the DB (extract + load) every hour.
#    The three passes above only *stage* bodies to data/staging; loading used to
#    happen only in the 07:00 daily + 12:00 noon batch runs, so mail arriving
#    after midday sat unloaded → "Emails STALE" by evening. --skip-export is the
#    catch-up path cmd_sync exposes (same command noon-catchup uses): no
#    re-export, just extract the staged bodies and load them. The load rc is
#    logged but deliberately NOT folded into overall_rc — export health (Outlook
#    reachable) drives the wrapper's failure notifications; load has the
#    daily/noon batch runs plus the 23:55 health-check backstop behind it.
# Guards:
#   - needs_gcloud_reauth: extraction needs Vertex ADC; skip until auth-watch clears it.
#   - pgrep: at 07:00/12:00 a daily/noon load is already running (and a long prior
#     hourly load could still be going) — defer rather than double-extract or
#     fight for the DB lock.
rc4=0
if [ -f "$HOME/.second-brain/needs_gcloud_reauth" ]; then
  echo "$(ts) — load skipped (needs_gcloud_reauth sentinel)" >> "$LOG"
elif pgrep -f "src\.cli sync" >/dev/null 2>&1; then
  echo "$(ts) — load skipped (another sync already running)" >> "$LOG"
else
  echo "$(ts) — start load (sync --skip-export)" >> "$LOG"
  "$PYTHON" -m src.cli sync --engine claude --workers 8 --skip-export >> "$LOG" 2>&1
  rc4=$?
  # Retry once on DB-lock contention (mirrors sb-daily-sync.sh).
  if [ "$rc4" -ne 0 ] && tail -20 "$LOG" | grep -qi "database is locked"; then
    echo "$(ts) — load retry after database-lock" >> "$LOG"
    sleep 10
    "$PYTHON" -c "import sqlite3,os; c=sqlite3.connect(os.path.expanduser('~/SourceCode/plessas-second-brain/data/brain.db')); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()" 2>/dev/null || true
    "$PYTHON" -m src.cli sync --engine claude --workers 8 --skip-export >> "$LOG" 2>&1
    rc4=$?
  fi
  echo "$(ts) — load done (rc=$rc4)" >> "$LOG"
fi

rc=$overall_rc

failures=$(read_failures)
if [ "$rc" -eq 0 ]; then
  write_state 0 ok
  echo "$(ts) — ok (Inbox + Archive + Sent + reconcile)" >> "$LOG"
else
  failures=$((failures + 1))
  write_state "$failures" "fail rc=$rc"
  echo "$(ts) — fail rc=$rc (inbox=$rc1 archive=$rc2 reconcile=$rc3) consecutive=$failures" >> "$LOG"
  if [ "$failures" -ge "$NOTIFY_THRESHOLD" ]; then
    notify "second-brain: $failures sync failures (rc=$rc)"
  fi
fi

exit "$rc"
