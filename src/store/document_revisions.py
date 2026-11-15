"""A newer revision of a document from the document inbox replaces the older one.

A document is keyed on its own bytes (attachment_pipeline._sha256_to_message_id), so each
saved revision of one file became another document beside the last: on the replica one
folder's INDEX.md was held 148 times, and search returned every edition. The owner chose to
replace (2026-11-15), as a newer session note replaces the older one
(src/export/session_notes.py).

A file is named by its folder label (reverse_scan.topic_label: the root's name and the
folder under it) and its logical name, the filename with a dated YYYYMMDDHHMM_ prefix left
out, as reverse_scan.select_files_to_ingest groups versions. document_paths (schema v43)
maps that key to the current document. With `reverse-ingest --replace-revisions`, which only
the producer's document inbox passes (sb-attachment-pass.sh):

- a file older than a revision held for its key is not stored (it leaves the inbox);
- otherwise, once stored, the revisions held before (the one mapped, and any document of
  the same label and logical name stored before the table existed) are forgotten through
  forget_documents, unless another path maps the same document;
- the key then maps to the stored document, or to none when the bytes were already held as
  a mail attachment.

This is a forget in passing, like a replaced session note's or a duplicate document's
(src/store/forget.py): it removes a superseded copy of what the store still holds, not the
content itself. The forgetting that removes content stays manual (src/store/retention.py).

`brain maint document-revisions` does the same for the revisions stored before: a dry run
(read only, so it runs on a replica) unless --apply, which needs --backup PATH.
"""

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

DOC_SENDER = "external@documents.local"
SUBJECT_PREFIX = "[Document] "
_DATED = re.compile(r"^\d{12}_(.+)$")
# The roots whose labels the backfill reads by default: those the document trees used.
DEFAULT_ROOT_LABELS = ("National", "Personal")


def logical_name(filename: str) -> str:
    """The filename with a dated YYYYMMDDHHMM_ prefix left out."""
    m = _DATED.match(filename)
    return m.group(1) if m else filename


def logical_key(label: str, filename: str) -> str:
    return f"{label}/{logical_name(filename)}"


def _table(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'document_paths'"
        ).fetchone()
        is not None
    )


def _moment(value: object) -> str:
    """A stored or file date as a UTC second, '' when unreadable."""
    if not isinstance(value, str) or not value:
        return ""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def _file_moment(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%S")


def held(conn: sqlite3.Connection, label: str, filename: str) -> dict[int, str]:
    """The documents held for this file: message_id -> date, the one document_paths maps and
    those of the same label and logical name."""
    key = logical_key(label, filename)
    out: dict[int, str] = {}
    rows = conn.execute(
        "SELECT e.message_id, e.date_received, a.filename FROM emails e"
        " JOIN attachments a ON a.email_id = e.id"
        " WHERE e.sender_address = ? AND e.subject = ?",
        (DOC_SENDER, SUBJECT_PREFIX + label),
    ).fetchall()
    for message_id, date, name in rows:
        if logical_name(name) == logical_name(filename):
            out[int(message_id)] = _moment(date)
    if _table(conn):
        row = conn.execute(
            "SELECT p.message_id, e.date_received FROM document_paths p"
            " JOIN emails e ON e.message_id = p.message_id WHERE p.path = ?",
            (key,),
        ).fetchone()
        if row:
            out[int(row[0])] = _moment(row[1])
    return out


def newer_held(conn: sqlite3.Connection, label: str, path: Path) -> bool:
    """Whether a revision held for this file is newer than it: then it is not stored."""
    mine = _file_moment(path)
    return any(date > mine for date in held(conn, label, path.name).values())


def _named_elsewhere(conn: sqlite3.Connection, message_id: int, key: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM document_paths WHERE message_id = ? AND path != ?", (message_id, key)
        ).fetchone()
        is not None
    )


def replace_older(conn: sqlite3.Connection, label: str, filename: str, result: dict) -> int:
    """After ingest_document stored (or skipped) this file: forget the revisions held before
    and map the key to the current document. How many documents were forgotten. Commits."""
    from src.extract.attachment_pipeline import FLOOR_REASON
    from src.store.forget import forget_documents

    if result.get("reason") == FLOOR_REASON:
        return 0
    key = logical_key(label, filename)
    stored = not result.get("skipped") or result.get("reason") == "already ingested"
    current = int(result["message_id"]) if stored else None
    gone = [
        m
        for m in held(conn, label, filename)
        if m != current and not _named_elsewhere(conn, m, key)
    ]
    forgotten = forget_documents(conn, gone)["emails"] if gone else 0
    if current is None:
        conn.execute("DELETE FROM document_paths WHERE path = ?", (key,))
    else:
        conn.execute(
            "INSERT INTO document_paths (path, message_id, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(path) DO UPDATE SET message_id = excluded.message_id,"
            " updated_at = excluded.updated_at",
            (key, current, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
    conn.commit()
    return forgotten


# --- the backfill -------------------------------------------------------------------------


@dataclass
class Plan:
    groups: int = 0  # files held more than once
    documents: int = 0  # documents under the labels read
    keep: dict[str, int] = field(default_factory=dict)  # key -> the newest document
    forget: list[int] = field(default_factory=list)  # message ids of the older revisions


def _under(label: str, roots: tuple[str, ...]) -> bool:
    return any(label == r or label.startswith(r + "/") for r in roots)


def plan_backfill(conn: sqlite3.Connection, roots: tuple[str, ...] = DEFAULT_ROOT_LABELS) -> Plan:
    """Group the documents under these root labels by file; the newest of each is kept."""
    groups: dict[str, list[tuple[str, int, int]]] = {}
    for email_id, message_id, date, subject, filename in conn.execute(
        "SELECT e.id, e.message_id, e.date_received, e.subject, a.filename FROM emails e"
        " JOIN attachments a ON a.email_id = e.id WHERE e.sender_address = ?"
        " AND e.subject LIKE ?",
        (DOC_SENDER, SUBJECT_PREFIX + "%"),
    ):
        label = subject[len(SUBJECT_PREFIX) :]
        if not filename or not _under(label, roots):
            continue
        groups.setdefault(logical_key(label, filename), []).append(
            (_moment(date), int(email_id), int(message_id))
        )
    plan = Plan()
    mapped = (
        dict(conn.execute("SELECT path, message_id FROM document_paths")) if _table(conn) else {}
    )
    for key, members in groups.items():
        by_document = {m[2]: m for m in members}  # an email with two files counts once
        ordered = sorted(by_document.values(), reverse=True)
        plan.documents += len(ordered)
        plan.keep[key] = ordered[0][2]
        older = [
            m[2]
            for m in ordered[1:]
            if not any(mid == m[2] and path != key for path, mid in mapped.items())
        ]
        if older:
            plan.groups += 1
            plan.forget.extend(older)
    return plan


def _item_counts(conn: sqlite3.Connection, message_ids: list[int]) -> dict[str, int]:
    """What the forgotten documents carry, for the dry run's report."""
    if not message_ids:
        return {}
    marks = ",".join("?" * len(message_ids))
    ids = f"(SELECT id FROM emails WHERE message_id IN ({marks}))"
    out = {}
    for table in ("attachments", "decisions", "action_items", "key_facts", "commitments"):
        try:
            out[table] = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE email_id IN {ids}", message_ids
            ).fetchone()[0]
        except sqlite3.OperationalError:
            continue
    return out


def cmd_document_revisions(args) -> int:
    """`brain maint document-revisions`: a dry run unless --apply, which needs --backup."""
    from src import config
    from src.store.forget import forget_documents
    from src.store.schema import ensure_schema, get_connection
    from src.store.sql_readonly import connect_read_only

    roots = tuple(args.root_label or DEFAULT_ROOT_LABELS)
    if not Path(args.db).exists():
        print(f"Error: no store at {args.db}", file=sys.stderr)
        return 1
    if args.apply:
        if config.is_replica():
            print(config.replica_refusal("maint document-revisions --apply"), file=sys.stderr)
            return 2
        if not args.backup:
            print("Error: --apply needs --backup PATH", file=sys.stderr)
            return 2
    conn = (
        get_connection(str(args.db))
        if args.apply
        else connect_read_only(str(args.db), immutable=config.is_replica())
    )
    try:
        if args.apply:
            ensure_schema(conn)
        plan = plan_backfill(conn, roots)
        print(
            f"Documents under {', '.join(roots)}: {plan.documents:,} in {len(plan.keep):,}"
            f" file(s); {plan.groups:,} held more than once, {len(plan.forget):,} older"
            f" revision(s) to forget"
        )
        for table, n in _item_counts(conn, plan.forget).items():
            print(f"  {table}: {n:,}")
        if not args.apply:
            print("Nothing changed. --apply --backup PATH forgets them.")
            return 0
        from src.cli import _gated_backup

        backup = _gated_backup(conn, args.backup)
        forgotten = forget_documents(conn, plan.forget) if plan.forget else {"emails": 0}
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.executemany(
            "INSERT INTO document_paths (path, message_id, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(path) DO UPDATE SET message_id = excluded.message_id,"
            " updated_at = excluded.updated_at",
            [(key, mid, now) for key, mid in plan.keep.items()],
        )
        conn.commit()
        print(f"Forgot {forgotten['emails']:,} document(s); mapped {len(plan.keep):,} file(s).")
        print(f"Backup of the store: {backup}")
        return 0
    finally:
        conn.close()


def _arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--root-label",
        action="append",
        help="A root's label to read (repeatable; default National and Personal)",
    )
    p.add_argument("--apply", action="store_true", help="Forget them (default: a dry run)")
    p.add_argument(
        "--backup",
        metavar="PATH",
        help="With --apply: a new file to back the store up to first, or one under an hour old",
    )


def add_maint_parsers(maint_subparsers) -> None:
    """`brain maint document-revisions`."""
    p = maint_subparsers.add_parser(
        "document-revisions",
        help="Forget the older revisions of each document stored from the document trees",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _arguments(p)
    p.set_defaults(func=cmd_document_revisions)


def main(argv: list[str] | None = None) -> int:
    from src.config import DEFAULT_DB

    p = argparse.ArgumentParser(prog="document-revisions")
    p.add_argument("--db", default=str(DEFAULT_DB))
    _arguments(p)
    return cmd_document_revisions(p.parse_args(argv))
