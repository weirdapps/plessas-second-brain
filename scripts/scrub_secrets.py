#!/usr/bin/env python3
"""Remove credentials that reached brain.db before the ingest path redacted them.

src/redact.py has redacted every staging batch since #55 (2026-09-09), but only
going forward. On 2026-09-23 the replica still held 24 conversation turns with
Anthropic, Google and Telegram keys, 5 email bodies and 9 attachment texts, all
older than the fix. Those rows are in every replica and every snapshot since, and
reach Vertex again whenever they are re-extracted.

Rewriting the row is not enough on its own. The old bytes survive in freed pages
unless secure_delete is on, and the full-text index keeps every term of a deleted
document in its old segments until they are merged. So --apply:

  1. scans every TEXT column of every content table with the src/redact.py
     patterns (generated columns and FTS shadow tables are derived, not stored
     input, and are skipped),
  2. rewrites each hit with redact_secrets under PRAGMA secure_delete=ON, in one
     transaction, re-reading the row inside it,
  3. runs FTS5 'optimize' on each full-text index whose content table changed,
     which merges its segments and drops the deleted terms,
  4. checkpoints the WAL (TRUNCATE) and re-scans; exits 1 if anything is left.

Counts only are printed, never a value. Defaults to a dry run, which opens the
database read-only and exits 1 when it finds anything. Offsite snapshots taken
before the scrub still hold the old rows; they are encrypted, and they age out
under the retention policy.
"""

import argparse
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.config import DEFAULT_DB  # noqa: E402
from src.redact import _PATTERNS, redact_secrets  # noqa: E402

_FTS_SHADOW_SUFFIXES = ("_data", "_idx", "_content", "_docsize", "_config")
_CONTENT_OPTION = re.compile(r"content\s*=\s*'([^']+)'", re.IGNORECASE)


def _virtual_tables(conn: sqlite3.Connection) -> dict[str, str]:
    """name -> CREATE statement for every virtual table."""
    return dict(
        conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'table' AND sql LIKE 'CREATE VIRTUAL TABLE%'"
        )
    )


def _text_columns(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(table, column) for every stored TEXT column of every content table."""
    virtual = _virtual_tables(conn)
    shadows = {v + s for v in virtual for s in _FTS_SHADOW_SUFFIXES}
    targets = []
    for (table,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ):
        if table in virtual or table in shadows:
            continue
        for _cid, column, decl, _notnull, _default, _pk, hidden in conn.execute(
            f"PRAGMA table_xinfo('{table}')"
        ):
            declared = (decl or "").upper()
            is_text = declared in ("", "TEXT") or "CHAR" in declared or "CLOB" in declared
            if hidden == 0 and is_text:  # hidden != 0: generated, not stored input
                targets.append((table, column))
    return targets


def _has_secret(value) -> bool:
    return isinstance(value, str) and any(p.search(value) for _n, p in _PATTERNS)


def _scan(conn: sqlite3.Connection) -> dict[tuple[str, str], list[int]]:
    """(table, column) -> rowids of rows whose value holds a credential."""
    found: dict[tuple[str, str], list[int]] = {}
    for table, column in _text_columns(conn):
        rowids = [
            rowid
            for rowid, value in conn.execute(
                f'SELECT rowid, "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
            )
            if _has_secret(value)
        ]
        if rowids:
            found[(table, column)] = rowids
    return found


def _fts_indexes_over(conn: sqlite3.Connection, tables: set[str]) -> list[str]:
    """External-content FTS indexes whose content table is in ``tables``."""
    indexes = []
    for name, sql in _virtual_tables(conn).items():
        match = _CONTENT_OPTION.search(sql or "")
        if "fts5" in (sql or "").lower() and match and match.group(1) in tables:
            indexes.append(name)
    return sorted(indexes)


def _report(found: dict[tuple[str, str], list[int]]) -> None:
    if not found:
        print("No credential-shaped values found.")
        return
    for (table, column), rowids in sorted(found.items()):
        noun = "row" if len(rowids) == 1 else "rows"
        print(f"  {table}.{column}: {len(rowids)} {noun}")


def _apply(conn: sqlite3.Connection, found: dict[tuple[str, str], list[int]]) -> int:
    conn.execute("PRAGMA secure_delete = ON")
    changed = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for (table, column), rowids in found.items():
            for rowid in rowids:
                row = conn.execute(
                    f'SELECT "{column}" FROM "{table}" WHERE rowid = ?', (rowid,)
                ).fetchone()
                if row is None or not _has_secret(row[0]):
                    continue  # changed since the scan
                conn.execute(
                    f'UPDATE "{table}" SET "{column}" = ? WHERE rowid = ?',
                    (redact_secrets(row[0]), rowid),
                )
                changed += 1
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    for fts in _fts_indexes_over(conn, {table for table, _column in found}):
        conn.execute(f'INSERT INTO "{fts}"("{fts}") VALUES(\'optimize\')')
        conn.commit()
        print(f"  optimized {fts}")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--db", default=str(DEFAULT_DB), help="path to brain.db")
    parser.add_argument("--apply", action="store_true", help="rewrite the rows (default: dry run)")
    args = parser.parse_args(argv)

    db = Path(args.db)
    if not db.exists():
        print(f"Error: no database at {db}", file=sys.stderr)
        return 2

    if not args.apply:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            found = _scan(conn)
        finally:
            conn.close()
        _report(found)
        return 1 if found else 0

    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("PRAGMA busy_timeout = 60000")
    try:
        found = _scan(conn)
        _report(found)
        changed = _apply(conn, found) if found else 0
        print(f"{changed} rows redacted")
        left = _scan(conn)
    finally:
        conn.close()
    if left:
        print("Credential-shaped values remain:", file=sys.stderr)
        _report(left)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
