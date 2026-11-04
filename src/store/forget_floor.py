"""The forget floor per source (issue 286): what `brain maint forget --before DATE` removed
stays removed.

The owner's decision (2026-11-04) is a date floor per source, not a tombstone list. Every
`forget --before DATE --apply` records DATE as the floor of each source it was run on
(src/store/retention.py), in `sync_metadata` under `forget_floor:<source>`, inside the same
transaction as the delete; a dry run records nothing. The floor only moves forward: a later
forget with an earlier date leaves it where it is.

Every path that stores an item of a source refuses one older than that source's floor and
counts it (`refuse`, SKIPPED): the Outlook export, the mail load, mail-reconcile, the Teams
sync and teams-backfill-replies, the WhatsApp sync, calendar-sync and calendar-backfill, the
conversation export and load, and the documents ingested as mail. An item is older than the
floor when the time it is dated by compares below it, the comparison forget itself makes on
the stored text; an item with no time is let through, as forget leaves undated rows alone.

`brain maint forget-floor` lists the floors; `--clear SOURCE` removes one (the owner's call:
a backfill may then load again what was forgotten).
"""

from __future__ import annotations

import sqlite3
import sys
from collections import Counter
from pathlib import Path

SOURCES = ("email", "teams", "whatsapp", "calendar", "conversation")
KEY = "forget_floor:"

# What this process refused, per source; the jobs print it (print_skipped).
SKIPPED: Counter[str] = Counter()


def _has_metadata(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sync_metadata'"
        ).fetchone()
        is not None
    )


def floors(conn: sqlite3.Connection) -> dict[str, str]:
    """source -> floor, for each source that has one."""
    if not _has_metadata(conn):
        return {}
    return {
        key[len(KEY) :]: value
        for key, value in conn.execute(
            "SELECT key, value FROM sync_metadata WHERE key GLOB ?", (KEY + "*",)
        )
        if value
    }


def floor(conn: sqlite3.Connection, source: str) -> str | None:
    """The source's floor, or None."""
    if not _has_metadata(conn):
        return None
    row = conn.execute("SELECT value FROM sync_metadata WHERE key = ?", (KEY + source,)).fetchone()
    return row[0] if row and row[0] else None


def floor_at(db_path: Path | str | None, source: str) -> str | None:
    """The floor read from a store by path, read-only; None when it cannot be read (the
    loader's own check still refuses what passes here)."""
    if not db_path or not Path(db_path).exists():
        return None
    from src.store.sql_readonly import connect_read_only

    try:
        conn = connect_read_only(Path(db_path))
    except (OSError, sqlite3.Error):
        return None
    try:
        return floor(conn, source)
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def raise_floors(conn: sqlite3.Connection, sources, cutoff: str) -> dict[str, str]:
    """Set each source's floor to `cutoff` where that moves it forward; in the caller's
    transaction. Returns the floors of those sources after."""
    out = {}
    for source in sorted(set(sources)):
        if source not in SOURCES:
            raise ValueError(f"unknown source {source!r}")
        current = floor(conn, source)
        if current is None or cutoff > current:
            conn.execute(
                "INSERT INTO sync_metadata (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (KEY + source, cutoff),
            )
            current = cutoff
        out[source] = current
    return out


def clear(conn: sqlite3.Connection, source: str) -> str | None:
    """Remove the source's floor; the floor it had, or None. The caller commits."""
    old = floor(conn, source)
    if old is not None:
        conn.execute("DELETE FROM sync_metadata WHERE key = ?", (KEY + source,))
    return old


def below(floor_value: str | None, when) -> bool:
    """Whether an item dated `when` is older than the floor; undated is never below."""
    return bool(floor_value) and bool(when) and str(when) < str(floor_value)


def refuse(source: str, floor_value: str | None, when) -> bool:
    """`below`, counting a refusal in SKIPPED."""
    if below(floor_value, when):
        SKIPPED[source] += 1
        return True
    return False


def print_skipped(file=None) -> None:
    """One line of what this process refused, when it refused anything."""
    if any(SKIPPED.values()):
        counts = ", ".join(f"{s} {n:,}" for s, n in sorted(SKIPPED.items()) if n)
        print(f"  Older than the forget floor, not stored: {counts}", file=file or sys.stdout)


# --- the command ------------------------------------------------------------------------------


def cmd_forget_floor(args) -> int:
    """`brain maint forget-floor`: list the floors; --clear SOURCE removes one."""
    from src import config
    from src.store.schema import get_connection

    if args.clear and config.is_replica():
        print(config.replica_refusal("maint forget-floor --clear"), file=sys.stderr)
        return 2
    if not Path(args.db).exists():
        print(f"Error: no store at {args.db}", file=sys.stderr)
        return 1
    conn = get_connection(str(args.db))
    try:
        if args.clear:
            old = clear(conn, args.clear)
            conn.commit()
            if old is None:
                print(f"No forget floor for {args.clear}")
            else:
                print(
                    f"WARNING: removed the forget floor of {args.clear} ({old}). A backfill or a"
                    " re-export reaching before that day now loads again what was forgotten.",
                    file=sys.stderr,
                )
        held = floors(conn)
    finally:
        conn.close()
    print("Forget floors (nothing older is stored again):")
    for source in SOURCES:
        print(f"  {source}: {held.get(source, '(none)')}")
    return 0


def add_maint_parsers(maint_subparsers) -> None:
    """`brain maint forget-floor`."""
    p = maint_subparsers.add_parser(
        "forget-floor",
        help="List the forget floors per source; --clear SOURCE removes one",
        description=__doc__,
    )
    p.add_argument("--clear", choices=SOURCES, metavar="SOURCE", help="Remove this source's floor")
    p.set_defaults(func=cmd_forget_floor)
