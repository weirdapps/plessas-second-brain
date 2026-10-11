"""`brain whatsapp-sync`: ingest the pushed snapshot, bound, extract, embed, and their budgets.

Moved from src/cli.py on 2026-10-11 as it was: the CLI parses the arguments and calls
run(args) with the argparse namespace.
"""

import sys
from pathlib import Path

# --- Wall-clock budget for one WhatsApp sync run ----------------------------
#
# sb-whatsapp-sync runs under TimeoutStartSec=600 at :55, in the gap after the
# calendar and news runs finish and before outlook's :10, so it does not queue on
# the brain.db write lock behind sb-teams-sync (:30, up to 11 min). Same rule as
# the Teams budget above: state the stages and assert the sum
# (tests/whatsapp/test_cli.py).
#
#   step 1 ingest      30 s   one upsert transaction over the pushed snapshot
#   step 2 bound       10 s   SQL only
#   step 3 extract    300 s   LLM per session, deadline-bounded, rest stays pending
#   step 4 embed       60 s   capped by WHATSAPP_EMBED_LIMIT
#                     ----
#                     400 s of 600 s
WHATSAPP_INGEST_OBSERVED_S = 30.0
WHATSAPP_BOUND_OBSERVED_S = 10.0
WHATSAPP_EXTRACT_DEADLINE_S = 300.0
WHATSAPP_EMBED_OBSERVED_S = 60.0
WHATSAPP_UNIT_TIMEOUT_S = 600.0
WHATSAPP_EMBED_LIMIT = 500


def run(args):
    """Ingest the pushed WhatsApp snapshot, then bound, extract and embed sessions.

    The snapshot is pushed hourly by the Mac that runs the WhatsApp bridge
    (scripts/wrappers/launchd/sync-whatsapp-to-vps.sh). 66 (EX_NOINPUT) when it
    is missing or unreadable, which is a broken push, not an empty chat list.
    Extraction failures do not fail the run, as in teams-sync: one poison
    session would otherwise keep the unit red every hour.
    """
    from src.config import WHATSAPP_SNAPSHOT
    from src.export.whatsapp_export import SnapshotUnavailable, ingest_snapshot
    from src.extract.whatsapp_pipeline import extract_threads
    from src.extract.whatsapp_threads import bound_threads
    from src.store.embeddings import build_whatsapp_index
    from src.store.schema import get_connection, run_migrations

    db_path = str(args.db)
    if not Path(db_path).exists():
        print("Error: Database not found.", file=sys.stderr)
        sys.exit(1)

    conn = get_connection(db_path)
    run_migrations(conn)
    try:
        snapshot = Path(args.snapshot) if args.snapshot else WHATSAPP_SNAPSHOT
        print("Step 1/4: ingesting the snapshot...")
        try:
            i = ingest_snapshot(conn, snapshot)
        except SnapshotUnavailable as err:
            print(f"  {err}", file=sys.stderr)
            return 66
        print(
            f"  {i['messages_inserted']} new, {i['messages_updated']} updated across "
            f"{i['chats']} chats"
            + (f"; {i['skipped']} with no usable time" if i["skipped"] else "")
        )

        print("Step 2/4: bounding sessions...")
        b = bound_threads(conn)
        print(f"  {b['threads_created']} new sessions, {b['threads_updated']} touched")

        print("Step 3/4: extracting sessions...")
        e = extract_threads(conn, limit=args.limit or 0, deadline_s=WHATSAPP_EXTRACT_DEADLINE_S)
        print(
            f"  {e['extracted']} extracted, {e['skipped']} skipped, {e['failed']} failed"
            + (f", {e['deferred']} deferred" if e["deferred"] else "")
        )

        print("Step 4/4: embedding session summaries...")
        n = build_whatsapp_index(conn, limit=WHATSAPP_EMBED_LIMIT)
        print(f"  {n} new/updated embeddings")
    finally:
        conn.close()
    print("whatsapp-sync complete.")
    return 0
