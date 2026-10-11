"""How completely the calendar is held, month by month.

`stats.coverage` gave the calendar's first and last date only, and agents are told
to check it before concluding that something did not happen. The first load kept
one page of outlook-cli's answer per month, the ten earliest events, so May to
August 2025 held ten events each against more than two hundred in a full month,
and read as covered from their first day (data-coverage-02).

A month is measured against the calendar's own usual month, the median of the
months already over, so the test fits a calendar of any size. Those still being
listed (this one and the 30 days the sync reaches ahead) are never flagged, and
neither is a month `calendar-sync --backfill` has listed completely, however few
meetings it held. A month a backfill began and did not finish is flagged until
one does.
"""

import sqlite3
from datetime import UTC, date, datetime
from statistics import median

from src.export.calendar_export import OUTLOOK_PAGE_SIZE

# A month holding less than this share of the usual month is not held in full.
# August 2026 held 119 events against a median of 216, a holiday month; the gap
# of September 2025 held 34.
SPARSE_SHARE = 0.25

# Months already over that it takes for their median to mean anything.
MIN_MONTHS = 3

# sync_metadata keys, by YYYY-MM: a month calendar-sync --backfill listed and stored
# completely, and one it began and has not finished. A month half filled by a run
# that stopped holds enough events to pass for a full one.
LISTED_PREFIX = "calendar_month_listed:"
STARTED_PREFIX = "calendar_month_started:"


def _next_month(month: str) -> str:
    year, number = int(month[:4]), int(month[5:7])
    return f"{year + number // 12}-{number % 12 + 1:02d}"


def _months(conn: sqlite3.Connection, prefix: str) -> set[str]:
    try:
        rows = conn.execute(
            "SELECT key FROM sync_metadata WHERE key LIKE ?", (prefix + "%",)
        ).fetchall()
    except sqlite3.OperationalError:  # a store older than sync_metadata
        return set()
    return {row[0][len(prefix) :] for row in rows}


def listed_months(conn: sqlite3.Connection) -> set[str]:
    """The months a backfill listed completely, as YYYY-MM."""
    return _months(conn, LISTED_PREFIX)


def started_months(conn: sqlite3.Connection) -> set[str]:
    """The months a backfill began and has not finished, as YYYY-MM."""
    return _months(conn, STARTED_PREFIX) - listed_months(conn)


def mark_month_started(conn: sqlite3.Connection, month: str, at: str) -> None:
    """Record that a backfill began `month` (YYYY-MM) at `at`, unless one already had."""
    conn.execute(
        "INSERT OR IGNORE INTO sync_metadata (key, value) VALUES (?, ?)",
        (STARTED_PREFIX + month, at),
    )
    conn.commit()


def record_listed_month(conn: sqlite3.Connection, month: str, at: str) -> None:
    """Record that every event of `month` (YYYY-MM) was listed and stored at `at`."""
    conn.execute(
        "INSERT OR REPLACE INTO sync_metadata (key, value) VALUES (?, ?)",
        (LISTED_PREFIX + month, at),
    )
    conn.execute("DELETE FROM sync_metadata WHERE key = ?", (STARTED_PREFIX + month,))
    conn.commit()


def held_per_month(conn: sqlite3.Connection) -> dict[str, int]:
    """Meetings held per month of their UTC start, every month from the first to the
    last held, a month holding none included. Cancelled ones are not meetings."""
    counts = {
        month: n
        for month, n in conn.execute(
            "SELECT substr(start_at, 1, 7), COUNT(*) FROM calendar_events "
            "WHERE is_cancelled = 0 GROUP BY 1"
        )
        if month
    }
    per_month: dict[str, int] = {}
    if counts:
        month, last = min(counts), max(counts)
        while month <= last:
            per_month[month] = counts.get(month, 0)
            month = _next_month(month)
    return per_month


def density(conn: sqlite3.Connection, today: date | None = None) -> dict:
    """Events held per month, the usual month, and the months not held in full.

    `incomplete` maps a month to `partial` when a backfill began it and has not
    finished, to `capped` when it holds exactly one listing page, and to `sparse`
    when it holds under SPARSE_SHARE of the usual month; `complete_from` is the
    first month after the last of them. A month holding exactly one page does not
    set the usual month either: it is suspect by construction.
    """
    current = f"{today or datetime.now(UTC).date():%Y-%m}"
    per_month = held_per_month(conn)
    over = {month: n for month, n in per_month.items() if month < current}
    measures = [n for n in over.values() if n != OUTLOOK_PAGE_SIZE]
    expected = round(median(measures)) if len(measures) >= MIN_MONTHS else None

    incomplete: dict[str, str] = {}
    listed, started = listed_months(conn), started_months(conn)
    for month, n in over.items():
        if month in listed:
            continue
        if month in started:
            incomplete[month] = "partial"
        elif expected and n < SPARSE_SHARE * expected:
            incomplete[month] = "capped" if n == OUTLOOK_PAGE_SIZE else "sparse"

    complete_from = None
    if incomplete:
        complete_from = _next_month(max(incomplete))
    elif per_month:
        complete_from = min(per_month)

    held: dict = {
        "per_month": per_month,
        "expected_per_month": expected,
        "incomplete": incomplete,
        "complete_from": complete_from,
    }
    if incomplete:
        held["note"] = (
            "Months in `incomplete` hold far fewer meetings than this calendar's usual "
            f"month (`expected_per_month`): `capped` ones exactly the {OUTLOOK_PAGE_SIZE} an "
            "early load kept of each month, `sparse` ones under a quarter, `partial` ones a "
            "backfill began and did not finish. A meeting there may be missing, so an empty "
            "answer for those months proves nothing; months from `complete_from` on are not "
            "flagged. `calendar-sync --backfill` fills them on the host that builds the store."
        )
    return held
