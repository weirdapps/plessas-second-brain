#!/bin/bash
# Full conversation backfill — runs extract → load in batches until done.
# Usage: nohup bash scripts/backfill-all.sh >> data/backfill.log 2>&1 &

set -e

cd "$(dirname "$0")/.."
PYTHON=".venv/bin/python"
LOG="data/backfill.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG"
}

log "========================================="
log "BACKFILL START"
log "========================================="

batch=1
while true; do
    log "--- Batch $batch: Extracting (limit 100, workers 3) ---"

    # Run extraction
    $PYTHON -m src.cli extract-conversations --workers 3 --limit 100 2>&1 | \
        grep -v "UserWarning\|warnings.warn\|_CLOUD_SDK" | tee -a "$LOG"

    # Check how many were actually extracted
    extracted=$(grep "Extracted:" data/extract.log 2>/dev/null | tail -1 | grep -oE '[0-9]+' | head -1)

    # Load into database
    log "--- Batch $batch: Loading into database ---"
    $PYTHON -m src.cli load 2>&1 | tee -a "$LOG"

    # Check remaining
    state_file="data/state/conv_extract_state.json"
    if [ -f "$state_file" ]; then
        total_processed=$(python3 -c "import json; print(json.load(open('$state_file')).get('total_extracted', 0))")
        log "Total conversations extracted so far: $total_processed"
    fi

    # Count staged vs extracted to see if there's more work
    staged=$(find data/staging/conversations -name "conversation-batch-*.json" -exec python3 -c "
import json, sys
total = 0
for f in sys.argv[1:]:
    total += len(json.load(open(f)).get('conversations', []))
print(total)
" {} +)
    extracted_count=$(ls data/extracted/conversations/*.json 2>/dev/null | wc -l | tr -d ' ')
    remaining=$((staged - extracted_count))

    log "Staged: $staged, Extracted: $extracted_count, Remaining: $remaining"

    if [ "$remaining" -le 0 ]; then
        log "All conversations extracted!"
        break
    fi

    log "--- Batch $batch complete. $remaining remaining. Starting next batch... ---"
    batch=$((batch + 1))

    # Brief pause between batches
    sleep 5
done

# Final embedding generation
log "--- Generating embeddings for all conversations ---"
$PYTHON -m src.cli embed 2>&1 | grep -v "UserWarning\|warnings.warn\|_CLOUD_SDK" | tee -a "$LOG"

# Final stats
log "--- Final statistics ---"
$PYTHON -c "
from src.store.schema import get_connection
from src.config import DEFAULT_DB
conn = get_connection(str(DEFAULT_DB))
convs = conn.execute('SELECT COUNT(*) FROM conversations').fetchone()[0]
turns = conn.execute('SELECT COUNT(*) FROM conversation_turns').fetchone()[0]
prefs = conn.execute(\"SELECT COUNT(*) FROM key_facts WHERE fact LIKE '%[PREFERENCE]%'\").fetchone()[0]
techs = conn.execute(\"SELECT COUNT(*) FROM key_facts WHERE fact LIKE '%[TECHNICAL]%'\").fetchone()[0]
decisions = conn.execute('SELECT COUNT(*) FROM decisions WHERE conversation_turn_id IS NOT NULL').fetchone()[0]
print(f'Conversations: {convs}')
print(f'Turns: {turns}')
print(f'Preferences: {prefs}')
print(f'Technical decisions: {techs}')
print(f'Decisions: {decisions}')
conn.close()
" 2>&1 | tee -a "$LOG"

log "========================================="
log "BACKFILL COMPLETE"
log "========================================="
