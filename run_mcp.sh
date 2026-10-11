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

# The server's own settings (GEMINI_API_KEY, BRAIN_EMBED_BACKEND and the like), so
# it does not depend on the environment it was started with: Claude Code passes on
# whatever it was launched with, which under an agent team holds no shell profile,
# and semantic search went quiet there. The file holds keys, so it is read only
# when nobody but its owner can read it, and its values replace inherited ones.
ENV_FILE="${BRAIN_DATA_DIR:-$HOME/.second-brain}/env"
if [ -f "$ENV_FILE" ]; then
  # GNU stat, then BSD stat; -L judges the file a symlink points at.
  _mode="$(stat -L -c %a "$ENV_FILE" 2>/dev/null || stat -L -f %Lp "$ENV_FILE" 2>/dev/null)"
  case "$_mode" in
    *[0-7]00)
      set -a
      # shellcheck disable=SC1090
      . "$ENV_FILE"
      set +a
      ;;
    *)
      echo "second-brain MCP: not reading $ENV_FILE: its mode is ${_mode:-unknown}," \
        "open to group or others; chmod 600 it" >&2
      ;;
  esac
fi

for _py in \
  "${SECOND_BRAIN_VENV_PYTHON:-}" \
  "$SCRIPT_DIR/.venv/bin/python" \
  "$SCRIPT_DIR/venv/bin/python" \
  "$HOME/.venvs/second-brain/bin/python"; do
  [ -n "$_py" ] && [ -x "$_py" ] && exec "$_py" -m src.mcp_server
done
exec python3 -m src.mcp_server
