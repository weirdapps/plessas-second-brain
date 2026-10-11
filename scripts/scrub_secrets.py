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

  1. scans every TEXT column of every content table with src/redact.py
     (generated columns and FTS shadow tables are derived, not stored input, and
     are skipped, and so are attachment file names and paths, which name files
     on disk), and the HTML kept in email_html, decompressed first: compressed, a
     key is invisible to a column scan and to a grep,
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
read-only and exits 1 when it finds anything. Either mode exits 2 when a kept
HTML value cannot be decompressed: it scrubs everything else, but cannot say the
database is clean. On a replica --apply is refused
with exit 3, since the next pull replaces the file. Snapshots taken before the scrub
still hold the old rows: the encrypted offsite ones, and the plaintext local
ones in data/backups/, both of which age out under the retention policy.

src/redact.py masks card numbers, IBANs and password values as well since
2026-10-11, and this scrub masks whatever it masks. A row counts as a hit when
redact_secrets would change it, never because a pattern matched: the card
pattern matches every long number, and only the checks behind it say which are
cards.

--files DIR [DIR ...] scrubs files instead of the database: the staged batches,
the extracted outputs and notes kept beside it hold the same text. Every .json
file under each DIR is masked as JSON (values only, as staging is), every .md,
.txt and .csv file as text; symbolic links are not followed. Each file is
rewritten whole (written beside itself, flushed, then renamed over itself, its
permissions kept) or not at all. A file with other hard links is not rewritten,
since the other links would keep the old text. Stop the jobs that write the
directories first. The dry run is the default here too, and the exit codes are
the same: 1 for values found or left, 2 for a file that could not be read or
written, 3 for --apply on a replica.
"""

import argparse
import contextlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.config import DEFAULT_DB, is_replica, replica_refusal  # noqa: E402
from src.redact import redact_payload, redact_secrets  # noqa: E402
from src.store.email_html import pack, unpack  # noqa: E402

_FTS_SHADOW_SUFFIXES = ("_data", "_idx", "_content", "_docsize", "_config")

# How long the write connection waits on a lock. Tests shorten it.
BUSY_TIMEOUT_MS = 60000

# Columns holding zlib-compressed text (schema v23 keeps an HTML body's markup
# in email_html), read and written through src.store.email_html.
_PACKED = (("email_html", "html"),)

# Columns that name a file on disk. The attachment registrar knows a file by its
# name, so a masked name no longer matches the file, which is registered and read
# again, and a masked path loses it. The file keeps its name either way.
_FILE_NAMES = (("attachments", "filename"), ("attachments", "file_path"))

# What --files rewrites, JSON as JSON and the rest as text, all UTF-8.
_FILE_SUFFIXES = (".json", ".md", ".txt", ".csv")
_MARK = "[REDACTED:"


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
            # hidden != 0: generated, not stored input
            if hidden == 0 and is_text and (table, column) not in _FILE_NAMES:
                targets.append((table, column))
    return targets


def _packed_columns(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(table, column) for every compressed column this database has."""
    tables = {
        name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    return [(table, column) for table, column in _PACKED if table in tables]


def _has_secret(value) -> bool:
    return isinstance(value, str) and redact_secrets(value) != value


def _unpacked(value) -> str | None:
    """A compressed value's text, or None for one that is not zlib, not UTF-8
    inside, or not bytes at all."""
    try:
        return unpack(value)
    except (zlib.error, UnicodeDecodeError, TypeError):
        return None


def _scan(
    conn: sqlite3.Connection,
) -> tuple[dict[tuple[str, str], list[int]], dict[str, list[int]]]:
    """(table, column) -> rowids of rows whose value holds a credential, and
    table -> rowids of the compressed values that could not be read."""
    found: dict[tuple[str, str], list[int]] = {}
    unreadable: dict[str, list[int]] = {}
    for table, column in _text_columns(conn) + _packed_columns(conn):
        packed = (table, column) in _PACKED
        rowids = []
        for rowid, value in conn.execute(
            f'SELECT rowid, "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
        ):
            text = _unpacked(value) if packed else value
            if packed and text is None:
                unreadable.setdefault(table, []).append(rowid)
            elif _has_secret(text):
                rowids.append(rowid)
        if rowids:
            found[(table, column)] = rowids
    return found, unreadable


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


def _report_unreadable(unreadable: dict[str, list[int]]) -> None:
    """Counts and rowids, which are no secret, so the rows can be fixed or deleted."""
    for table, rowids in sorted(unreadable.items()):
        noun = "row" if len(rowids) == 1 else "rows"
        shown = ", ".join(str(r) for r in rowids[:20]) + (" ..." if len(rowids) > 20 else "")
        print(
            f"  {len(rowids)} {table} {noun} could not be read, so were not checked "
            f"(rowid {shown})",
            file=sys.stderr,
        )


def _report(found: dict[tuple[str, str], list[int]], unread: bool = False, file=None) -> None:
    if not found:
        if not unread:  # an unread row may hold one
            print("No credential-shaped values found.", file=file)
        return
    for (table, column), rowids in sorted(found.items()):
        noun = "row" if len(rowids) == 1 else "rows"
        print(f"  {table}.{column}: {len(rowids)} {noun}", file=file)


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
                value = None if row is None else _unpacked(row[0]) if packed else row[0]
                clean = None if value is None else redact_secrets(value)
                if clean is None or clean == value:
                    continue  # changed since the scan
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


class _Unreadable(Exception):
    """A file that is not UTF-8, or not JSON where its name says it is."""


def _counted(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _listed(paths: list[Path]) -> str:
    """Up to twenty paths: a name is no secret, and says which file to fix."""
    return ", ".join(str(p) for p in paths[:20]) + (" ..." if len(paths) > 20 else "")


def _files_under(directory: Path, unreadable: list[Path]) -> list[Path]:
    """The files --files reads under `directory`, symbolic links left out. A
    directory that cannot be listed goes on `unreadable`, not unnoticed."""
    found = []
    for parent, _dirs, names in os.walk(
        directory, onerror=lambda e: unreadable.append(Path(e.filename))
    ):
        for name in names:
            path = Path(parent) / name
            if path.suffix.lower() in _FILE_SUFFIXES and not path.is_symlink():
                found.append(path)
    return sorted(found)


def _masked_file(path: Path) -> tuple[str, int] | None:
    """The file's text with every value masked, and how many were, or None when it
    holds none. A JSON file is masked value by value and written back in the
    layout it had, so it stays JSON. Raises _Unreadable."""
    try:
        with open(path, encoding="utf-8", newline="") as f:
            raw = f.read()
        if path.suffix.lower() != ".json":
            before, clean = raw, redact_secrets(raw)
            text = clean
        else:
            data = json.loads(raw)
            before = json.dumps(data, ensure_ascii=False)
            masked = redact_payload(data)
            clean = json.dumps(masked, ensure_ascii=False)
            indent = 2 if "\n" in raw.strip() else None
            text = json.dumps(masked, ensure_ascii=False, indent=indent)
            text += "\n" if raw.endswith("\n") else ""
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as e:
        raise _Unreadable(path) from e
    if clean == before:
        return None
    # One marker per value masked; a password value that held a marker swaps one.
    return text, max(clean.count(_MARK) - before.count(_MARK), 1)


def _write_atomically(path: Path, text: str) -> None:
    """Replace the file with `text` as write_json_atomic does: written beside it,
    flushed to disk, then renamed over it, so a reader sees the old file or the
    new one. Its permission bits are kept, since a new file would take the umask."""
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _scan_files(
    directories: list[Path], apply: bool
) -> tuple[dict[Path, tuple[int, int]], dict[str, list[Path]]]:
    """Per directory, (files, values) found, or rewritten with apply; and the files
    that could not be read, written, or were left for their other hard links."""
    found: dict[Path, tuple[int, int]] = {}
    problems: dict[str, list[Path]] = {"unreadable": [], "unwritable": [], "linked": []}
    for directory in directories:
        files = values = 0
        for path in _files_under(directory, problems["unreadable"]):
            try:
                result = _masked_file(path)
            except _Unreadable:
                problems["unreadable"].append(path)
                continue
            if result is None:
                continue
            if apply:
                try:
                    if path.stat().st_nlink > 1:
                        problems["linked"].append(path)
                        continue
                    _write_atomically(path, result[0])
                except OSError:
                    problems["unwritable"].append(path)
                    continue
            files += 1
            values += result[1]
        if files:
            found[directory] = (files, values)
    return found, problems


def _report_files(found: dict[Path, tuple[int, int]], file=None) -> None:
    for directory, (files, values) in found.items():
        print(f"  {directory}: {_counted(files, 'file')}, {_counted(values, 'value')}", file=file)


def _scrub_files(directories: list[Path], apply: bool) -> int:
    """--files: the dry run, or the rewrite and a scan after it. Counts and file
    names only are printed, never a value."""
    missing = [d for d in directories if not d.is_dir()]
    for d in missing:
        print(f"Error: no directory at {d}", file=sys.stderr)
    if missing:
        return 2
    found, problems = _scan_files(directories, apply)
    _report_files(found)
    if apply:
        files = sum(f for f, _ in found.values())
        values = sum(v for _, v in found.values())
        print(f"{_counted(files, 'file')} rewritten, {_counted(values, 'value')} masked")
        found, rescan = _scan_files(directories, apply=False)
        problems["unreadable"] = rescan["unreadable"]
        if found:
            print("Credential-shaped values remain:", file=sys.stderr)
            _report_files(found, file=sys.stderr)
    elif not found and not problems["unreadable"]:
        print("No credential-shaped values found.")
    for kind, one, many in (
        (
            "linked",
            "file has other hard links, so was not rewritten",
            "files have other hard links, so were not rewritten",
        ),
        ("unwritable", "file could not be written", "files could not be written"),
        (
            "unreadable",
            "file could not be read, so was not checked",
            "files could not be read, so were not checked",
        ),
    ):
        paths = problems[kind]
        if paths:
            what = one if len(paths) == 1 else many
            print(f"  {len(paths)} {what} ({_listed(paths)})", file=sys.stderr)
    if problems["unreadable"] or problems["unwritable"]:
        return 2
    return 1 if found else 0


def main(argv: list[str] | None = None) -> int:
    # No --db flag on purpose: the database is the configured one (BRAIN_DATA_DIR,
    # else <repo>/data), so no command-line string ever reaches sqlite3.connect.
    # To rehearse on a copy, point BRAIN_DATA_DIR at the copy's directory.
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="rewrite the rows (default: dry run)")
    parser.add_argument("--dry-run", action="store_true", help="count only, the default")
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="with --apply, VACUUM afterwards to drop copies freed before the scrub",
    )
    parser.add_argument(
        "--files",
        nargs="+",
        type=Path,
        metavar="DIR",
        help="scrub the .json, .md, .txt and .csv files under each DIR, not the database",
    )
    args = parser.parse_args(argv)
    if args.vacuum and not args.apply:
        parser.error("--vacuum requires --apply")
    if args.apply and args.dry_run:
        parser.error("--dry-run and --apply exclude each other")
    if args.files and args.vacuum:
        parser.error("--vacuum is for the database, not --files")
    if args.apply and is_replica():
        # The dry run reads only; the scrub rewrites rows, and a replica's copy
        # is replaced by the next pull (see src/config.py). 3, since 1 and 2
        # already mean "found some" and "cannot run".
        print(replica_refusal("the secret scrub"), file=sys.stderr)
        return 3
    if args.files:
        return _scrub_files(args.files, args.apply)

    db = Path(DEFAULT_DB)
    if not db.exists():
        print(f"Error: no database at {db}", file=sys.stderr)
        return 2

    if not args.apply:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            found, unreadable = _scan(conn)
        finally:
            conn.close()
        _report(found, unread=bool(unreadable))
        if unreadable:
            _report_unreadable(unreadable)
            return 2
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
        found, first_unread = _scan(conn)
        _report(found, unread=bool(first_unread))
        changed = _apply(conn, found) if found else 0
        print(f"{changed} rows redacted")
        for fts in _fts5_indexes(conn):
            conn.execute(f'INSERT INTO "{fts}"("{fts}") VALUES(\'optimize\')')
        print("  optimized every full-text index")
        if args.vacuum:
            conn.execute("VACUUM")
            print("  vacuumed")
        checkpointed = _checkpoint(conn)
        left, unreadable = _scan(conn)
    finally:
        conn.close()
    # Every warning first, then one exit code: 2 for an unread row wins, since
    # the run cannot vouch for what it could not read.
    if left:
        print("Credential-shaped values remain:", file=sys.stderr)
        _report(left, file=sys.stderr)
    if unreadable:
        _report_unreadable(unreadable)
    if not checkpointed:
        print(
            "The WAL checkpoint was blocked by a reader, so the main file still holds "
            "pre-scrub pages. Stop the readers and re-run.",
            file=sys.stderr,
        )
    if unreadable:
        return 2
    return 1 if left or not checkpointed else 0


if __name__ == "__main__":
    sys.exit(main())
