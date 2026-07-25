"""Action-item lifecycle hygiene: dedup exact duplicates + age out stale actions.

Extraction is append-only, so action_items only ever grows and everything stays
'open' forever (122K+ open, many re-extracted duplicates or long-overdue). This
gives the 'open actions' view (query_action_items, stale_threads) a lifecycle:

  * dedup — remove exact-duplicate open actions from the same email
    (same task + owner + deadline), a re-extraction artifact.
  * age-out — mark open actions whose deadline is far in the past as 'expired'
    (a soft close: NOT 'completed', since we can't prove completion, but no longer
    surfaced by the default status='open' queries).

Runs only under an explicit invocation (no cron); `--dry-run` rolls back.
"""

import sqlite3

from src.config import DEFAULT_DB
from src.store.schema import get_connection

DB_PATH = DEFAULT_DB

DEFAULT_EXPIRE_DAYS = 180


def dedup_exact_open_actions(conn: sqlite3.Connection) -> int:
    """Delete duplicate OPEN actions sharing (email_id, task, owner, deadline).

    Keeps the lowest id per group; groups of one are untouched. action_items has no
    dependents, so a plain delete is safe. Returns the number of rows removed.
    """
    before = conn.execute("SELECT COUNT(*) FROM action_items WHERE status = 'open'").fetchone()[0]
    conn.execute(
        """
        DELETE FROM action_items
        WHERE status = 'open'
          AND id NOT IN (
              SELECT MIN(id) FROM action_items
              WHERE status = 'open'
              GROUP BY email_id, task, owner, COALESCE(deadline, '')
          )
        """
    )
    after = conn.execute("SELECT COUNT(*) FROM action_items WHERE status = 'open'").fetchone()[0]
    return before - after


def expire_stale_actions(conn: sqlite3.Connection, days: int = DEFAULT_EXPIRE_DAYS) -> int:
    """Mark OPEN actions with a parseable deadline older than `days` as 'expired'.

    Only touches dated actions (NULL/free-text deadlines are left alone). Returns
    the number of rows expired.
    """
    cur = conn.execute(
        """
        UPDATE action_items
        SET status = 'expired'
        WHERE status = 'open'
          AND deadline IS NOT NULL
          AND deadline GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'
          AND date(substr(deadline, 1, 10)) < date('now', '-' || ? || ' day')
        """,
        (days,),
    )
    return cur.rowcount


def run_action_lifecycle(
    db_path: str | None = None, dry_run: bool = False, expire_days: int = DEFAULT_EXPIRE_DAYS
) -> dict:
    """Run dedup + age-out. Returns stats."""
    conn = get_connection(db_path or str(DB_PATH))
    before_open = conn.execute(
        "SELECT COUNT(*) FROM action_items WHERE status = 'open'"
    ).fetchone()[0]

    deduped = dedup_exact_open_actions(conn)
    expired = expire_stale_actions(conn, expire_days)

    after_open = conn.execute("SELECT COUNT(*) FROM action_items WHERE status = 'open'").fetchone()[
        0
    ]
    result = {
        "before_open": before_open,
        "deduped": deduped,
        "expired": expired,
        "after_open": after_open,
    }

    if dry_run:
        conn.rollback()
        print(f"DRY RUN — {result} (rolled back)")
    else:
        conn.commit()
        print(f"Action lifecycle: {result}")
    conn.close()
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Action-item lifecycle hygiene")
    parser.add_argument("--db", type=str, default=str(DB_PATH))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--expire-days", type=int, default=DEFAULT_EXPIRE_DAYS)
    args = parser.parse_args()
    run_action_lifecycle(args.db, args.dry_run, args.expire_days)
