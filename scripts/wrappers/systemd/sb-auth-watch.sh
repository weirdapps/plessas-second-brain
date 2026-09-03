#!/bin/bash
# Auth watchdog — launchd surface for com.plessas.second-brain.auth-watch.
#
# Probes outlook-cli AND teams-cli. Each probe is independent: a failure in one
# doesn't stop the other. The script always exits 0; per-probe failures are
# surfaced via sentinel files + macOS notifications, which the corresponding
# sync wrappers consult to decide whether to skip their next run.
#
# Outlook flow:
#   1. Check current bearer via `outlook-cli auth-check`
#   2. If still healthy (>= MIN_HOURS_BEFORE_NOTIFY remaining): clear sentinel
#   3. If expiring soon OR auth-check failed: try `outlook-cli auth-renew`
#      first (silent headless via persistent profile + ESTSAUTHPERSISTENT
#      cookie). Only notify + touch sentinel if renew also fails.
#
# Teams flow:
#   1. Check current bearer via `teams-cli auth-check --no-auto-reauth`
#   2. Exit 0 → clear sentinel
#   3. Exit 4 (AuthRequired): try `teams-cli auth-renew` first (silent
#      headless: drives a persistent-profile browser through teams →
#      outlook → office.com to provoke Graph + chatsvcagg + ic3 captures).
#      Only notify + touch sentinel if renew also fails.
#   4. teams-cli not installed → no-op (Phase 1 deployment safety)
#
# Silent-renew architecture: both clis expose `auth-renew` (outlook since
# 1.5.0+c79c1e1, teams since post-Phase-1). Headless Chromium with the
# persisted profile lets Microsoft Entra silently re-issue Bearer tokens
# while the device-trust cookie ESTSAUTHPERSISTENT is alive (~90 days).
# When that cookie expires, only THEN is interactive login needed.
#
# Lives at ~/.local/bin/ (legacy from OneDrive/TCC era; constraint no longer applies post-migration).

# NOTE: deliberately NOT using `set -e` because we want both probes to run
# even if the first one fails. We do use -u and pipefail.
set -uo pipefail

export PATH="$HOME/.local/share/fnm/aliases/default/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

SENTINEL="$HOME/.second-brain/needs_reauth"
TEAMS_SENTINEL="$HOME/.second-brain/needs_teams_reauth"
GCLOUD_SENTINEL="$HOME/.second-brain/needs_gcloud_reauth"
LOG_DIR="$HOME/.second-brain/logs"
LOG="$LOG_DIR/auth-watch.log"
TRIGGERED_LOG="$LOG_DIR/auth-triggered.log"
MIN_HOURS_BEFORE_NOTIFY=1   # notify + sentinel when <1h to expiry
GCLOUD_AUTO_LOGIN="$HOME/scripts/gcloud-auto-login.sh"
WRAPPER_DIR="$HOME/.local/bin"

mkdir -p "$LOG_DIR" "$(dirname "$SENTINEL")"

# --- Snapshot sentinel state BEFORE probes run.
# When a probe successfully renews auth it clears its sentinel; comparing this
# snapshot to the post-probe state tells us which sentinels were just cleared
# so we can re-trigger the wrappers that skipped during the outage window.
# Without this, a sentinel cleared at 10:00 doesn't unblock daily-sync (07:00
# cron) until tomorrow morning — we lose ~24h of extraction.
GCLOUD_WAS_BLOCKED=0
OUTLOOK_WAS_BLOCKED=0
TEAMS_WAS_BLOCKED=0
[ -f "$GCLOUD_SENTINEL" ] && GCLOUD_WAS_BLOCKED=1
[ -f "$SENTINEL" ]        && OUTLOOK_WAS_BLOCKED=1
[ -f "$TEAMS_SENTINEL" ]  && TEAMS_WAS_BLOCKED=1

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "$(ts) — $*" >> "$LOG"; }

# Serialise runs. This script is invoked from TWO places: sb-auth-watch.service
# (its own timer, 00/04/08/12/16/20) and sb-outlook-sync.sh's pre-flight
# (hourly, on the hour). They collide exactly at those six ticks. Two concurrent
# teams-cli renews fight over the same persistent Playwright profile and both
# can lose: on 2026-08-10 00:00 the headed renew died with "no Bearer captured,
# audiences seen: (none)", then latched needs_teams_reauth.
# Non-blocking on purpose: the loser skips rather than delaying outlook-sync.
# macOS has no flock, so the guard is a no-op there and behaviour is unchanged.
LOCK="$HOME/.second-brain/.auth-watch.lock"
exec 9>"$LOCK"
if command -v flock >/dev/null 2>&1 && ! flock -n 9; then
  log "another auth-watch run holds the lock; skipping this invocation"
  exit 0
fi

# --- Findings channel -------------------------------------------------------
# The notify_* helpers below are osascript, i.e. a no-op on Linux. On the VPS a
# dead token therefore had NO route to a human: this script exits 0 by design
# (correctly: its job is probing, and it probed fine), the sentinel only gates
# other jobs, and the email path belongs to a different job entirely. On
# 2026-08-09 that cost two hours of silent Graph 401s.
#
# Ping a per-surface Healthchecks check instead, which gives dedup, auto-resolve
# and escalation for free. ?create=1 provisions the check on first ping, so
# there is nothing to register by hand.
#
# These are deliberately SEPARATE from the unit's own hc-success/hc-fail, which
# answer "did the watchdog run", not "is auth healthy". Never conflate the two:
# a watchdog that reports its findings through its own exit status can no longer
# distinguish "I am broken" from "I found something broken".
[ -r "$HOME/.config/healthchecks-ping.env" ] && . "$HOME/.config/healthchecks-ping.env"
hc_report() {  # hc_report <slug> <ok|fail>
  [ -n "${HC_PING_URL:-}" ] || return 0
  local suffix=""
  [ "$2" = "fail" ] && suffix="/fail"
  curl -fsS -m 10 --retry 2 -o /dev/null "${HC_PING_URL}/$1${suffix}?create=1" 2>/dev/null || true
  return 0
}

notify_interactive_required() {
  local detail="$1"
  /usr/bin/osascript -e "display notification \"$detail\" with title \"second-brain auth: run outlook-cli login\"" 2>/dev/null || true
  command -v terminal-notifier > /dev/null && \
    terminal-notifier -title "second-brain auth" -message "Run: outlook-cli login (${detail})" -sound Glass 2>/dev/null || true
}

notify_teams_reauth() {
  /usr/bin/osascript -e \
    'display notification "Run: teams-cli login" with title "second-brain: teams reauth needed" sound name "Basso"' \
    2>/dev/null || true
  command -v terminal-notifier > /dev/null && \
    terminal-notifier -title "second-brain teams auth" -message "Run: teams-cli login" -sound Basso 2>/dev/null || true
}

notify_gcloud_reauth() {
  /usr/bin/osascript -e \
    'display notification "Vertex AI extraction will fail. Run: gcloud auth application-default login" with title "second-brain: gcloud ADC reauth needed" sound name "Basso"' \
    2>/dev/null || true
  command -v terminal-notifier > /dev/null && \
    terminal-notifier -title "second-brain gcloud auth" -message "Run: gcloud auth application-default login" -sound Basso 2>/dev/null || true
}

# --- Silent renew helper (returns 0 on success, non-zero on failure) ---
try_silent_renew_outlook() {
  log "outlook: attempting silent auth-renew (headless via persistent profile)"
  # Capture stdout only — stderr from auth-renew is noisy diagnostic prints
  # (bearer-seen, navigated to ..., etc.) that confuse jq downstream.
  local renew_stdout
  if renew_stdout=$(outlook-cli auth-renew 2>/dev/null); then
    log "outlook: silent renew OK — $(echo "$renew_stdout" | jq -r '"expires=" + .tokenExpiresAt + " durationMs=" + (.durationMs | tostring)' 2>/dev/null || echo 'ok-but-output-unparseable')"
    return 0
  else
    log "outlook: silent renew failed (stdout: $(echo "$renew_stdout" | head -c 300))"
    return 1
  fi
}

try_silent_renew_teams() {
  log "teams: attempting silent auth-renew (headless via persistent profile)"
  local renew_stdout
  if renew_stdout=$(teams-cli auth-renew 2>/dev/null); then
    log "teams: silent renew OK — $(echo "$renew_stdout" | jq -r '"audiencesCaptured=" + (.audiencesCaptured | tostring) + " durationMs=" + (.durationMs | tostring)' 2>/dev/null || echo 'ok-but-output-unparseable')"
    return 0
  else
    log "teams: silent renew failed (stdout: $(echo "$renew_stdout" | head -c 300))"
    return 1
  fi
}

# --- Outlook auth probe ---
auth_check_outlook() {
  local result expires_at expires_epoch now_epoch hours_remaining seconds_remaining

  if ! result=$(outlook-cli auth-check --json --no-auto-reauth 2>&1); then
    log "outlook auth-check failed: $result"
    # Try silent renew first; fall back to user notification only if it fails.
    if try_silent_renew_outlook; then
      rm -f "$SENTINEL"
      return 0
    fi
    notify_interactive_required "auth-check failed AND silent renew failed"
    touch "$SENTINEL"
    return 1
  fi

  expires_at=$(echo "$result" | jq -r '.tokenExpiresAt // empty')
  if [ -z "$expires_at" ]; then
    log "outlook auth-check returned no tokenExpiresAt: $result"
    notify_interactive_required "no token expiry returned"
    touch "$SENTINEL"
    return 1
  fi

  # `date -j -f` doesn't parse millisecond precision, so use python — outlook-cli
  # emits expiry as "2026-04-23T03:31:14.000Z" (millis included).
  expires_epoch=$(/usr/bin/python3 -c "
import sys, datetime
print(int(datetime.datetime.fromisoformat(sys.argv[1].replace('Z','+00:00')).timestamp()))
" "$expires_at" 2>/dev/null || echo 0)
  now_epoch=$(date "+%s")
  hours_remaining=$(( (expires_epoch - now_epoch) / 3600 ))
  seconds_remaining=$(( expires_epoch - now_epoch ))

  log "outlook bearer expires $expires_at (${hours_remaining}h / ${seconds_remaining}s remaining)"

  if [ "$hours_remaining" -ge "$MIN_HOURS_BEFORE_NOTIFY" ]; then
    log "outlook bearer still healthy (≥${MIN_HOURS_BEFORE_NOTIFY}h); no action"
    rm -f "$SENTINEL"
    return 0
  fi

  log "outlook bearer expiring within ${MIN_HOURS_BEFORE_NOTIFY}h — attempting silent renew"
  if try_silent_renew_outlook; then
    rm -f "$SENTINEL"
    return 0
  fi
  log "outlook silent renew failed — interactive login required"
  notify_interactive_required "bearer expires in ${hours_remaining}h, silent renew failed"

  # Do NOT latch while the bearer still works. auth-check passed at the top of
  # this function, so the session is usable for another $seconds_remaining, and
  # this sentinel gates mail, calendar, attachments and curation all at once.
  # Latching here treats "a renewal I did not need yet did not work" as an
  # outage. Measured 2026-09-03 16:04: renewal failed with 2468s (41 minutes) of
  # valid token left, the sentinel went down, and because only an interactive
  # login clears it, the whole pipeline stayed blocked long past the point where
  # the token would have carried several more syncs. Notify, but keep working:
  # the run after expiry takes the auth-check-failed branch above and latches
  # then, on evidence rather than on a forecast.
  if [ "$seconds_remaining" -gt 0 ]; then
    log "outlook bearer still valid for ${seconds_remaining}s; not latching the sentinel yet"
    return 1
  fi

  touch "$SENTINEL"
  return 1
}

# --- Teams auth probe (added in teams ingest Phase 1) ---
# Mirrors the outlook probe shape: degraded/AuthRequired → sentinel + notify.
# teams-cli not installed → silent no-op (the `command -v` guard makes this
# safe to ship before the CLI lands on every machine).
#
# Probe choice: `health-check` (NOT `auth-check`). auth-check only verifies the
# base Graph token; teams-sync also needs chatsvcagg.teams.microsoft.com which
# expires/disappears independently. Past incident (2026-05-02): auth-check was
# returning 0 every hour while teams-sync 401'd 11 hours straight on chatsvcagg.
# health-check probes Graph + chatsvc + chatsvcagg and exits non-zero when
# overall != "ok".
auth_check_teams() {
  if ! command -v teams-cli >/dev/null 2>&1; then
    log "teams-cli not on PATH; skipping teams probe"
    return 0
  fi

  # Run the probe first, THEN capture rc.
  local hc_out
  hc_out=$(teams-cli health-check 2>/dev/null)
  local rc=$?

  if [ "$rc" -eq 0 ]; then
    log "teams-cli health-check ok (all audiences accepted)"
    rm -f "$TEAMS_SENTINEL"
    return 0
  fi

  # Anything non-zero = degraded. Extract which audiences failed for the log.
  local failing
  failing=$(echo "$hc_out" | jq -r '[.probes[] | select(.ok == false) | .name] | join(",")' 2>/dev/null || echo "unknown")
  log "teams-cli health-check rc=$rc, failing probes: $failing — attempting silent renew"

  if try_silent_renew_teams; then
    # Renew said it captured everything — verify with health-check before
    # declaring victory. Strict auth-renew should already guarantee this, but
    # belt-and-braces: if the next health-check still fails, the renew lied.
    if teams-cli health-check >/dev/null 2>&1; then
      log "teams: silent renew + post-renew health-check ok"
      rm -f "$TEAMS_SENTINEL"
      return 0
    fi
    log "teams: renew claimed ok but post-renew health-check still failing — interactive login required"
    notify_teams_reauth
    touch "$TEAMS_SENTINEL"
    return 1
  fi

  # Re-probe before latching. The condition that got us here can clear on its
  # own: on 2026-09-03 16:05 this latched on `failing probes: graph_me` alone,
  # a single transient probe, while chatsvcagg (the audience that actually
  # carries messages) was healthy, and health-check returned ok again minutes
  # later. The renew attempt itself takes ~90s, which is long enough for a blip
  # to pass, so the state this decides on must be re-read rather than assumed
  # from before the renew. The success branch above already re-probes for the
  # opposite reason; this is the same distrust applied symmetrically.
  if teams-cli health-check >/dev/null 2>&1; then
    log "teams: renew failed but health-check now passes; transient, not latching"
    rm -f "$TEAMS_SENTINEL"
    return 0
  fi

  log "teams silent renew failed — interactive login required"
  notify_teams_reauth
  touch "$TEAMS_SENTINEL"
  return 1
}

# --- Vertex AI / gcloud ADC probe ---
# Email + conversation extraction goes through Vertex AI (Gemini for embeddings,
# Anthropic Claude on Vertex for extraction). Both use Application Default
# Credentials. ADC's refresh token is independent of the user-auth refresh
# token — when it expires (security policy, ~weeks), every Vertex AI call
# fails with "RefreshError: Reauthentication is needed" and the extraction
# circuit breaker trips into 1h "QUOTA PAUSE" loops without surfacing why.
#
# Past incident (2026-05-03): 14h of silent extraction failures — sync ran
# hourly, fetched mail successfully, then extraction failed all 20+ messages
# with RefreshError. auth-watch didn't probe gcloud, so the sentinel never
# fired and no notification surfaced.
#
# Strategy mirrors the UserPromptSubmit hook in ~/.claude/hooks.json: probe
# fast (single CLI call), refresh in background if expired (delegating to
# gcloud-auto-login.sh which already handles the headless+browser flow for
# both user auth and ADC). Don't block: gcloud-auto-login.sh has its own
# 180s timeout and may need to drive a Chrome tab.
auth_check_gcloud_adc() {
  # Resolve gcloud across macOS (Homebrew) and the Linux VPS (SDK install).
  # Hardcoding /opt/homebrew/bin/gcloud made this probe ALWAYS fail on the VPS
  # (no Homebrew there) — so auth-watch could only ever `touch` the gcloud
  # sentinel, never clear it. Since sb-outlook-sync.sh calls this as a per-run
  # self-heal, every hourly run planted a false needs_gcloud_reauth that then
  # blocked its own load step. Try known install locations, then fall back to
  # PATH (mirrors health_check.py's launchctl_bin/systemctl_bin resolution).
  local c gcloud_bin=""
  for c in /opt/homebrew/bin/gcloud "$HOME/google-cloud-sdk/bin/gcloud" /usr/local/bin/gcloud /usr/bin/gcloud; do
    [ -x "$c" ] && { gcloud_bin="$c"; break; }
  done
  [ -z "$gcloud_bin" ] && gcloud_bin="$(command -v gcloud 2>/dev/null || echo gcloud)"

  if "$gcloud_bin" auth application-default print-access-token &>/dev/null 2>&1; then
    log "gcloud ADC ok"
    rm -f "$GCLOUD_SENTINEL"
    return 0
  fi

  log "gcloud ADC expired (or refresh token revoked) — Vertex AI extraction will fail"
  touch "$GCLOUD_SENTINEL"
  notify_gcloud_reauth

  if [ -x "$GCLOUD_AUTO_LOGIN" ]; then
    log "launching gcloud-auto-login.sh in background (timeout=180s, may drive Chrome)"
    nohup "$GCLOUD_AUTO_LOGIN" >> "$LOG_DIR/gcloud-auto-login.log" 2>&1 &
    return 1
  fi

  log "gcloud-auto-login.sh not executable at $GCLOUD_AUTO_LOGIN — manual gcloud auth application-default login required"
  return 1
}

auth_check_outlook || true
auth_check_teams || true
auth_check_gcloud_adc || true

# Teams token watchdog: alerts when chatsvcagg is close to expiry. Carried over
# from ~/scripts/auth-watch.sh on 2026-08-10 when this script replaced it as the
# scheduled probe. Without this line the repoint would have silently dropped the
# alert, since that was the only caller.
if [ -x "$HOME/scripts/teams-token-watchdog.sh" ]; then
  "$HOME/scripts/teams-token-watchdog.sh" >/dev/null 2>&1 || true
fi

# Report each surface. Sentinel present == that surface needs a human.
if [ -f "$SENTINEL" ];        then hc_report sb-auth-outlook fail; else hc_report sb-auth-outlook ok; fi
if [ -f "$TEAMS_SENTINEL" ];  then hc_report sb-auth-teams   fail; else hc_report sb-auth-teams   ok; fi
if [ -f "$GCLOUD_SENTINEL" ]; then hc_report sb-auth-gcloud  fail; else hc_report sb-auth-gcloud  ok; fi

# --- Re-trigger wrappers whose sentinels were just cleared.
# Each wrapper has its own sentinel guards at the top, so dispatching one whose
# OTHER blocker is still active is safe — it'll log SKIP and exit 0.
trigger_job() {
  local script="$1"
  local reason="$2"
  if [ ! -x "$WRAPPER_DIR/$script" ]; then
    log "auth-trigger: $script not executable, skipping"
    return
  fi

  # Fire via launchctl kickstart, not nohup. Backgrounded children of this
  # script live inside auth-watch's process group; when auth-watch exits a few
  # seconds later, launchd reaps the group with SIGTERM and kills the children
  # mid-flight (observed: daily-sync, calendar-sync, teams-sync, curate-docs
  # all dying with "Received signal 15" right after a restoration trigger).
  # kickstart starts the matching launchd job under its own management.
  local label="" unit=""
  case "$script" in
    sb-daily-sync.sh)       label="com.plessas.second-brain-sync"          unit="sb-daily-sync.service" ;;
    sb-calendar-sync.sh)    label="com.plessas.second-brain.calendar-sync" unit="sb-calendar-sync.service" ;;
    sb-curate-docs.sh)      label="com.plessas.second-brain.curate-docs"   unit="sb-curate-docs.service" ;;
    sb-teams-sync.sh)       label="com.plessas.second-brain.teams-sync"    unit="sb-teams-sync.service" ;;
    sb-reverse-ingest.sh)   label="com.plessas.second-brain.reverse-ingest" unit="sb-reverse-ingest.service" ;;
    sb-attachment-pass.sh)  label="com.plessas.second-brain.attachments"   unit="sb-attachments.service" ;;
  esac

  # systemd first on Linux, launchd first on macOS. Both hand the job to the
  # platform supervisor so it runs under ITS management. The nohup fallback is
  # a last resort and is actively unsafe: the child lands in THIS script's
  # cgroup (systemd KillMode=control-group) or process group (launchd) and is
  # SIGTERMed the instant we exit. It only ever worked because the caller
  # outlived it. Measured on the VPS: 2026-08-09 17:00 parent 2m15s vs child
  # 54s (survived), but 2026-08-10 01:00 parent 42s vs a ~55s child (would have
  # been killed). As its own probe-only unit this script exits in well under a
  # minute, so the fallback would lose that race nearly every time.
  if [ -n "$unit" ] && command -v systemctl >/dev/null 2>&1 \
     && systemctl --user start --no-block "$unit" 2>/dev/null; then
    log "auth-trigger: started $unit via systemd (reason=$reason)"
  elif [ -n "$label" ] && command -v launchctl >/dev/null 2>&1 \
     && launchctl kickstart "gui/$UID/$label" >/dev/null 2>&1; then
    log "auth-trigger: kickstarted $label (reason=$reason)"
  else
    nohup "$WRAPPER_DIR/$script" >> "$TRIGGERED_LOG" 2>&1 &
    log "auth-trigger: nohup-fallback $script (pid=$!, reason=$reason, label=${label:-none} unit=${unit:-none})"
  fi
}

# Determine which wrappers should fire. A job runs at most once per auth-watch
# tick even if multiple sentinels were restored.
fire_daily=0
fire_calendar=0
fire_curate=0
fire_teams=0
fire_reverse=0
fire_attachments=0
# Mail was missing from this list. A reauth restored every other outlook-gated
# job but left sb-outlook-sync waiting for its next tick, so the 2026-08-18
# lapse cost four hours of mail instead of minutes — and the overnight schedule
# (22:00 -> 01:00 -> 07:00) can stretch that to nine.
fire_outlook_sync=0

if [ "$GCLOUD_WAS_BLOCKED" = "1" ] && [ ! -f "$GCLOUD_SENTINEL" ]; then
  log "auth-trigger: gcloud ADC restored — queueing all gcloud-gated jobs"
  fire_daily=1; fire_calendar=1; fire_curate=1; fire_teams=1; fire_reverse=1; fire_attachments=1
  fire_outlook_sync=1
fi
if [ "$OUTLOOK_WAS_BLOCKED" = "1" ] && [ ! -f "$SENTINEL" ]; then
  log "auth-trigger: outlook auth restored — queueing outlook-gated jobs"
  fire_calendar=1; fire_curate=1; fire_reverse=1; fire_attachments=1; fire_outlook_sync=1
fi
if [ "$TEAMS_WAS_BLOCKED" = "1" ] && [ ! -f "$TEAMS_SENTINEL" ]; then
  log "auth-trigger: teams auth restored — queueing teams-sync"
  fire_teams=1
fi

if [ "$fire_daily$fire_calendar$fire_curate$fire_teams$fire_reverse$fire_attachments$fire_outlook_sync" != "0000000" ]; then
  echo "$(ts) — === auth-watch restoration trigger ===" >> "$TRIGGERED_LOG"
  [ "$fire_daily" = "1" ]       && trigger_job sb-daily-sync.sh        gcloud
  [ "$fire_calendar" = "1" ]    && trigger_job sb-calendar-sync.sh     gcloud_or_outlook
  [ "$fire_curate" = "1" ]      && trigger_job sb-curate-docs.sh       gcloud_or_outlook
  [ "$fire_teams" = "1" ]       && trigger_job sb-teams-sync.sh        gcloud_or_teams
  [ "$fire_reverse" = "1" ]     && trigger_job sb-reverse-ingest.sh    gcloud_or_outlook
  [ "$fire_attachments" = "1" ] && trigger_job sb-attachment-pass.sh   gcloud_or_outlook
  [ "$fire_outlook_sync" = "1" ] && trigger_job sb-outlook-sync.sh     gcloud_or_outlook
fi

exit 0
