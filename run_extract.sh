#!/bin/bash
# Local streaming extraction wrapper.
# Usage: ./run_extract.sh [--workers N] [--limit N]

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Find the interpreter the way run_mcp.sh does: the producer has no in-repo
# .venv, its venv lives in ~/.venvs/second-brain.
PYTHON=""
for _py in \
  "${SECOND_BRAIN_VENV_PYTHON:-}" \
  "$SCRIPT_DIR/.venv/bin/python" \
  "$SCRIPT_DIR/venv/bin/python" \
  "$HOME/.venvs/second-brain/bin/python"; do
  if [ -n "$_py" ] && [ -x "$_py" ]; then
    PYTHON="$_py"
    break
  fi
done
if [ -z "$PYTHON" ]; then
  echo "Error: no second-brain virtual environment found (.venv, venv, ~/.venvs/second-brain)" >&2
  echo "Run: uv sync --frozen   (or set SECOND_BRAIN_VENV_PYTHON)" >&2
  exit 1
fi

echo "Starting local extraction at $(date)"
echo "Working dir: $SCRIPT_DIR"
echo ""

# Run extraction
"$PYTHON" -m src.extract.local "$@"

echo ""
echo "Extraction finished at $(date)"
