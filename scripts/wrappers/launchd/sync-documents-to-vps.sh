#!/bin/zsh
# Mirror the OneDrive document trees to the VPS.
# Runs every 6h via LaunchAgent com.plessas.document-sync-vps.
#
# Why this exists: second-brain's reverse-ingest runs ON THE VPS and scans
# ~/Documents/{National,Personal} there. Those roots are only a copy of this
# MacBook's OneDrive. Nothing kept them in sync, so they froze — on 2026-08-06
# the VPS copy's newest real document was 2026-06-06 while this Mac had files
# through 2026-07-24 (60 National documents missing). reverse-ingest kept
# reporting OK every day because it was scanning a stale snapshot perfectly.

set -uo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

LOG="$HOME/Library/Logs/document-sync-vps.log"
LOCK="/tmp/.document-sync-vps.lock"
VPS="vps"

# The binary launchd actually spawned. TCC grants Full Disk Access per-binary,
# so this is the name that matters — the message used to hardcode /bin/bash and
# would have sent the next reader after the wrong one.
INTERP=$(ps -o comm= -p $$ 2>/dev/null || echo "the interpreter")

log() { echo "[doc-sync] $(date '+%Y-%m-%d %H:%M:%S') $*" >>"$LOG"; }

# Single-instance guard: the first sync moves ~4 GB and must not overlap itself.
if ! mkdir "$LOCK" 2>/dev/null; then
  pid=$(cat "$LOCK/pid" 2>/dev/null || echo "")
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    log "already running (pid $pid) — skipping"
    exit 0
  fi
  rm -rf "$LOCK"
  mkdir "$LOCK" 2>/dev/null || exit 0
fi
echo $$ >"$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT INT TERM

# Bail out quietly when the VPS is unreachable (laptop off-network) so this does
# not log an rsync failure every 6 hours.
if ! ssh -o ConnectTimeout=10 -o BatchMode=yes "$VPS" true 2>/dev/null; then
  log "VPS unreachable — skipping"
  exit 0
fi

rc=0
# Reasons accumulate so the failure marker can name the cause, not just the
# fact. Keep them free of single quotes: they are interpolated into the ssh
# command below.
fail_reason=""
note_failure() {
  rc=1
  fail_reason="${fail_reason:+$fail_reason; }$1"
}

for tree in National Personal; do
  src="$HOME/Documents/$tree"

  # Refuse to sync a tree that is not present. OneDrive can transiently unmount;
  # syncing "nothing" is harmless here only because we never pass --delete.
  if [ ! -d "$src" ]; then
    log "MISSING local root $src — skipping"
    note_failure "MISSING local root $tree"
    continue
  fi

  # ~/Documents is TCC-protected on macOS. A launchd-spawned shell without Full
  # Disk Access can stat the directory but gets "Operation not permitted" when
  # enumerating it, so rsync sees an EMPTY source and reports a bare "total size
  # is 0" failure that reads like a network problem. Detect it and name the
  # binary that needs the grant. The LaunchAgent runs /bin/zsh precisely because
  # it already holds FDA on this machine; /bin/bash does not.
  if ! ls "$src" >/dev/null 2>&1; then
    log "DENIED reading $src — grant Full Disk Access to $INTERP in"
    log "      System Settings > Privacy & Security > Full Disk Access, then retry."
    note_failure "DENIED reading $tree (grant Full Disk Access to $INTERP)"
    continue
  fi

  # Additive only — deliberately NO --delete. A --delete run against a
  # half-mounted OneDrive tree would wipe the VPS copy that the brain reads
  # from. Deleted files linger on the VPS instead; reverse-ingest's
  # latest-version-per-logical-name dedup already tolerates that.
  out=$(rsync -rlptz --partial --stats \
    --exclude '.DS_Store' --exclude '.localized' \
    --exclude '~$*' --exclude '.~lock*' --exclude '*.tmp' \
    --exclude '.git/' \
    "$src/" "$VPS:Documents/$tree/" 2>&1)

  if [ $? -ne 0 ]; then
    log "rsync FAILED for $tree: $(echo "$out" | tail -3 | tr '\n' ' ')"
    note_failure "rsync failed for $tree"
    continue
  fi

  sent=$(echo "$out" | sed -n 's/^Number of files transferred: *//p' | head -1)
  log "synced $tree (${sent:-?} files transferred)"
done

# Heartbeat for the VPS health check. check_document_roots treats this stamp as
# the authoritative "is the source still connected?" signal rather than the
# newest file mtime: sb-curate-docs writes its own files into the very same
# roots, so mtimes stay fresh even when this push has been dead for weeks —
# which is exactly how the 2026-06-06 freeze went unnoticed.
#
# The stamp records only SUCCESSES, and check_document_roots judges it against a
# 14-day window — so a run that fails every time emits no signal for a fortnight.
# That is exactly what happened from 2026-08-06: 56 consecutive TCC denials while
# the health report said OK. A failed run therefore writes document-sync.fail
# (line 1 ISO-8601 UTC, line 2 the reason), which the check reads ahead of the
# stamp; a later success removes it. Written on both hosts: the VPS check is the
# authoritative one, the Mac check is where the failure actually originates.
now=$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')
mkdir -p "$HOME/.second-brain"

if [ "$rc" -eq 0 ]; then
  if ssh -o ConnectTimeout=10 -o BatchMode=yes "$VPS" \
    "mkdir -p ~/.second-brain && printf '%s\n' '$now' > ~/.second-brain/document-sync.stamp && rm -f ~/.second-brain/document-sync.fail" 2>/dev/null; then
    log "heartbeat written ($now)"
  else
    log "WARN: heartbeat write failed"
  fi
  # Locally too, so a health check run on this Mac judges the push by the push
  # rather than falling back to mtimes and calling a quiet fortnight STALE.
  printf '%s\n' "$now" >"$HOME/.second-brain/document-sync.stamp"
  rm -f "$HOME/.second-brain/document-sync.fail"
else
  printf '%s\n%s\n' "$now" "$fail_reason" >"$HOME/.second-brain/document-sync.fail"
  if ! ssh -o ConnectTimeout=10 -o BatchMode=yes "$VPS" \
    "mkdir -p ~/.second-brain && printf '%s\n%s\n' '$now' '$fail_reason' > ~/.second-brain/document-sync.fail" 2>/dev/null; then
    log "WARN: failure marker write to VPS failed"
  fi
  log "failure marker written ($fail_reason)"
fi

exit $rc
