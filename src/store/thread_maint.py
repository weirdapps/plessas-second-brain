"""`brain maint thread-rollups` and `thread-identity-revert` (issue 179), apart from
src/store/thread_identity.py so that the MCP server, which reaches that module through the
gated steps, never reaches the CLI."""

import sys

from src.store.thread_identity import enabled, revert


def cmd_thread_rollups(args) -> int:
    """`brain maint thread-rollups`: rollups for the threads of history that have none, newest
    first, `--limit` at a time (the nightly batches); a dry run (rolled back) unless --apply.
    By rule only: no model call."""
    from src.store import thread_rollups
    from src.store.schema import get_connection

    conn = get_connection(str(args.db))
    try:
        if not enabled(conn):
            print("No thread rollups: run `brain maint migrate --apply-gated` first.")
            return 1
        written = thread_rollups.backfill(conn, args.limit)
        if args.apply:
            conn.commit()
            print(f"thread-rollups: {written} written")
        else:
            conn.rollback()
            print(f"thread-rollups: dry run, {written} would be written; --apply writes them")
    finally:
        conn.close()
    sys.stdout.flush()
    return 0


def cmd_thread_identity_revert(args) -> int:
    """`brain maint thread-identity-revert`: every email back on the id it had before the
    gated step thread_identity, and the step marked not applied; a dry run unless --apply,
    which needs --backup as --apply-gated does."""
    from src.cli import _gated_backup
    from src.store.schema import get_connection

    conn = get_connection(str(args.db))
    try:
        if args.apply:
            _gated_backup(conn, args.backup)
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        changed = revert(conn)
        if args.apply:
            conn.commit()
            print(f"thread-identity-revert: {changed} emails back on their old thread")
        else:
            conn.rollback()
            print(f"thread-identity-revert: dry run, {changed} emails would go back")
    finally:
        conn.close()
    return 0


def add_maint_parsers(maint_subparsers) -> None:
    parser = maint_subparsers.add_parser(
        "thread-rollups",
        help="Build the rollups of threads that have none, newest first (after the gated "
        "thread_identity; dry run unless --apply; no model call)",
    )
    parser.add_argument("--limit", type=int, default=500, help="Threads per run (default 500)")
    parser.add_argument("--apply", action="store_true", help="Write the rollups")
    parser.set_defaults(func=cmd_thread_rollups)
    parser = maint_subparsers.add_parser(
        "thread-identity-revert",
        help="Put every email back on its thread id from before the gated thread_identity "
        "(dry run unless --apply, which needs --backup)",
    )
    parser.add_argument("--apply", action="store_true", help="Write the old ids back")
    parser.add_argument(
        "--backup", metavar="PATH", help="With --apply: a new file to back up to first"
    )
    parser.set_defaults(func=cmd_thread_identity_revert)
