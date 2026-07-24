#!/bin/bash
# Stop hook: automatically capture Claude Code conversations into second-brain.
# Reads session_id and transcript_path from stdin (Claude Code Stop event),
# debounces, and runs incremental ingest in the background.

input=$(cat)
session_id=$(echo "$input" | jq -r '.session_id // empty')
transcript=$(echo "$input" | jq -r '.transcript_path // empty')

# Guard: need both values
[ -z "$session_id" ] || [ -z "$transcript" ] && exit 0

# Debounce: skip if run <600s ago for this session
CACHE_DIR="/tmp/second-brain-capture"
mkdir -p "$CACHE_DIR"
cache_file="$CACHE_DIR/$session_id"
if [ -f "$cache_file" ]; then
  age=$(( $(date +%s) - $(stat -f%m "$cache_file") ))
  [ "$age" -lt 600 ] && exit 0
fi
touch "$cache_file"

# Run in background, non-blocking
BRAIN_DIR="$HOME/SourceCode/second-brain"
cd "$BRAIN_DIR"
nohup "$BRAIN_DIR/.venv/bin/python3" -m src.cli \
  ingest-conversation-incremental \
  --transcript "$transcript" \
  --session-id "$session_id" \
  &>/dev/null &

exit 0
