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
     input, and are skipped), and the HTML kept in email_html, decompressed
     first: compressed, a key is invisible to a column scan and to a grep,
  2. rewrites each hit with redact_secrets under PRAGMA secure_delete=ON, in one
     transaction, re-reading the row inside it,
  3. runs FTS5 'optimize' on EVERY full-text index, hits or not, which merges its
     segments and drops the terms of deleted documents. Every index, because a
     document re-ingested or deleted since it was indexed leaves its terms behind
     with no live row to find, and because a re-run must be able to finish an
     interrupted one whose rows are already clean,
  4. checkpoints the WAL (TRUNCATE), failing if a reader blocks it, and re-scans;
     exits 1 if anything is left.

secure_delete only zeroes what is freed while it is on. Copies freed by earlier
churn (freelist pages, slack inside live pages) survive all four steps, and only
--vacuum removes them: it rewrites the whole file, needs free space of about
twice the database, and holds an exclusive lock for minutes, so stop the jobs
that write the database first.

Run it on the host that builds the database, with the writers stopped. Counts
only are printed, never a value. Defaults to a dry run, which opens the database
read-only and exits 1 when it finds anything. On a replica --apply is refused
with exit 3, since the next pull replaces the file. Snapshots taken before the scrub
still hold the old rows: the encrypted offsite ones, and the plaintext local
ones in data/backups/, both of which age out under the retention policy.
"""

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.config import DEFAULT_DB, is_replica, replica_refusal  # noqa: E402
from src.redact import _PATTERNS, redact_secrets  # noqa: E402
from src.store.email_html import pack, unpack  # noqa: E402

_FTS_SHADOW_SUFFIXES = ("_data", "_idx", "_content", "_docsize", "_config")

# How long the write connection waits on a lock. Tests shorten it.
BUSY_TIMEOUT_MS = 60000

# Columns holding zlib-compressed text (schema v23 keeps an HTML body's markup
# in email_html), read and written through src.store.email_html.
_PACKED = (("email_html", "html"),)


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


def _packed_columns(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(table, column) for every compressed column this database has."""
    tables = {
        name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    return [(table, column) for table, column in _PACKED if table in tables]


def _has_secret(value) -> bool:
    return isinstance(value, str) and any(p.search(value) for _n, p in _PATTERNS)


def _scan(conn: sqlite3.Connection) -> dict[tuple[str, str], list[int]]:
    """(table, column) -> rowids of rows whose value holds a credential."""
    found: dict[tuple[str, str], list[int]] = {}
    for table, column in _text_columns(conn) + _packed_columns(conn):
        packed = (table, column) in _PACKED
        rowids = [
            rowid
            for rowid, value in conn.execute(
                f'SELECT rowid, "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
            )
            if _has_secret(unpack(value) if packed else value)
        ]
        if rowids:
            found[(table, column)] = rowids
    return found


def _fts5_indexes(conn: sqlite3.Connection) -> list[str]:
    """Every FTS5 virtual table in the database."""
    return sorted(
        name for name, sql in _virtual_tables(conn).items() if "fts5" in (sql or "").lower()
    )


def _temp_dir_beside(conn: sqlite3.Connection, directory: Path) -> bool:
    """Point this connection's temporary files at ``directory``. False if ignored.

    Left to itself SQLite uses $SQLITE_TMPDIR, $TMPDIR, /var/tmp or /tmp, read
    once when sqlite3 is imported, so setting the variable from here is too
    late. The pragma is deprecated but compiled into the builds this runs on;
    reading it back is how an omitted one is detected.
    """
    quoted = str(directory).replace("'", "''")
    conn.execute(f"PRAGMA temp_store_directory = '{quoted}'")
    row = conn.execute("PRAGMA temp_store_directory").fetchone()
    return bool(row) and row[0] == str(directory)


def _checkpoint(conn: sqlite3.Connection) -> bool:
    """Copy the WAL back into the main file and truncate it. False if a reader blocked it.

    Until it completes, the main file still holds the pre-scrub pages, and the
    main file is what the replica pull and the snapshots copy.
    """
    busy, log_frames, checkpointed = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    return busy == 0 and log_frames == checkpointed


def _report(found: dict[tuple[str, str], list[int]]) -> None:
    if not found:
        print("No credential-shaped values found.")
        return
    for (table, column), rowids in sorted(found.items()):
        noun = "row" if len(rowids) == 1 else "rows"
        print(f"  {table}.{column}: {len(rowids)} {noun}")


def _apply(conn: sqlite3.Connection, found: dict[tuple[str, str], list[int]]) -> int:
    changed = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for (table, column), rowids in found.items():
            packed = (table, column) in _PACKED
            for rowid in rowids:
                row = conn.execute(
                    f'SELECT "{column}" FROM "{table}" WHERE rowid = ?', (rowid,)
                ).fetchone()
                value = None if row is None else unpack(row[0]) if packed else row[0]
                if value is None or not _has_secret(value):
                    continue  # changed since the scan
                clean = redact_secrets(value)
                conn.execute(
                    f'UPDATE "{table}" SET "{column}" = ? WHERE rowid = ?',
                    (pack(clean) if packed else clean, rowid),
                )
                changed += 1
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return changed


def main(argv: list[str] | None = None) -> int:
    # No --db flag on purpose: the database is the configured one (BRAIN_DATA_DIR,
    # else <repo>/data), so no command-line string ever reaches sqlite3.connect.
    # To rehearse on a copy, point BRAIN_DATA_DIR at the copy's directory.
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="rewrite the rows (default: dry run)")
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="with --apply, VACUUM afterwards to drop copies freed before the scrub",
    )
    args = parser.parse_args(argv)
    if args.vacuum and not args.apply:
        parser.error("--vacuum requires --apply")
    if args.apply and is_replica():
        # The dry run reads only; the scrub rewrites rows, and a replica's copy
        # is replaced by the next pull (see src/config.py). 3, since 1 and 2
        # already mean "found some" and "cannot run".
        print(replica_refusal("the secret scrub"), file=sys.stderr)
        return 3

    db = Path(DEFAULT_DB)
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

    if args.vacuum:
        # VACUUM builds a full temporary copy, then writes the result through
        # the WAL: about twice the database, all of it beside the database once
        # the temp directory is pointed there below.
        need = 2 * db.stat().st_size
        if shutil.disk_usage(db.parent).free < need:
            print(f"Error: --vacuum needs about {need:,} bytes free beside {db}", file=sys.stderr)
            return 2

    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_MS)}")
    conn.execute("PRAGMA secure_delete = ON")
    if args.vacuum and not _temp_dir_beside(conn, db.parent):
        conn.close()
        print(
            "Error: this SQLite build ignores PRAGMA temp_store_directory, so VACUUM's "
            "temporary copy could land on a small tmpfs. Not vacuuming.",
            file=sys.stderr,
        )
        return 2
    try:
        found = _scan(conn)
        _report(found)
        changed = _apply(conn, found) if found else 0
        print(f"{changed} rows redacted")
        for fts in _fts5_indexes(conn):
            conn.execute(f'INSERT INTO "{fts}"("{fts}") VALUES(\'optimize\')')
        print("  optimized every full-text index")
        if args.vacuum:
            conn.execute("VACUUM")
            print("  vacuumed")
        checkpointed = _checkpoint(conn)
        left = _scan(conn)
    finally:
        conn.close()
    if left:
        print("Credential-shaped values remain:", file=sys.stderr)
        _report(left)
        return 1
    if not checkpointed:
        print(
            "The WAL checkpoint was blocked by a reader, so the main file still holds "
            "pre-scrub pages. Stop the readers and re-run.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
