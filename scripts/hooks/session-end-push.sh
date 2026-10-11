#!/bin/bash
# Claude Code SessionEnd hook: send the transcript of the session that just ended
# to the producer, whose conversation sync reads it into conversation memory.
#
# Claude Code pipes the hook its input as JSON on stdin (session_id,
# transcript_path, cwd, reason; agent_id and agent_type when the hook fires in a
# subagent, an in-process teammate or an --agent session) and gives every
# SessionEnd hook 1.5 s together. This script decides in milliseconds and leaves
# the push to sync-transcripts-to-vps.sh, started in the background and detached
# from the session, so a slow or absent network never holds the exit.
#
# Agent sessions are not sent: one whose hook input names an agent, a scripted
# run (claude -p, the Agent SDK), a subagent's transcript, and a split-pane
# teammate's, whose records name its team. Conversation memory holds the owner's
# sessions, and the producer's exporter leaves agent sessions out anyway; this
# only saves sending them.
#
# Installed as ~/.local/bin/session-end-push.sh beside the push (docs/DEPLOY.md).
# It never fails the hook: SessionEnd ignores the exit code, and whatever it
# printed would land in the owner's terminal. What it decided goes to the push's log.

set -u
PUSH="${SB_TRANSCRIPTS_PUSH:-$HOME/.local/bin/sync-transcripts-to-vps.sh}"
LOG="${SB_TRANSCRIPTS_LOG:-$HOME/Library/Logs/transcripts-sync-vps.log}"

# All of stdin first: the push started below must not inherit the pipe.
input=$(cat)

log() {
  mkdir -p "$(dirname "$LOG")" 2>/dev/null
  echo "[session-end] $(date '+%Y-%m-%d %H:%M:%S') $*" >>"$LOG" 2>/dev/null
}

# A top-level string field of the hook input, or empty. Claude Code writes the
# input; a path or an id in it holds no quote.
field() {
  printf '%s' "$input" | tr '\n' ' ' |
    sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p"
}

transcript=$(field transcript_path)
[ -n "$transcript" ] || exit 0
name=$(basename "$transcript")

if [ -n "$(field agent_type)$(field agent_id)" ]; then
  log "skip $name: an agent's session"
  exit 0
fi
case "${CLAUDE_CODE_ENTRYPOINT:-}" in
  sdk*)
    log "skip $name: a scripted run"
    exit 0
    ;;
esac
case "$transcript" in
  */subagents/*)
    log "skip $name: a subagent's transcript"
    exit 0
    ;;
esac
[ -f "$transcript" ] || exit 0
# A teammate's own session names its team on its records from the first lines.
# As a key, not as text: inside a message the quotes are escaped.
if head -n 50 "$transcript" 2>/dev/null | grep -Eq '"teamName"[[:space:]]*:[[:space:]]*"'; then
  log "skip $name: a teammate's session"
  exit 0
fi
if [ ! -f "$PUSH" ]; then
  log "skip $name: no push at $PUSH"
  exit 0
fi

nohup /bin/bash "$PUSH" "$transcript" </dev/null >/dev/null 2>&1 &
exit 0
