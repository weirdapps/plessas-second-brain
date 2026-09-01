#!/bin/bash
# Hourly Teams sync — launchd surface for com.plessas.second-brain.teams-sync.
#
# Lives at ~/.local/bin/sb-teams-sync.sh (sourced from
# scripts/launchd/wrappers/sb-teams-sync.sh, mirrored to ~/.local/bin via
# install.sh). Same shape as sb-outlook-sync.sh.

set -uo pipefail

# Env setup — sources $HOME files only; brings Vertex AI creds via .zprofile.
# Step 5 (thread extraction) needs ANTHROPIC_VERTEX_PROJECT_ID for Vertex AI
# Claude; without it, steps 1-4 (network IO) succeed but extraction fails.
[ -f "$HOME/.zprofile" ] && source "$HOME/.zprofile" 2>/dev/null || true

PROJECT="$HOME/SourceCode/plessas-second-brain"
PYTHON="$HOME/.venvs/second-brain/bin/python"
SENTINEL="$HOME/.second-brain/needs_teams_reauth"
STATE="$HOME/.second-brain/teams_sync_wrapper.json"
LOG_DIR="$HOME/.second-brain/logs"
LOG="$LOG_DIR/teams-sync.log"
NOTIFY_THRESHOLD=3

mkdir -p "$LOG_DIR" "$(dirname "$STATE")"

# fnm 'default' alias (not a pinned version) so teams-cli stays resolvable across node upgrades.
export PATH="$PATH:$HOME/.local/share/fnm/aliases/default/bin"

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
}

# Opt-out
if [ "${SB_TEAMS_SYNC_DISABLED:-0}" = "1" ]; then
  echo "$(ts) — opted out via SB_TEAMS_SYNC_DISABLED" >> "$LOG"
  exit 0
fi

# Pre-flight: skip if reauth needed
if [ -f "$SENTINEL" ]; then
  echo "$(ts) — skip (reauth sentinel present)" >> "$LOG"
  # exit 75 (EX_TEMPFAIL), not 0. Exiting 0 here made systemd record success,
  # fired OnSuccess=hc-success@, and pinged the dead-man's switch GREEN once an
  # hour while Teams ingest was dead. On 2026-09-01 that hid a 20-hour outage:
  # nine green pings, brain.db frozen at 03:33, and every downstream consumer
  # (search_teams, teams_chat_summary, meeting prep) silently serving stale data.
  # Same reasoning as the auth-renew branch below, which was fixed on 2026-08-10.
  # The sentinel is only cleared by an interactive `teams-cli login`, so the
  # correct signal is RED, not silence.
  exit 75
fi

# Pre-flight: skip if gcloud ADC expired (Vertex AI extraction would fail).
# auth-watch clears the sentinel on the next successful probe.
if [ -f "$HOME/.second-brain/needs_gcloud_reauth" ]; then
  echo "$(ts) — skip (gcloud reauth sentinel present)" >> "$LOG"
  exit 0
fi

# Pre-flight: confirm the Teams bearer is actually alive, and renew it if not.
#
# The sentinel checks above only catch the case where auth-watch has ALREADY
# noticed a dead token. They do not catch the common one: the bearer expires
# between auth-watch probes, this job starts, and teams-cli dies instantly with
# `exit 4 {"code":"auth_required"} Graph 401 ... token is expired`. That turned
# into a hard unit failure roughly once per few hours, tripping OnFailure and
# leaving a failed unit behind, even though auth-watch silently renewed minutes
# later and the next hourly run succeeded. Nothing was ever actually broken.
#
# So probe first and try to fix it ourselves. Use health-check, not auth-check:
# auth-check only validates the base Graph token, and has returned 0 while
# teams-sync 401'd for hours on chatsvcagg.
#
# If we cannot recover, exit 75 (EX_TEMPFAIL). This exited 0 until 2026-08-10,
# on the reasoning that a missed hourly sync is not an incident but a red unit
# that pages is. The retry drop-in (Restart=on-failure, RestartSec=90,
# StartLimitBurst=3) changed that trade-off: restarts now absorb blips, and only
# a sustained outage reaches OnFailure. Worse, exiting 0 here actively CONSUMED
# the retry budget. On 2026-08-09 the first attempt failed, systemd restarted
# once, and this branch then reported success, so the remaining attempts never
# ran and the VPS sat 401 for two hours looking healthy.
#
# NOTE ON SENTINEL OWNERSHIP: this block deliberately never writes $SENTINEL.
# sb-auth-watch owns that file and is the only thing that sets or clears it.
# A renew can fail transiently, for example when auth-watch is renewing at the
# same moment and the two contend over session.json, so latching the sentinel
# from here would block every later sync on a blip until auth-watch happened to
# clear it. This repo already has a history of sticky-latch reauth bugs. We
# just skip this run and let the owner of the state machine decide.
if command -v teams-cli >/dev/null 2>&1; then
  if ! teams-cli health-check >/dev/null 2>&1; then
    echo "$(ts) — teams auth looks dead, attempting silent renew" >> "$LOG"
    if teams-cli auth-renew >/dev/null 2>&1 && teams-cli health-check >/dev/null 2>&1; then
      echo "$(ts) — silent renew OK, continuing" >> "$LOG"
    else
      echo "$(ts) — fail (auth down and silent renew did not take; systemd will retry, or run: teams-cli login)" >> "$LOG"
      exit 75
    fi
  fi
fi

cd "$PROJECT" || { echo "$(ts) — abort (cannot cd to $PROJECT)" >> "$LOG"; exit 1; }
echo "$(ts) — start" >> "$LOG"
"$PYTHON" -m src.cli teams-sync --concurrency 2 --workers 4 >> "$LOG" 2>&1
rc=$?

failures=$(read_failures)
if [ "$rc" -eq 0 ]; then
  write_state 0 ok
  echo "$(ts) — ok" >> "$LOG"
else
  failures=$((failures + 1))
  write_state "$failures" "fail rc=$rc"
  echo "$(ts) — fail rc=$rc consecutive=$failures" >> "$LOG"
  if [ "$failures" -ge "$NOTIFY_THRESHOLD" ]; then
    notify "second-brain: $failures teams-sync failures (rc=$rc)"
  fi
fi

exit "$rc"
