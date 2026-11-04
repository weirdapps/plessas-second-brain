#!/bin/bash
# scripts/deploy.sh: bring the producer level with this checkout, as one reconciliation
# (issue 173). Run by the hourly pull's hook when it brought new commits, from the
# checkout; safe to run by hand at any time, and a second run changes nothing.
#
#   1. the venv, from the lock: uv sync --frozen --no-dev --inexact
#   2. each systemd wrapper whose installed copy in ~/.local/bin differs (cmp); a wrapper
#      with no installed copy has no unit yet, which is a manual step, so it is named only
#   3. the schema, once, when the store is behind the code (`brain maint migrate --check`
#      exits 3): the active sb-*.timers stopped, the running sb-* jobs let finish (up to
#      SB_DEPLOY_WAIT_SECONDS; the migration is held, and the deploy fails, if one does not),
#      `brain maint migrate`, the same timers started again whatever happened. Numbered
#      migrations only: a gated one is the owner's step, after a backup and a dry run, and
#      this script never passes --apply-gated. Jobs refuse a behind store meanwhile
#      (src/store/schema.py ensure_schema), so none migrates it under the timers.
#   4. try-restart sb-mcp, which keeps serving the code it started with.
#
# Exits non-zero when a step failed, so the pull's hook counts the repo as failed.
# Log: ~/.second-brain/logs/deploy.log, and stdout.
set -uo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${UV_PROJECT_ENVIRONMENT:-$HOME/.venvs/second-brain}"
PYTHON="$VENV/bin/python"
BIN="$HOME/.local/bin"
WRAPPERS="$PROJECT/scripts/wrappers/systemd"
LOG="$HOME/.second-brain/logs/deploy.log"
WAIT_S="${SB_DEPLOY_WAIT_SECONDS:-900}"
POLL_S="${SB_DEPLOY_POLL_SECONDS:-10}"
# The always-on server is no job to wait for.
LONG_RUNNING='^sb-mcp\.service$'

mkdir -p "$(dirname "$LOG")"
say() {
  local line
  line="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
  echo "$line"
  echo "$line" >> "$LOG"
}

UV="$(command -v uv || true)"
[ -n "$UV" ] || UV="$HOME/.local/bin/uv"

cd "$PROJECT" || exit 1
say "deploy $(git rev-parse --short HEAD 2>/dev/null || echo '?') from $PROJECT"

# 1. The venv.
if ! UV_PROJECT_ENVIRONMENT="$VENV" "$UV" sync --frozen --no-dev --inexact; then
  say "FAILED: uv sync --frozen; nothing else done"
  exit 1
fi
say "venv in sync with uv.lock"

# 2. The wrappers.
failed=0
for src in "$WRAPPERS"/*.sh; do
  name="$(basename "$src")"
  dest="$BIN/$name"
  if [ ! -e "$dest" ]; then
    say "wrapper $name: not installed here (no unit yet), left alone"
  elif ! cmp -s "$src" "$dest"; then
    if install -m 755 "$src" "$dest"; then
      say "wrapper $name: installed"
    else
      say "FAILED: install $name"
      failed=1
    fi
  fi
done

# 3. The schema.
running_jobs() {
  systemctl --user list-units --type=service --state=active,activating,deactivating \
    --plain --no-legend 'sb-*.service' 2>/dev/null | awk '{print $1}' | grep -Ev "$LONG_RUNNING"
}

check_out="$("$PYTHON" -m src.cli maint migrate --check 2>&1)"
check_rc=$?
say "schema: $(printf '%s' "$check_out" | head -1)"
if [ "$check_rc" -eq 3 ]; then
  timers="$(systemctl --user list-units --type=timer --state=active --plain --no-legend \
    'sb-*.timer' 2>/dev/null | awk '{print $1}' | tr '\n' ' ')"
  restart_timers() {
    if [ -n "${timers// /}" ]; then
      # shellcheck disable=SC2086
      systemctl --user start $timers && say "timers started: $timers"
    fi
  }
  trap restart_timers EXIT
  if [ -n "${timers// /}" ]; then
    # shellcheck disable=SC2086
    systemctl --user stop $timers
    say "timers stopped: $timers"
  fi
  waited=0
  busy="$(running_jobs)"
  while [ -n "$busy" ] && [ "$waited" -lt "$WAIT_S" ]; do
    sleep "$POLL_S"
    waited=$((waited + POLL_S))
    [ "$POLL_S" -gt 0 ] || waited=$((waited + 1))
    busy="$(running_jobs)"
  done
  if [ -n "$busy" ]; then
    say "FAILED: still running after ${WAIT_S}s, migration held: $(echo "$busy" | tr '\n' ' ')"
    exit 1
  fi
  if "$PYTHON" -m src.cli maint migrate; then
    say "schema migrated"
  else
    say "FAILED: brain maint migrate"
    exit 1
  fi
  restart_timers
  trap - EXIT
elif [ "$check_rc" -ne 0 ]; then
  say "FAILED: brain maint migrate --check exited $check_rc: $check_out"
  exit 1
fi

# 4. The HTTP server.
if [ -e "$HOME/.config/systemd/user/sb-mcp.service" ]; then
  systemctl --user try-restart sb-mcp.service || failed=1
fi

if [ "$failed" -ne 0 ]; then
  say "deploy finished with failures"
  exit 1
fi
say "deploy done"
