"""Stored mail set against live Outlook: the received times and the attachments the retired
Apple Mail exporter got wrong.

    brain maint mail-times --dry-run               # what Outlook would correct; nothing written
    brain maint mail-times                         # set them (producer)
    brain maint mail-attachments --since 2026-04-06 --until 2026-05-04 --dry-run
    brain maint mail-attachments --since 2026-04-06 --until 2026-05-04 [--limit N]

Both list Archive, Sent Items and Inbox once over the window the mail asks for (outlook-cli
list-mail, read-only), and find each stored email's live message the way refetch-garbled's
lookup does (src/export/refetch_garbled.py): by a Graph id it went by, its own or an alias, then
by its RFC822 Message-ID, then, for a message with neither, by its subject once normalised
within LOOKUP_WINDOW of its received time, in the folder it records first. Several messages
matching is never guessed at: the sender's address narrows them, and if more than one is left
the email is counted ambiguous and left alone. Listed once rather than per email, as that
lookup does, since a window of mail is thousands of emails.

mail-times. The exporter wrote a Mac's wall time with no zone; schema v41 read it as Athens
time, which 18,407 of the 18,795 rows Outlook still held on 2026-10-21 agree with, but 384 rows
of 2026-05-31..06-05 were written on a Mac in another zone and are an hour off. For a legacy
email (an integer message_id) whose RFC822 Message-ID Outlook lists, a received time that
differs from Outlook's by a whole number of hours is set to Outlook's. Only that: Mail.app and
Outlook agree to the second otherwise, and a match by subject alone is never trusted with a
time. Each email thread's newest time is then recomputed (schema.recompute_thread_last_at).
Written only while the value is the one read, so a second run changes nothing.

mail-attachments. Mail of 2026-04-06..05-03 was stored with its document attachments in about
2% of emails, against 17-20% around it: the exporter took the Archive's mail without them. For
each stored email in the window with no attachment row whose live message has attachments,
they are downloaded as the hourly export downloads them (outlook_export.
download_attachments_for_messages, into the attachments tree under the message's Graph id),
the Graph id is recorded as an alias of the email (loader.record_alias), and the files are
registered by the export's own registrar (outlook_attachments.register_downloaded_attachments),
which keys them to the email and keeps out bytes it already holds. The nightly attachment pass
then reads, summarises and embeds them like any other.

What happens to each email is kept in <data home>/state/mail_attachment_backfill.json, written
after each one, so a run can stop anywhere and the next goes on: done, no_files (the live
message has none), held_elsewhere (another stored email holds that message, a copy the
outlook-cli export stored, and its attachments go with it), duplicate (another email of this
run took the message), ambiguous, no_key (no id, Message-ID or subject to look it up by), and
gone (not listed, and older than refetch-garbled's RETENTION, the mail the mailbox keeps) are
final; not_found and failed are tried again, MAX_ATTEMPTS runs in all. An email given an
attachment row since is no longer a candidate at all. The dry run lists, matches and asks
get-mail for each matched message's attachments (no download), per week of mail.

At most MAX_CONCURRENCY outlook-cli calls run at once, the M365 limit the hourly sync keeps
to. Exit 4 when outlook-cli needs a login, 5 when it failed; a write refuses on a replica
(exit 2), the dry run reads.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from src.config import ATTACHMENTS_DIR, DATA_ROOT, is_replica, replica_refusal
from src.export.mail_reconcile import (
    DEFAULT_FOLDERS,
    NOT_MAIL,
    TruncatedListing,
    canonical_folder,
    list_folder,
    normalize_imid,
    normalize_subject,
    parse_since,
)
from src.export.outlook_cli import OutlookCliAuthRequired, OutlookCliError, run_outlook_cli
from src.export.refetch_garbled import LOOKUP_WINDOW, RETENTION, mailbox_id, received_span

logger = logging.getLogger(__name__)

LIVE_SELECT = "Id,InternetMessageId,ReceivedDateTime,Subject,From,HasAttachments"
# How far past the stored times the listing reaches: a time read in the wrong zone is hours off.
LISTING_MARGIN = timedelta(hours=14)
# M365 throttles a mailbox above two concurrent calls (ApplicationThrottled).
MAX_CONCURRENCY = 2
MAX_ATTEMPTS = 3
STATE_FILE = "mail_attachment_backfill.json"
FILE_ATTACHMENT = "#Microsoft.OutlookServices.FileAttachment"

FINAL = ("done", "no_files", "held_elsewhere", "duplicate", "ambiguous", "no_key", "gone")
RETRIED = ("not_found", "failed")


@dataclass(frozen=True)
class Live:
    """One message a listing returned."""

    id: str
    folder: str
    received: datetime | None
    imid: str
    subject: str
    sender: str
    has_attachments: bool

    @classmethod
    def of(cls, message: dict, folder: str) -> Live:
        when = message.get("ReceivedDateTime")
        try:
            received = datetime.fromisoformat(str(when).replace("Z", "+00:00")) if when else None
        except ValueError:
            received = None
        sender = ((message.get("From") or {}).get("EmailAddress") or {}).get("Address") or ""
        return cls(
            id=str(message.get("Id") or ""),
            folder=folder,
            received=received,
            imid=normalize_imid(message.get("InternetMessageId")),
            subject=normalize_subject(message.get("Subject")),
            sender=sender.strip().lower(),
            has_attachments=bool(message.get("HasAttachments")),
        )


@dataclass
class Listing:
    """The folders listed once over a window, indexed the ways a stored email is looked up."""

    by_id: dict[str, Live] = field(default_factory=dict)
    by_imid: dict[str, list[Live]] = field(default_factory=lambda: defaultdict(list))
    by_subject: dict[tuple[str, str], list[Live]] = field(default_factory=lambda: defaultdict(list))

    def add(self, message: Live) -> None:
        if not message.id or message.id in self.by_id:
            return
        self.by_id[message.id] = message
        if message.imid:
            self.by_imid[message.imid].append(message)
        if message.subject and message.received is not None:
            self.by_subject[(message.folder, message.subject)].append(message)

    @classmethod
    def fetch(cls, since: datetime, until: datetime, folders=DEFAULT_FOLDERS) -> Listing:
        listing = cls()
        for folder in folders:
            for message in list_folder(folder, since, until, select=LIVE_SELECT):
                listing.add(Live.of(message, folder))
        return listing


@dataclass
class Stored:
    """A stored email, as the lookup reads it."""

    email_id: int
    message_id: str
    received: str
    subject: str | None
    sender: str | None
    folder: str | None
    imid: str = ""
    aliases: tuple[str, ...] = ()

    @property
    def legacy(self) -> bool:
        """Stored by the retired Apple Mail exporter: its number, not a Graph id."""
        return self.message_id.isdigit()


@dataclass
class Match:
    state: str  # by_id, by_rfc822_id, by_subject_time, ambiguous, not_found, no_key
    message: Live | None = None
    candidates: int = 0


def _folders(recorded: str | None) -> list[str]:
    """The folder the email records first, then the others the listing walks."""
    first = canonical_folder(recorded or "")
    rest = [folder for folder in DEFAULT_FOLDERS if folder != first]
    return [first, *rest] if first in DEFAULT_FOLDERS else rest


def by_rfc822_id(row: Stored, listing: Listing) -> Match | None:
    """The live message with the email's Message-ID, or None when none is listed. Copies of
    one message in two folders (sent with a copy to oneself) are one message: the folder the
    email records decides, then the walk's order; two in one folder are ambiguous."""
    copies = listing.by_imid.get(row.imid) if row.imid else None
    if not copies:
        return None
    for folder in _folders(row.folder):
        here = [m for m in copies if m.folder == folder]
        if len(here) == 1:
            return Match("by_rfc822_id", here[0], len(copies))
        if here:
            return Match("ambiguous", None, len(here))
    return None


def find(row: Stored, listing: Listing) -> Match:
    """The live message a stored email is, by id, Message-ID, or subject and time (see the
    module docstring). Several messages left after narrowing by sender: ambiguous."""
    for value in (row.message_id, *row.aliases):
        mid = mailbox_id(value)
        if mid and mid in listing.by_id:
            return Match("by_id", listing.by_id[mid], 1)
    found = by_rfc822_id(row, listing)
    if found is not None:
        return found
    subject = normalize_subject(row.subject)
    span = received_span(row.received)
    if not subject or span is None:
        return Match("not_found" if row.imid else "no_key")
    low, high = span[0] - LOOKUP_WINDOW, span[1] + LOOKUP_WINDOW
    sender = (row.sender or "").strip().lower()
    for folder in _folders(row.folder):
        near = [
            m
            for m in listing.by_subject.get((folder, subject), [])
            if m.received is not None and low <= m.received <= high
        ]
        if len(near) > 1 and sender:
            near = [m for m in near if m.sender == sender] or near
        if len(near) == 1:
            return Match("by_subject_time", near[0], 1)
        if near:
            return Match("ambiguous", None, len(near))
    return Match("not_found")


# ------------------------------------------------------------------ reading the store


def _aliases(conn: sqlite3.Connection, email_ids: list[int]) -> dict[int, list[str]]:
    found: dict[int, list[str]] = defaultdict(list)
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'email_aliases'"
    ).fetchone():
        return found
    for start in range(0, len(email_ids), 500):
        chunk = email_ids[start : start + 500]
        for email_id, alias in conn.execute(
            "SELECT email_id, message_id FROM email_aliases WHERE email_id IN"
            f" ({','.join('?' * len(chunk))}) ORDER BY recorded_at DESC",
            chunk,
        ):
            found[email_id].append(str(alias))
    return found


def _in_window(received: str, since: datetime, until: datetime) -> bool:
    span = received_span(received)
    return span is not None and since <= span[0] < until


def read_mail(
    conn: sqlite3.Connection, since: datetime, until: datetime, where: str = "1"
) -> list[Stored]:
    """The stored mail received in [since, until) for which `where` holds (SQL on e), oldest
    first. Selected by text a day either side, then by instant: the store held times in UTC
    and, before schema v41, Athens wall time with no zone."""
    rows = conn.execute(
        "SELECT e.id, e.message_id, e.date_received, e.subject, e.sender_address,"
        " e.mailbox_name, e.internet_message_id FROM emails e"
        " WHERE e.date_received >= ? AND e.date_received < ?"
        " AND COALESCE(e.source_class, 'mail') IN ('mail', 'automation')"
        f" AND COALESCE(e.mailbox_name, '') NOT IN ({','.join('?' * len(NOT_MAIL))})"
        f" AND {where} ORDER BY e.date_received, e.id",
        (
            (since - timedelta(days=1)).strftime("%Y-%m-%d"),
            (until + timedelta(days=1)).strftime("%Y-%m-%d"),
            *NOT_MAIL,
        ),
    ).fetchall()
    stored = [
        Stored(
            email_id=r[0],
            message_id=str(r[1]),
            received=r[2],
            subject=r[3],
            sender=r[4],
            folder=r[5],
            imid=normalize_imid(r[6]),
        )
        for r in rows
        if _in_window(r[2], since, until)
    ]
    aliases = _aliases(conn, [s.email_id for s in stored])
    for s in stored:
        s.aliases = tuple(aliases.get(s.email_id, ()))
    return stored


def _listing_span(rows: list[Stored]) -> tuple[datetime, datetime] | None:
    spans = [span for r in rows if (span := received_span(r.received))]
    if not spans:
        return None
    return min(s[0] for s in spans) - LISTING_MARGIN, max(s[1] for s in spans) + LISTING_MARGIN


def _utc_text(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ mail-times


def plan_times(rows: list[Stored], listing: Listing) -> tuple[Counter, list[tuple]]:
    """(counts, corrections): for each legacy email with a Message-ID, whether Outlook lists
    it and agrees; a correction (email id, Outlook's time, the stored value) where the two
    differ by a whole number of hours, to the second."""
    counts: Counter = Counter()
    corrections = []
    for row in rows:
        if not row.legacy or not row.imid:
            continue
        counts["checked"] += 1
        match = by_rfc822_id(row, listing)
        span = received_span(row.received)
        if match is None or match.message is None or match.message.received is None:
            counts["ambiguous" if match is not None else "not listed"] += 1
            continue
        if span is None:
            counts["stored time unreadable"] += 1
            continue
        live = match.message.received
        off = round((span[0] - live).total_seconds())
        if off == 0:
            counts["agree"] += 1
        elif off % 3600 == 0:
            counts[f"stored {off // 3600:+d} h from Outlook"] += 1
            corrections.append((row.email_id, _utc_text(live), row.received))
        else:
            counts["differ, not by whole hours (left)"] += 1
    return counts, corrections


def apply_times(conn: sqlite3.Connection, corrections: list[tuple]) -> int:
    """Write the corrections, each only while the stored value is the one read; how many."""
    from src.store.schema import recompute_thread_last_at

    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    written = 0
    for email_id, utc, stored in corrections:
        cur = conn.execute(
            "UPDATE emails SET date_received = ? WHERE id = ? AND date_received = ?",
            (utc, email_id, stored),
        )
        written += cur.rowcount
    conn.commit()
    recompute_thread_last_at(conn)
    return written


# ------------------------------------------------------------------ mail-attachments


def state_path() -> Path:
    return DATA_ROOT / "state" / STATE_FILE


def load_state(path: Path) -> dict[str, dict]:
    """email id (as text) -> its record; an unreadable file is an empty one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    emails = data.get("emails") if isinstance(data, dict) else None
    return {str(k): v for k, v in (emails or {}).items() if isinstance(v, dict)}


def save_state(path: Path, records: dict[str, dict]) -> None:
    from src.export.state import write_json_atomic

    write_json_atomic(path, {"updated_at": _utc_text(datetime.now(UTC)), "emails": records})


def _settled(record: dict | None) -> bool:
    if not record:
        return False
    if record.get("state") in FINAL:
        return True
    return record.get("state") in RETRIED and int(record.get("attempts") or 0) >= MAX_ATTEMPTS


def _held_by(conn: sqlite3.Connection, message: Live, imids: dict[str, int]) -> set[int]:
    """The stored emails that hold this live message: by its Graph id, an alias, or its
    Message-ID. Exact, never folded: Graph ids that differ only in case are other messages."""
    held = {r[0] for r in conn.execute("SELECT id FROM emails WHERE message_id = ?", (message.id,))}
    held |= {
        r[0]
        for r in conn.execute(
            "SELECT email_id FROM email_aliases WHERE message_id = ?", (message.id,)
        )
    }
    if message.imid and message.imid in imids:
        held.add(imids[message.imid])
    return held


def _week(received: str) -> str:
    span = received_span(received)
    day = span[0].date() if span else date.min
    return (day - timedelta(days=day.weekday())).isoformat()


def _attachment_list(message_id: str) -> list[dict]:
    """The message's attachments as get-mail lists them, without their bytes."""
    found = run_outlook_cli(["get-mail", message_id, "--body", "none"])
    return [a for a in (found or {}).get("Attachments") or [] if isinstance(a, dict)]


def _download(message_id: str) -> bool:
    """Download as the hourly export does; whether it succeeded. A login lost raises."""
    from src.export.outlook_export import download_attachments_for_messages

    result = download_attachments_for_messages([{"Id": message_id, "HasAttachments": True}])
    return result.get("downloaded_messages", 0) == 1


def _register(conn: sqlite3.Connection, row: Stored, message_id: str) -> list[int]:
    """Note the Graph id as the email's alias and register the files downloaded under it."""
    from src.export.outlook_attachments import register_downloaded_attachments
    from src.store.loader import record_alias

    if message_id != row.message_id:
        record_alias(conn, message_id, row.email_id)
        conn.commit()
    stats = register_downloaded_attachments(conn, ATTACHMENTS_DIR, only=[message_id])
    return list(stats.get("ids") or [])


def backfill_attachments(
    conn: sqlite3.Connection,
    since: datetime,
    until: datetime,
    *,
    dry_run: bool,
    limit: int = 0,
    workers: int = 1,
    path: Path | None = None,
    listing: Listing | None = None,
) -> dict:
    """See the module docstring. Returns {"weeks": {monday: Counter}, "totals": Counter,
    "stopped": exit code or None, "error": text}."""
    from src.export.mail_reconcile import read_store

    path = path or state_path()
    records = load_state(path)
    where = "NOT EXISTS (SELECT 1 FROM attachments a WHERE a.email_id = e.id)"
    rows = [
        r
        for r in read_mail(conn, since, until, where)
        if not _settled(records.get(str(r.email_id)))
    ]
    weeks: dict[str, Counter] = defaultdict(Counter)
    totals: Counter = Counter()
    result: dict = {"weeks": weeks, "totals": totals, "stopped": None, "error": None}
    span = _listing_span(rows)
    if span is None:
        return result
    listing = listing or Listing.fetch(*span)
    imids = read_store(conn).imids
    taken: set[str] = set()
    now = datetime.now(UTC)
    todo: list[tuple[Stored, Live]] = []

    def note(row: Stored, state: str, **extra) -> None:
        week = weeks[_week(row.received)]
        week[state] += 1
        totals[state] += 1
        if dry_run:
            return
        before = records.get(str(row.email_id)) or {}
        attempts = int(before.get("attempts") or 0) + (1 if state in RETRIED else 0)
        records[str(row.email_id)] = {
            "state": state,
            "attempts": attempts,
            "at": _utc_text(datetime.now(UTC)),
            **extra,
        }
        save_state(path, records)

    for row in rows:
        weeks[_week(row.received)]["checked"] += 1
        totals["checked"] += 1
        match = find(row, listing)
        if match.message is None:
            state = match.state
            if state == "not_found":
                span_row = received_span(row.received)
                if span_row and span_row[1] < now - RETENTION:
                    state = "gone"
            note(row, state, candidates=match.candidates)
            continue
        weeks[_week(row.received)]["matched"] += 1
        totals["matched"] += 1
        message = match.message
        if _held_by(conn, message, imids) - {row.email_id}:
            note(row, "held_elsewhere", id=message.id)
            continue
        if message.id in taken:
            note(row, "duplicate", id=message.id)
            continue
        taken.add(message.id)
        if not message.has_attachments:
            note(row, "no_files", id=message.id)
            continue
        todo.append((row, message))

    if limit > 0:
        todo = todo[:limit]
    pool = ThreadPoolExecutor(max_workers=max(1, min(workers, MAX_CONCURRENCY)))
    try:
        work = _attachment_list if dry_run else _download
        futures = [(row, message, pool.submit(work, message.id)) for row, message in todo]
        for row, message, future in futures:
            if result["stopped"] is not None:
                future.cancel()
                continue
            try:
                outcome = future.result()
            except OutlookCliAuthRequired as e:
                result.update(stopped=4, error=f"outlook-cli needs re-authentication: {e.stderr}")
                continue
            except (OutlookCliError, subprocess.TimeoutExpired, ValueError) as e:
                logger.warning("outlook-cli failed for email %s: %s", row.email_id, e)
                note(row, "failed", id=message.id, error=str(e)[:200])
                continue
            week = weeks[_week(row.received)]
            if dry_run:
                listed = outcome if isinstance(outcome, list) else []
                files = [a for a in listed if a.get("@odata.type") == FILE_ATTACHMENT]
                inline = sum(1 for a in files if a.get("IsInline"))
                if not files:
                    note(row, "no_files", id=message.id)
                    continue
                week["with_files"] += 1
                totals["with_files"] += 1
                for key, n in (("files", len(files) - inline), ("inline", inline)):
                    week[key] += n
                    totals[key] += n
                held = sum(1 for a in files if _document_holds(conn, a))
                week["held_as_document"] += held
                totals["held_as_document"] += held
                continue
            if not outcome:
                note(row, "failed", id=message.id)
                continue
            ids = _register(conn, row, message.id)
            week["registered"] += len(ids)
            totals["registered"] += len(ids)
            note(row, "done", id=message.id, attachments=len(ids))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return result


def _document_holds(conn: sqlite3.Connection, attachment: dict) -> bool:
    """Whether a stored standalone document has a file of this name and size: the
    documents recovered from the exporter's own attachment folders hold some of these."""
    return (
        conn.execute(
            "SELECT 1 FROM attachments a JOIN emails e ON e.id = a.email_id"
            " WHERE e.source_class = 'document' AND a.filename = ? AND a.file_size = ? LIMIT 1",
            (attachment.get("Name") or "", attachment.get("Size")),
        ).fetchone()
        is not None
    )


# ------------------------------------------------------------------ the commands


def _open(db_path, dry_run: bool) -> sqlite3.Connection:
    if dry_run:
        from src.store.sql_readonly import connect_read_only

        return connect_read_only(db_path)
    from src.store.schema import get_connection, run_migrations

    conn = get_connection(str(db_path))
    run_migrations(conn)
    return conn


def _window(args) -> tuple[datetime, datetime] | None:
    now = datetime.now(UTC)
    since = parse_since(args.since, now)
    until = parse_since(args.until, now) if args.until else now
    return (since, until) if since is not None and until is not None else None


def _refuse(args, what: str) -> int | None:
    if not args.dry_run and is_replica():
        print(replica_refusal(what), file=sys.stderr)
        return 2
    if not Path(args.db).exists():
        print(f"Error: database not found: {args.db}", file=sys.stderr)
        return 1
    return None


def cmd_mail_times(args) -> int:
    """`brain maint mail-times [--since] [--until] [--dry-run]`."""
    refused = _refuse(args, "maint mail-times without --dry-run")
    if refused is not None:
        return refused
    window = _window(args)
    if window is None:
        print("Error: --since and --until take ISO dates or e.g. 365d", file=sys.stderr)
        return 1
    conn = _open(args.db, args.dry_run)
    try:
        rows = [r for r in read_mail(conn, *window) if r.legacy and r.imid]
        span = _listing_span(rows)
        try:
            listing = Listing.fetch(*span) if span else Listing()
        except OutlookCliAuthRequired as e:
            print(f"outlook-cli needs re-authentication: {e.stderr}", file=sys.stderr)
            return 4
        except (OutlookCliError, subprocess.TimeoutExpired, TruncatedListing) as e:
            print(f"outlook-cli failed: {e}", file=sys.stderr)
            return 5
        counts, corrections = plan_times(rows, listing)
        mode = "DRY RUN, nothing written" if args.dry_run else "writing"
        print(f"mail-times ({mode}): legacy mail with a Message-ID, {_span_text(window)}")
        for key in sorted(counts, key=lambda k: (k != "checked", k)):
            print(f"  {key:<40}: {counts[key]:,}")
        if not args.dry_run:
            written = apply_times(conn, corrections)
            print(f"  {'set to the time Outlook holds':<40}: {written:,}")
    finally:
        conn.close()
    return 0


def _span_text(window: tuple[datetime, datetime]) -> str:
    return f"{window[0]:%Y-%m-%d} to {window[1]:%Y-%m-%d}"


_COLUMNS = (
    ("checked", "checked"),
    ("matched", "matched"),
    ("with files", "with_files"),
    ("files", "files"),
    ("inline", "inline"),
    ("as doc", "held_as_document"),
    ("registered", "registered"),
    ("no files", "no_files"),
    ("elsewhere", "held_elsewhere"),
    ("ambiguous", "ambiguous"),
    ("duplicate", "duplicate"),
    ("no key", "no_key"),
    ("not found", "not_found"),
    ("gone", "gone"),
    ("failed", "failed"),
)


def cmd_mail_attachments(args) -> int:
    """`brain maint mail-attachments --since D [--until D] [--dry-run] [--limit N]
    [--concurrency 1|2]`."""
    refused = _refuse(args, "maint mail-attachments without --dry-run")
    if refused is not None:
        return refused
    window = _window(args)
    if window is None:
        print("Error: --since and --until take ISO dates or e.g. 30d", file=sys.stderr)
        return 1
    if args.limit < 0 or args.concurrency not in (1, 2):
        print("Error: --limit is 0 or more, --concurrency 1 or 2", file=sys.stderr)
        return 1
    conn = _open(args.db, args.dry_run)
    try:
        result = backfill_attachments(
            conn,
            *window,
            dry_run=args.dry_run,
            limit=args.limit,
            workers=args.concurrency,
        )
    except OutlookCliAuthRequired as e:
        print(f"outlook-cli needs re-authentication: {e.stderr}", file=sys.stderr)
        return 4
    except (OutlookCliError, subprocess.TimeoutExpired, TruncatedListing) as e:
        print(f"outlook-cli failed: {e}", file=sys.stderr)
        return 5
    finally:
        conn.close()
    mode = "DRY RUN, nothing downloaded or written" if args.dry_run else "downloading"
    print(f"mail-attachments ({mode}): mail with no attachment row, {_span_text(window)}")
    shown = [(label, key) for label, key in _COLUMNS if result["totals"].get(key)]
    shown = shown or list(_COLUMNS[:2])
    print("  week of     " + "".join(f"{label:>11}" for label, _ in shown))
    for week in sorted(result["weeks"]):
        counts = result["weeks"][week]
        print(f"  {week}  " + "".join(f"{counts.get(key, 0):>11,}" for _, key in shown))
    totals = result["totals"]
    print("  total       " + "".join(f"{totals.get(key, 0):>11,}" for _, key in shown))
    if not args.dry_run:
        print(f"  state: {state_path()}")
    if result["stopped"] is not None:
        print(result["error"], file=sys.stderr)
        return result["stopped"]
    return 5 if totals.get("failed") else 0


def add_maint_parsers(maint_subparsers) -> None:
    """mail-times and mail-attachments, under `brain maint`."""
    import argparse

    times = maint_subparsers.add_parser(
        "mail-times",
        help="Set the received times the Apple Mail exporter wrote in another zone from "
        "Outlook (a dry run reads only)",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    times.add_argument("--since", default="365d", help="ISO date or e.g. 365d (default 365d)")
    times.add_argument("--until", default=None, help="ISO date or e.g. 1d (default now)")
    times.add_argument("--dry-run", action="store_true", help="Count only; write nothing")
    times.set_defaults(func=cmd_mail_times)

    attachments = maint_subparsers.add_parser(
        "mail-attachments",
        help="Download and register the attachments of stored mail that has none, from "
        "Outlook (a dry run lists only)",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    attachments.add_argument("--since", required=True, help="ISO date or e.g. 30d")
    attachments.add_argument("--until", default=None, help="ISO date (default now)")
    attachments.add_argument("--dry-run", action="store_true", help="List only; write nothing")
    attachments.add_argument(
        "--limit", type=int, default=0, help="Messages to download in this run (0: all)"
    )
    attachments.add_argument(
        "--concurrency", type=int, default=1, help="outlook-cli calls at once, 1 or 2"
    )
    attachments.set_defaults(func=cmd_mail_attachments)
