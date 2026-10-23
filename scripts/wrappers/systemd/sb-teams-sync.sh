#!/bin/bash
# Hourly Teams sync — launchd surface for com.plessas.second-brain.teams-sync.
#
# Lives at ~/.local/bin/sb-teams-sync.sh (sourced from
# scripts/launchd/wrappers/sb-teams-sync.sh, mirrored to ~/.local/bin via
# install.sh). Same shape as sb-outlook-sync.sh.

set -uo pipefail

# Env setup — sources $HOME files only; brings Vertex AI creds via .zprofile.
# Step 5 (thread extraction) needs VERTEX_SDK_PROJECT (or
# ANTHROPIC_VERTEX_PROJECT_ID) for Vertex AI
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
  # Non-zero, not 0. Exiting 0 here made systemd record success, fired
  # OnSuccess=hc-success@, and pinged the dead-man's switch GREEN once an hour
  # while Teams ingest was dead. On 2026-09-01 that hid a 20-hour outage: nine
  # green pings, brain.db frozen at 03:33, and every downstream consumer
  # (search_teams, teams_chat_summary, meeting prep) silently serving stale data.
  # Same reasoning as the auth-renew branch below, which was fixed on 2026-08-10.
  # The correct signal is RED, not silence.
  #
  # 69 (EX_UNAVAILABLE) rather than 75 (EX_TEMPFAIL), and the distinction is the
  # retry budget. retry.conf carries Restart=on-failure with StartLimitBurst=3
  # in a 1800s window, sized on the assumption that an attempt costs
  # "timeout + 90s". THIS branch costs under a second, so three of them burn the
  # entire budget in 92 seconds, and the next start systemd is asked for is
  # refused outright with "Start request repeated too quickly".
  #
  # Observed 2026-09-18. The sentinel skips at 22:32:03 and 22:33:33 spent the
  # budget; sb-auth-watch's health-check passed at 22:33:44, cleared the
  # sentinel, and its start at 22:33:45 was rejected. The unit then sat failed
  # for an hour holding a token that was valid the whole time.
  #
  # Retrying is pointless here anyway, which is what makes 69 honest rather than
  # merely convenient: the sentinel is owned by sb-auth-watch and cleared only
  # when ITS probe next succeeds, and that timer is 4-hourly. Nothing a restart
  # 90 seconds from now can do will change this branch's answer.
  #
  # RestartPreventExitStatus=69 in retry.conf is the other half. The unit still
  # goes failed and still fires OnFailure, so this stays RED; what it stops is
  # the pointless restart that eats the budget a real transient failure needs.
  exit 69
fi

# Pre-flight: gcloud ADC expired, so Vertex AI extraction would fail; auth-watch
# clears the sentinel on the next successful probe. The pull does not need it, and
# it is the step that cannot wait: each read takes a chat's newest page only, so an
# outage long enough loses messages. This used to skip the whole run and exit 0. Now
# the run pulls, bounds, resolves and embeds as usual and tries one thread
# (--limit 1), which defers to 'pending' on the dead credential instead of every
# pending thread failing in turn, and the unit ends 69 for the extraction it owes.
limit_args=""
if [ -f "$HOME/.second-brain/needs_gcloud_reauth" ]; then
  echo "$(ts) gcloud reauth sentinel present: pulling, extraction owed" >> "$LOG"
  limit_args="--limit 1"
fi

# Each teams-cli call below is bounded. A renew drives a headless Chrome: its launch
# can wait three minutes before giving up, then 90 s of login and 40 s more, and all
# of it came out of this unit's 600 s before the sync had started. In 139 renews on
# the VPS to 2026-10-20 none took over 92 s (and one succeeded), so the bound only
# caps the case not seen yet. -k because a wedged Chrome may ignore SIGTERM. A call
# cut off reads as a failed call, so the exit codes below keep their meaning.
# (The audit's "6m38s per attempt" was wall clock including RestartSec=300; each
# attempt itself took 99 s.)
PROBE_TIMEOUT_S="${SB_TEAMS_PROBE_TIMEOUT_S:-60}"
RENEW_TIMEOUT_S="${SB_TEAMS_RENEW_TIMEOUT_S:-120}"

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
#
# AN EXPIRED TOKEN ON LINUX WAITS FOR THE LAPTOP (2026-10-23). The renew below has
# never worked on the producer (1 success in 347), because silent SSO needs device
# trust it cannot hold; only the laptop's token push renews Teams there. With the
# required audiences already past their exp, the renew and the four retries 75 buys
# could only fail: on 2026-10-23 the laptop slept from 17:27 to 19:43 and this unit
# spent seven attempts of about 100 s and 850 MB each on a token that had expired.
# So that case exits 69 (no retry, see retry.conf) and the push restarts the run:
# a credential arriving starts sb-auth-watch (sb-creds-arrived.path), whose probe
# then finds this unit failed and owed. A token that has not expired, a file that
# cannot be read, or a Mac keeps the renew and the 75 retry, for a 401 or a 429
# that clears by itself.
TEAMS_TOKENS="${SB_TEAMS_TOKENS:-$HOME/.teams-cli/multi-tokens.json}"
TEAMS_SYNC_PLATFORM="${TEAMS_SYNC_PLATFORM:-$(uname -s)}"

# Seconds left in the shortest-lived audience teams-cli requires (its
# REQUIRED_AUDIENCES), negative once expired; prints nothing when unreadable.
teams_token_ttl() {
  /usr/bin/jq -r --argjson now "$(date +%s)" \
    '[.["https://graph.microsoft.com"].exp, .["https://chatsvcagg.teams.microsoft.com"].exp]
     | if all(type == "number") then (min - $now | floor) else empty end' \
    "$TEAMS_TOKENS" 2>/dev/null
}

if command -v teams-cli >/dev/null 2>&1; then
  if ! timeout -k 10 "$PROBE_TIMEOUT_S" teams-cli health-check >/dev/null 2>&1; then
    ttl=$(teams_token_ttl)
    if [ "$TEAMS_SYNC_PLATFORM" = Linux ] && [ -n "$ttl" ] && [ "$ttl" -le 0 ]; then
      echo "$(ts): skip (teams token expired $(( -ttl ))s ago; no renew on Linux, waiting for the laptop's push)" >> "$LOG"
      exit 69
    fi
    echo "$(ts) — teams auth looks dead, attempting silent renew" >> "$LOG"
    if timeout -k 10 "$RENEW_TIMEOUT_S" teams-cli auth-renew >/dev/null 2>&1 \
       && timeout -k 10 "$PROBE_TIMEOUT_S" teams-cli health-check >/dev/null 2>&1; then
      echo "$(ts) — silent renew OK, continuing" >> "$LOG"
    else
      echo "$(ts) — fail (auth down and silent renew did not take; systemd will retry, or run: teams-cli login)" >> "$LOG"
      exit 75
    fi
  fi
fi

cd "$PROJECT" || { echo "$(ts) — abort (cannot cd to $PROJECT)" >> "$LOG"; exit 1; }
echo "$(ts) — start" >> "$LOG"
# shellcheck disable=SC2086  # empty, or the one option and its value
"$PYTHON" -m src.cli teams-sync --concurrency 2 --workers 4 $limit_args >> "$LOG" 2>&1
rc=$?

# The organisation's directory (title, department, manager) through the same Teams
# session. Only after a good sync, whose auth it shares; only while the unit has time
# left (TimeoutStartSec is 600 s, the sync takes about 190, and SECONDS counts from this
# script's start), cut off well before the timeout; and never fatal: each chunk is
# stored as it comes back, and what was not asked stays due for the next run.
if [ "$rc" -eq 0 ] && [ "$SECONDS" -lt 420 ]; then
  timeout $((540 - SECONDS)) "$PYTHON" -m src.cli directory-sync --limit 400 >> "$LOG" 2>&1
  directory_rc=$?
  if [ "$directory_rc" -ne 0 ]; then
    echo "$(ts): directory-sync failed rc=$directory_rc (not fatal)" >> "$LOG"
  fi
fi

# Pulled under a dead gcloud credential: the extraction is owed (see above).
if [ "$rc" -eq 0 ] && [ -n "$limit_args" ]; then
  rc=69
fi

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
