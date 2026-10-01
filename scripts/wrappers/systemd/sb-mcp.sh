#!/bin/bash
# second-brain MCP over streamable HTTP, for clients that cannot spawn the stdio
# server themselves (the Telegram brain bot). Long-running: systemd keeps it up
# (Restart=always), so this wrapper only sets the scene and execs.
#
# Loopback only, bearer token required: src/mcp_http.py refuses anything else.
# BRAIN_ROLE=replica turns off the one write path into brain.db (the sharepoint_index
# refetch), although the master database sits next to this process.

set -uo pipefail

[ -f "$HOME/.zprofile" ] && source "$HOME/.zprofile" 2>/dev/null || true

PROJECT="$HOME/SourceCode/plessas-second-brain"
PYTHON="$HOME/.venvs/second-brain/bin/python"
export BRAIN_ROLE=replica
export BRAIN_MCP_TOKEN_FILE="${BRAIN_MCP_TOKEN_FILE:-$HOME/.config/second-brain/mcp-token}"

cd "$PROJECT" || exit 1
exec "$PYTHON" -m src.mcp_server --http "${SB_MCP_LISTEN:-127.0.0.1:8765}"
