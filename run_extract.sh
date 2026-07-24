#!/bin/bash
# Local streaming extraction wrapper.
# Usage: ./run_extract.sh [--concurrency N] [--limit N]

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Activate venv
source .venv/bin/activate

echo "Starting local extraction at $(date)"
echo "Working dir: $SCRIPT_DIR"
echo ""

# Run extraction
python3 -m src.extract.local "$@"

echo ""
echo "Extraction finished at $(date)"
