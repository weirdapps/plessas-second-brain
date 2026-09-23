"""Action-item lifecycle hygiene: dedup exact duplicates + age out stale actions.

Extraction is append-only, so action_items only ever grows and everything stays
'open' forever (122K+ open, many re-extracted duplicates or long-overdue). This
gives the 'open actions' view (query_action_items, stale_threads) a lifecycle:

  * dedup: remove exact-duplicate open actions from the same parent (same
    task + owner + deadline), a re-extraction artifact.
  * age-out: mark open actions whose deadline is far in the past as 'expired'
    (a soft close: NOT 'completed', since we can't prove completion, but no longer
    surfaced by the default status='open' queries). An action with no date ages
    with the email, thread, meeting or conversation it came from instead.

sb-daily-sync.sh runs it after each successful sync on the producer, whose
database the replicas copy. By hand, `--dry-run` rolls back.
"""

import re
import sqlite3
from datetime import date

from src.config import DEFAULT_DB
from src.store.schema import get_connection

DB_PATH = DEFAULT_DB

DEFAULT_EXPIRE_DAYS = 180
# An action with no date has no deadline to miss, so it expires this long after
# the last activity of its parent.
DEFAULT_UNDATED_EXPIRE_DAYS = 90

_ISO_DATE = "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'"

# When an action's parent last saw activity: a Teams thread by its last message,
# not its first, so a thread still in use keeps its actions.
_PARENT_LAST_ACTIVE = """COALESCE(
    (SELECT e.date_received FROM emails e WHERE e.id = action_items.email_id),
    (SELECT t.ended_at FROM teams_threads t WHERE t.id = action_items.teams_thread_id),
    (SELECT ce.start_at FROM calendar_events ce WHERE ce.id = action_items.event_id),
    (SELECT ct.timestamp FROM conversation_turns ct
      WHERE ct.id = action_items.conversation_turn_id))"""


def dedup_exact_open_actions(conn: sqlite3.Connection) -> int:
    """Delete duplicate OPEN actions sharing their parent, task, owner and deadline.

    The parent is all four parent columns. Grouped on email_id alone, every action
    from a Teams thread, a meeting or a conversation (email_id NULL) fell into one
    group, since GROUP BY puts NULLs together, and the same task under two parents
    was deleted as a duplicate.

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
              GROUP BY email_id, event_id, teams_thread_id, conversation_turn_id,
                       task, owner, COALESCE(deadline, '')
          )
        """
    )
    after = conn.execute("SELECT COUNT(*) FROM action_items WHERE status = 'open'").fetchone()[0]
    return before - after


def expire_stale_actions(conn: sqlite3.Connection, days: int = DEFAULT_EXPIRE_DAYS) -> int:
    """Mark OPEN actions with a parseable deadline older than `days` as 'expired'.

    Only touches dated actions (NULL/free-text deadlines are left alone), and
    never one whose parent is itself younger than `days`: a model that writes
    last year for 'by 30/9' dates a fresh action a year overdue, and this runs
    every day. The shield reaches `days` into the future too, since calendar
    sync runs a month ahead; a parent dated beyond that is a bad date, not a
    fresh one. Returns the number of rows expired.
    """
    cur = conn.execute(
        f"""
        UPDATE action_items
        SET status = 'expired'
        WHERE status = 'open'
          AND deadline IS NOT NULL
          AND deadline GLOB {_ISO_DATE}
          AND date(substr(deadline, 1, 10)) < date('now', '-' || ? || ' day')
          AND NOT COALESCE({_PARENT_LAST_ACTIVE} GLOB {_ISO_DATE}
              AND substr({_PARENT_LAST_ACTIVE}, 1, 10) >= date('now', '-' || ? || ' day')
              AND substr({_PARENT_LAST_ACTIVE}, 1, 10) <= date('now', '+' || ? || ' day'), 0)
        """,
        (days, days, days),
    )
    return cur.rowcount


# How a free-text deadline names a year: four digits standing alone, or two in
# a d/m/yy date, a m/yy month, a quarter or half ('Q4/26', 'H1-27') or a fiscal
# year ('FY26'). A year inside a longer number names none ('PO 120265'), nor do
# two digits after a time or a version ('17.30', 'v1.30'), nor the day of a full
# date ('10/26/2023', '2023/10/26'). An amount beside the date is no obstacle
# ('pay 4521 by 30/10/27').
_YEAR_FORMS = re.compile(
    r"(?<![0-9])(?P<full>20[0-9]{2})(?![0-9])"
    r"|(?<![0-9A-Za-z])[0-9]{1,2}[/.-][0-9]{1,2}[/.-](?P<dmy>[0-9]{2})(?![0-9])"
    r"|(?<![0-9A-Za-z/.-])(?:1[0-2]|[1-9])[/.-](?P<my>[0-9]{2})(?![0-9/-])"
    r"|(?<![0-9A-Za-z])[QqHh][1-4][/.-]?(?P<quarter>[0-9]{2})(?![0-9])"
    r"|(?<![0-9A-Za-z])[Ff][Yy] ?(?P<fiscal>[0-9]{2})(?![0-9])"
)


def _names_a_coming_year(text: str | None, this_year: int) -> int:
    """1 if `text` names this year or one of the next ten, in a _YEAR_FORMS form."""
    for match in _YEAR_FORMS.finditer(text or ""):
        full, *short = match.group("full", "dmy", "my", "quarter", "fiscal")
        year = int(full) if full else 2000 + int(next(digits for digits in short if digits))
        if this_year <= year <= this_year + 10:
            return 1
    return 0


def expire_undated_actions(
    conn: sqlite3.Connection, days: int = DEFAULT_UNDATED_EXPIRE_DAYS
) -> int:
    """Mark OPEN actions with no date as 'expired' once their parent is `days` old.

    expire_stale_actions needs a deadline to have passed, and on 2026-09-23 128K
    of the 154K open actions had none (NULL or free text such as 'ASAP'), so they
    stayed open for good. Every deadline the dated pass cannot use counts as
    none: free text, '2026-13-01', and strings SQLite's date() would still read
    ('2024' as a Julian day, '10:00', 'now'). Left alone: an action whose parent
    has no ISO date (no parent row, or '', the store's word for a missing date,
    which sorts before every date), and a free-text deadline naming this year or
    one of the next ten in any of the _YEAR_FORMS ('31/12/2027', '31/12/27',
    'Q4/26', 'FY26/27'), which is still to come. Returns the number of rows
    expired.
    """
    conn.create_function("sb_names_coming_year", 2, _names_a_coming_year, deterministic=True)
    cur = conn.execute(
        f"""
        UPDATE action_items
        SET status = 'expired'
        WHERE status = 'open'
          AND (deadline IS NULL
               OR ((deadline NOT GLOB {_ISO_DATE} OR date(substr(deadline, 1, 10)) IS NULL)
                   AND NOT sb_names_coming_year(deadline, ?)))
          AND {_PARENT_LAST_ACTIVE} GLOB {_ISO_DATE}
          AND substr({_PARENT_LAST_ACTIVE}, 1, 10) < date('now', '-' || ? || ' day')
        """,
        (date.today().year, days),
    )
    return cur.rowcount


def run_action_lifecycle(
    db_path: str | None = None,
    dry_run: bool = False,
    expire_days: int = DEFAULT_EXPIRE_DAYS,
    undated_expire_days: int = DEFAULT_UNDATED_EXPIRE_DAYS,
) -> dict:
    """Run dedup + age-out. Returns stats."""
    conn = get_connection(db_path or str(DB_PATH))
    before_open = conn.execute(
        "SELECT COUNT(*) FROM action_items WHERE status = 'open'"
    ).fetchone()[0]

    deduped = dedup_exact_open_actions(conn)
    expired = expire_stale_actions(conn, expire_days)
    expired_undated = expire_undated_actions(conn, undated_expire_days)

    after_open = conn.execute("SELECT COUNT(*) FROM action_items WHERE status = 'open'").fetchone()[
        0
    ]
    result = {
        "before_open": before_open,
        "deduped": deduped,
        "expired": expired,
        "expired_undated": expired_undated,
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
    parser.add_argument("--undated-expire-days", type=int, default=DEFAULT_UNDATED_EXPIRE_DAYS)
    args = parser.parse_args()
    run_action_lifecycle(args.db, args.dry_run, args.expire_days, args.undated_expire_days)
