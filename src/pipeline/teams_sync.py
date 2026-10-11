"""`brain teams-sync`: discover, pull, bound, resolve, extract, embed, and their budgets.

Moved from src/cli.py on 2026-10-11 as it was: the CLI parses the arguments and calls
run(args) with the argparse namespace.
"""

import sys
from pathlib import Path

# --- Wall-clock budget for one Teams sync run -------------------------------
#
# sb-teams-sync also runs under TimeoutStartSec=600, and restoring the full
# 1,219-chat corpus turned every stage into real work at once. Bounding only
# the pull was not enough — the run still died, because Step 5 had limit=0 and
# 804 threads came up pending. Same rule as the Outlook budget above: state the
# stages together and assert the sum (tests/test_sync_budget.py).
#
#   step 1 discover    90 s   measured 86.4 s, fixed cost, no budget of its own
#   step 2 pull       150 s   ~330 of 1,219 chats at a measured 0.45 s each
#   step 3 bound       10 s   measured 1.3 s, SQL only
#   step 4 MRIs        40 s   already capped at 50 lookups/run
#   step 5 extract    150 s   LLM per thread
#   step 6 embed       30 s   local
#                     ----
#                     470 s of 600 s
#
# The pull rotates oldest-first, so every chat is read about every two hours,
# and deferred threads stay pending for the next run.
TEAMS_DISCOVER_OBSERVED_S = 90.0
TEAMS_PULL_DEADLINE_S = 150.0
TEAMS_BOUND_OBSERVED_S = 10.0
TEAMS_MRI_OBSERVED_S = 40.0
TEAMS_EXTRACT_DEADLINE_S = 150.0
TEAMS_EMBED_OBSERVED_S = 30.0
TEAMS_UNIT_TIMEOUT_S = 600.0

# Step 6 embeds by count, capped so the 30 s above still holds when a backlog
# appears: threads are now re-selected when their vector is missing from the
# index, and 5,336 were missing on 2026-09-23. 500 is five embedding batches and
# one index save; the backlog drains over a day of hourly runs.
TEAMS_EMBED_LIMIT = 500


def run(args):
    """Run the Teams ingestion pipeline: channels plus 1:1, group and meeting chats."""
    from src.export.teams_export import discover_chats, pull_messages
    from src.extract.teams_mri import resolve_mris
    from src.extract.teams_pipeline import extract_threads
    from src.extract.teams_threads import bound_threads
    from src.store.embeddings import build_teams_index
    from src.store.schema import get_connection, run_migrations

    db_path = str(args.db)
    if not Path(db_path).exists():
        print("Error: Database not found.", file=sys.stderr)
        sys.exit(1)

    conn = get_connection(db_path)
    run_migrations(conn)

    pull_failed = False
    if not getattr(args, "skip_pull", False):
        print("Step 1/6: discovering chats + channels...")
        d = discover_chats(conn, scope="all")
        print(
            f"  {d['chats_inserted']} new, {d['chats_updated']} updated, {d['chats_discovered']} total"
        )

        print("Step 2/6: pulling messages...")
        p = pull_messages(conn, concurrency=args.concurrency or 2, deadline_s=TEAMS_PULL_DEADLINE_S)
        deferred = p.get("deferred", 0)
        print(
            f"  {p['messages_inserted']} messages inserted across {p['chats_pulled']} chats; "
            f"{p['errors']} errors"
            + (
                f"; {deferred} chats deferred (out of time, next run takes them)"
                if deferred
                else ""
            )
        )
        # pull_messages re-raises only an expired session and counts every other
        # failure per chat, so a service-wide 403, 429 or 5xx arrives here as
        # errors with nothing pulled. That returned 0, and sb-teams-sync.sh wrote
        # 'ok' over a dead pull for as long as it lasted. Some chats pulled is a
        # partial run, not a failed one.
        pull_failed = p["errors"] > 0 and p["chats_pulled"] == 0
    else:
        print("Steps 1+2 SKIPPED — --skip-pull set.")

    print("Step 3/6: bounding threads...")
    b = bound_threads(conn)
    print(f"  {b['threads_created']} new threads, {b['threads_updated']} touched")

    print("Step 4/6: resolving MRIs...")
    r = resolve_mris(conn, max_per_run=50)
    print(
        f"  {r['resolved']} resolved, {r['permanent_fail']} 404'd, {r['retryable_fail']} retryable"
    )

    print("Step 5/6: extracting threads...")
    e = extract_threads(
        conn,
        workers=args.workers or 4,
        limit=args.limit or 0,
        deadline_s=TEAMS_EXTRACT_DEADLINE_S,
    )
    e_deferred = e.get("deferred", 0)
    e_refused = e.get("refused", 0)
    print(
        f"  {e['extracted']} extracted, {e['skipped']} skipped, {e['failed']} failed"
        + (f", {e_refused} refused" if e_refused else "")
        + (f", {e_deferred} deferred (out of time)" if e_deferred else "")
    )

    print("Step 6/6: embedding thread summaries...")
    n = build_teams_index(conn, limit=TEAMS_EMBED_LIMIT)
    print(f"  {n} new/updated embeddings")

    conn.close()
    print("teams-sync complete.")

    # The later steps still ran on what was already stored. 5 is "upstream
    # misbehaved", the same code as the M365 CLIs'. Extraction failures do not
    # count: one poison thread would otherwise keep the unit red every hour.
    if pull_failed:
        print(f"Every chat pull failed ({p['errors']} errors, 0 chats pulled)", file=sys.stderr)
        return 5
    return 0
