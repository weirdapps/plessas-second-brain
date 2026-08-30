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

# One retry and no more; the transfer loop below explains why one. 45s is long
# enough for OneDrive to finish hydrating the placeholder that failed, and short
# enough that a doubled run still finishes well inside its 6h window.
RSYNC_MAX_ATTEMPTS=2
RSYNC_RETRY_SLEEP=45

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

# Almost every file under ~/Documents/{National,Personal} is a DATALESS
# OneDrive placeholder, and reading one forces macOS to materialise it. macOS
# gates that materialisation behind kTCCServiceFileProviderDomain PER CLIENT
# BINARY. Homebrew's rsync is ad-hoc signed, so it is not a platform binary and
# macOS makes it its OWN TCC subject rather than attributing the read to the
# responsible process. That is why Full Disk Access on the interpreter did not
# cover it: on 2026-08-28 rsync exited 23 for six hours while a permission
# dialog sat unanswered on the screen.
#
# The grant that fixed it is pinned to a cdhash at a VERSIONED Cellar path. A
# `brew upgrade rsync` changes both, the grant stops matching in silence, and
# the job goes straight back to exit 23 with nobody at the keyboard to click
# Allow. Re-read this value after any rsync upgrade, with:
#   codesign -dvvv "$(command -v rsync)" 2>&1 | sed -n 's/^CDHash=//p'
EXPECTED_RSYNC_CDHASH="78d3b891ef9ec4827cb47f761d9d264ee6eb5df8"

# Echoes a human-actionable reason when the rsync on PATH is not the binary the
# grant was issued to, and stays silent otherwise. It fails OPEN on every case
# it cannot judge (no rsync, no codesign, no signature, an Apple binary): a
# tripwire that stops a working sync because it could not read a signature is
# worse than the time bomb it was added to watch for.
rsync_cdhash_mismatch() {
  local bin sig actual
  bin=$(command -v rsync 2>/dev/null)
  if [ -z "$bin" ]; then
    log "note: no rsync on PATH, cdhash tripwire skipped"
    return 0
  fi

  sig=$(codesign -dvvv "$bin" 2>&1)
  actual=$(printf '%s\n' "$sig" | sed -n 's/^CDHash=//p' | head -1)
  if [ -z "$actual" ]; then
    log "note: no readable code signature for $bin, cdhash tripwire skipped"
    return 0
  fi

  # Apple's own rsync is a platform binary, so it can never become its own TCC
  # subject: there is no per-binary grant to lose and nothing to warn about.
  # That is also why the fix here is NOT to switch to /usr/bin/rsync. If the
  # premise ever turned out to be wrong there would be no dialog to click and
  # no grant to add, which is a permanently dead job rather than a clickable
  # one.
  if printf '%s\n' "$sig" | grep -q '^Platform identifier='; then
    log "note: rsync at $bin is a platform binary, cdhash tripwire not applicable"
    return 0
  fi

  if [ "$actual" != "$EXPECTED_RSYNC_CDHASH" ]; then
    # No single quotes in this reason: it is interpolated into the ssh command
    # that writes the failure marker.
    printf '%s' "rsync at $bin now has cdhash $actual, not the $EXPECTED_RSYNC_CDHASH that was granted OneDrive FileProvider access. A human must re-grant it: System Settings > Privacy & Security > Files and Folders, allow the new rsync, then update EXPECTED_RSYNC_CDHASH in this script."
    return 1
  fi
  return 0
}

cdhash_problem=$(rsync_cdhash_mismatch)
if [ -n "$cdhash_problem" ]; then
  log "ABORT: $cdhash_problem"
  note_failure "$cdhash_problem"
fi

for tree in National Personal; do
  # A cdhash mismatch is fatal for the whole run rather than for one tree:
  # every transfer below would hit the same missing grant.
  if [ -n "$cdhash_problem" ]; then
    break
  fi

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
  #
  # Retried once, because the FIRST read of a dataless OneDrive placeholder is
  # what kicks its hydration off, and that read can fail while the next one,
  # moments later, walks straight through. Two of the three exit-23 events on
  # 2026-08-28 were exactly that shape. One retry and no more: a second failure
  # is a real problem and belongs in the failure marker rather than slept over.
  attempt=1
  while :; do
    out=$(rsync -rlptz --partial --stats \
      --exclude '.DS_Store' --exclude '.localized' \
      --exclude '~$*' --exclude '.~lock*' --exclude '*.tmp' \
      --exclude '.git/' \
      "$src/" "$VPS:Documents/$tree/" 2>&1)
    rsync_rc=$?
    if [ $rsync_rc -eq 0 ]; then
      break
    fi

    # Keep the lines that NAME something. `tail -3` used to be here and it only
    # ever caught the --stats footer, the one part of the output with no
    # diagnostic value at all: rsync 3.5.0 puts "rsync: [sender] send_files
    # failed to open ..." FIRST and a generic "(code 23)" line last, while
    # openrsync emits one "rsync(pid): error: ..." line first and nothing at
    # the end. Filter on content, because neither position nor wording is
    # portable across the two.
    log "rsync attempt $attempt FAILED for $tree (rc=$rsync_rc): $(printf '%s\n' "$out" | grep -iE 'rsync(\([0-9]+\))?:|error|failed' | head -5 | tr '\n' ' ')"

    if [ "$attempt" -ge "$RSYNC_MAX_ATTEMPTS" ]; then
      break
    fi
    attempt=$((attempt + 1))
    log "retrying $tree in ${RSYNC_RETRY_SLEEP}s (attempt $attempt of $RSYNC_MAX_ATTEMPTS)"
    sleep "$RSYNC_RETRY_SLEEP"
  done

  if [ "$rsync_rc" -ne 0 ]; then
    note_failure "rsync failed for $tree after $attempt attempts"
    continue
  fi

  # `Number of files transferred` is openrsync's wording. rsync 3.5.0, the one
  # actually on PATH here, says `Number of regular files transferred`, so this
  # never matched and every run logged "(? files transferred)": a real sync and
  # an empty one were indistinguishable in the log. -E because BSD sed rejects
  # `\?` in a basic regex and this runs on macOS.
  sent=$(printf '%s\n' "$out" | sed -n -E 's/^Number of (regular )?files transferred: *//p' | head -1)
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
