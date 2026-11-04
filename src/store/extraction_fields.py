"""The gated migration `extraction_item_kind` (issue 211): emails.item_kind.

A schema v2 extraction says what an email is (src/extract/schema_v2.py ITEM_KINDS: a work
thread, a notification, bulk mail, a regulatory text...). Schema v35 stored what v2 says
about each item; the email's own kind stayed only in its extraction file under
data/extracted/. This step adds the column, empty: the loader fills it for each email it
loads or re-extracts from a v2 extraction, and leaves it NULL for a v1 one.

Gated, not numbered, so a deploy (scripts/deploy.sh applies numbered migrations by itself)
changes no table until the owner runs `brain maint migrate --apply-gated`; every write here
is a no-op on a store without the column. ADD COLUMN only: no row is rewritten and no
full-text index moves.
"""

import sqlite3

from src.store.schema import applied_gated

NAME = "extraction_item_kind"
COLUMN = "item_kind"


def _has_column(conn: sqlite3.Connection) -> bool:
    return COLUMN in {r[1] for r in conn.execute("PRAGMA table_info(emails)")}


def migrate(conn: sqlite3.Connection) -> None:
    """The step: emails.item_kind, once. The caller commits (schema.apply_gated), or rolls
    back for a dry run."""
    if not _has_column(conn):
        conn.execute(f"ALTER TABLE emails ADD COLUMN {COLUMN} TEXT")


def applied(conn: sqlite3.Connection) -> bool:
    """Whether this store took the step, so the loader writes the column."""
    return NAME in applied_gated(conn) and _has_column(conn)


def item_kind(extraction: dict) -> str | None:
    """A v2 extraction's item_kind when it is one of the vocabulary's; None for a v1
    extraction (known by the missing `extraction_schema: 2` mark, as the loader knows it)
    and for any other value."""
    from src.extract.schema_v2 import ITEM_KINDS

    if extraction.get("extraction_schema") != 2:
        return None
    value = extraction.get(COLUMN)
    return value if isinstance(value, str) and value in ITEM_KINDS else None
