#!/bin/zsh
# Push the documents changed in the National OneDrive tree to the producer's document inbox.
# Runs every 6h via LaunchAgent com.plessas.document-push-vps (2026-11-11).
#
# It replaces the mirror retired on 2026-10-07 (sync-documents-to-vps.sh, plus the
# producer's scheduled scan of the copy), which ingested a file saved from mail as a
# second document beside its attachment. Two things are different now:
#   - only the files changed since the last successful push go, into
#     ~/.second-brain/doc-inbox/National/ on the producer, with their path under National;
#     nothing is mirrored and nothing is kept there once read;
#   - the producer's nightly sb-attachment-pass reads the inbox with
#     `reverse-ingest --remove-ingested --no-summary`, and ingest_document skips a file
#     whose bytes a mail attachment already holds (attachments.sha256).
#
# The first run (no stamp) takes the files changed since SB_DOCS_SINCE, by default the
# day the mirror was retired. The stamp is the mark taken at the START of the last
# successful run, so a file saved during a run goes next time. Additive: nothing is
# deleted on either side by this script.
#
# Two lessons kept from the retired mirror:
#   - launchd children cannot read a dataless OneDrive placeholder (EDEADLK) unless the
#     process opts in to materialising it; the re-exec below sets that io-policy once,
#     and every child inherits it. Fails open, logged.
#   - the LaunchAgent runs /bin/zsh because zsh holds Full Disk Access here and bash
#     does not; a denied enumeration looks like an empty tree, so it is checked.

set -uo pipefail
umask 077
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

if [ -z "${DOCPUSH_MATERIALIZE_ON:-}" ]; then
  if /usr/bin/python3 -c 'import ctypes; ctypes.CDLL("/usr/lib/libSystem.B.dylib")' 2>/dev/null; then
    export DOCPUSH_MATERIALIZE_ON=pending
    exec /usr/bin/python3 -c 'import ctypes, os, sys
libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
# setiopolicy_np(IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES=3, IOPOL_SCOPE_PROCESS=0,
#                IOPOL_MATERIALIZE_DATALESS_FILES_ON=2), from sys/resource.h.
os.environ["DOCPUSH_MATERIALIZE_ON"] = "1" if libc.setiopolicy_np(3, 0, 2) == 0 else "failed"
os.execv(sys.argv[1], sys.argv[1:])' /bin/zsh "$0" "$@"
  fi
  export DOCPUSH_MATERIALIZE_ON=unavailable
fi

VPS="${SB_DOCS_VPS:-vps}"
# Overridable so a test can stand in for the transport by name (a stub on PATH is not
# enough: the PATH line above puts Homebrew and /usr/bin first).
SSH="${SB_DOCS_SSH:-ssh}"
# Homebrew rsync by name: the system openrsync failed deltas past 4 GiB on this Mac.
RSYNC="${SB_DOCS_RSYNC:-/opt/homebrew/bin/rsync}"
SRC="${SB_DOCS_ROOT:-$HOME/Library/CloudStorage/OneDrive-Personal/Documents/National}"
SINCE="${SB_DOCS_SINCE:-202610070000}"
LOG="${SB_DOCS_LOG:-$HOME/Library/Logs/document-push-vps.log}"
STATE="${SB_DOCS_STATE:-$HOME/.second-brain}"
STAMP="$STATE/document-push.stamp"
FAIL="$STATE/document-push.fail"
REMOTE_DIR=".second-brain/doc-inbox/National"
SSH_OPTS=(-o ConnectTimeout=10 -o BatchMode=yes)
LOCK="${TMPDIR:-/tmp}/.document-push-vps.lock"

mkdir -p "$STATE" "$(dirname "$LOG")"
log() { echo "[doc-push] $(date '+%Y-%m-%d %H:%M:%S') $*" >>"$LOG"; }

case "${DOCPUSH_MATERIALIZE_ON:-}" in
  failed) log "WARN: setiopolicy_np refused; dataless placeholders will fail with EDEADLK" ;;
  unavailable) log "WARN: no usable /usr/bin/python3; dataless placeholders will fail with EDEADLK" ;;
esac

if ! mkdir "$LOCK" 2>/dev/null; then
  pid=$(cat "$LOCK/pid" 2>/dev/null || echo "")
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    log "already running (pid $pid), skipping"
    exit 0
  fi
  rm -rf "$LOCK"
  mkdir "$LOCK" 2>/dev/null || exit 0
fi
echo $$ >"$LOCK/pid"
WORK=$(mktemp -d)
trap 'rm -rf "$LOCK" "$WORK"' EXIT INT TERM

# A laptop off the network is this job's normal state: skip quietly, and write the failure
# marker only once the last push is 24h old or after 4 unreachable runs (4 x 6h).
# ---- UNREACHABLE-GATE-BEGIN ----
UNREACHABLE_RUNS_FILE="$STATE/document-push.unreachable-runs"
UNREACHABLE_MAX_RUNS=4
UNREACHABLE_MAX_AGE=86400

unreachable_verdict() {
  local runs last_ok since=-1 ago reason
  runs=$(cat "$UNREACHABLE_RUNS_FILE" 2>/dev/null)
  case "$runs" in '' | *[!0-9]*) runs=0 ;; esac
  runs=$((runs + 1))
  printf '%s\n' "$runs" >"$UNREACHABLE_RUNS_FILE"
  last_ok=$(/usr/bin/stat -f %m "$STAMP" 2>/dev/null)
  [ -n "$last_ok" ] && since=$(($(date +%s) - last_ok))
  if [ "$runs" -lt "$UNREACHABLE_MAX_RUNS" ] && [ "$since" -lt "$UNREACHABLE_MAX_AGE" ]; then
    log "producer unreachable: skipping ($runs consecutive; pages at $UNREACHABLE_MAX_RUNS, or 24h after the last successful push)"
    return 0
  fi
  if [ "$since" -ge 0 ]; then ago="$((since / 3600))h ago"; else ago="never recorded here"; fi
  reason="unreachable: ssh to $VPS failed on $runs consecutive runs, last successful push $ago"
  printf '%s\n%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')" "$reason" >"$FAIL"
  log "producer unreachable: $reason; failure marker written"
  return 1
}
# ---- UNREACHABLE-GATE-END ----
if ! "$SSH" "${SSH_OPTS[@]}" "$VPS" true 2>/dev/null; then
  unreachable_verdict
  exit $?
fi
rm -f "$UNREACHABLE_RUNS_FILE"

now=$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')
# Reasons are fixed strings plus numbers: they go inside single quotes on the producer.
fail() {
  printf '%s\n%s\n' "$now" "$1" >"$FAIL"
  "$SSH" "${SSH_OPTS[@]}" "$VPS" \
    "umask 077; mkdir -p ~/.second-brain && printf '%s\n%s\n' '$now' '$1' > ~/.second-brain/document-push.fail" 2>>"$LOG" ||
    log "WARN: failure marker write to the producer failed"
  log "FAIL: $1; failure marker written"
  exit 1
}

[ -d "$SRC" ] || fail "MISSING local root National"
ls "$SRC" >/dev/null 2>&1 || fail "DENIED reading National (grant Full Disk Access to /bin/zsh)"

# The mark for the next run, taken before the scan.
MARK="$WORK/mark"
touch "$MARK"
REF="$STAMP"
if [ ! -f "$REF" ]; then
  REF="$WORK/since"
  touch -t "$SINCE" "$REF" || fail "bad SB_DOCS_SINCE"
fi

# The extensions reverse-ingest reads; Office lock files, temp files and hidden files never.
LIST="$WORK/files"
(cd "$SRC" && find . -type f -newer "$REF" \
  \( -iname '*.pdf' -o -iname '*.pptx' -o -iname '*.xlsx' -o -iname '*.docx' \
  -o -iname '*.md' -o -iname '*.txt' \) \
  ! -name '~$*' ! -name '.*' ! -name '*.tmp' ! -path '*/.git/*' -print0) >"$LIST" ||
  fail "scan of National failed"
count=$(tr -cd '\0' <"$LIST" | wc -c | tr -d ' ')

if [ "$count" -gt 0 ]; then
  "$SSH" "${SSH_OPTS[@]}" "$VPS" "umask 077; mkdir -p ~/$REMOTE_DIR" 2>>"$LOG" ||
    fail "could not create the inbox on the producer"
  out=$("$RSYNC" -rt --from0 --files-from="$LIST" --partial --chmod=Du=rwx,Fu=rw \
    -e "$SSH ${SSH_OPTS[*]}" "$SRC/" "$VPS:$REMOTE_DIR/" 2>&1)
  rc=$?
  if [ "$rc" -ne 0 ]; then
    log "rsync failed (rc=$rc): $(printf '%s\n' "$out" | grep -iE 'rsync|error|failed' | head -5 | tr '\n' ' ')"
    fail "rsync failed with rc $rc for $count files"
  fi
fi

# Success: the mark becomes the stamp, on both hosts the stamp says when.
mv "$MARK" "$STAMP"
rm -f "$FAIL"
"$SSH" "${SSH_OPTS[@]}" "$VPS" \
  "mkdir -p ~/.second-brain && printf '%s\n' '$now' > ~/.second-brain/document-push.stamp && rm -f ~/.second-brain/document-push.fail" 2>>"$LOG" ||
  log "WARN: stamp write to the producer failed"
log "pushed $count file(s) to the document inbox"
exit 0
