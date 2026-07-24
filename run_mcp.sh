#!/bin/bash
# MCP server launcher. Resolves to the script's own directory so the repo can be
# cloned anywhere, and auto-detects the venv python so the SAME committed script
# works on every host (MacBook in-repo .venv, VPS out-of-repo ~/.venvs):
#   1. $SECOND_BRAIN_VENV_PYTHON  (explicit override)
#   2. ./.venv or ./venv          (in-repo; .venv may symlink to ~/.venvs)
#   3. ~/.venvs/second-brain      (shared venv dir)
#   4. python3                    (last resort)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
for _py in \
  "${SECOND_BRAIN_VENV_PYTHON:-}" \
  "$SCRIPT_DIR/.venv/bin/python" \
  "$SCRIPT_DIR/venv/bin/python" \
  "$HOME/.venvs/second-brain/bin/python"; do
  [ -n "$_py" ] && [ -x "$_py" ] && exec "$_py" -m src.mcp_server
done
exec python3 -m src.mcp_server
