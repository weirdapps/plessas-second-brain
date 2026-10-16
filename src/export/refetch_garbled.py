"""Fetch again from the mailbox the swept originals of OCR texts marked as garbled Greek.

    python -m src.cli refetch-garbled --dry-run     # count what it would ask for; change nothing
    python -m src.cli refetch-garbled --limit 200   # ask for up to 200 messages, oldest mail first
    python -m src.cli refetch-garbled --no-lookup   # ask by id only

`ocr-regarble --apply` marks the stored OCR texts that are Greek read as Latin look-alike letters
(src/extract/ocr_regarble.py), and a marked text is read better only from its file: again, Greek
first, by `reextract --garbled`, or by the image pass's transcription. The audit found most of
those files already swept (data-quality-03). This asks outlook-cli for the attachments of their
messages again and puts back each file whose bytes are a marked row's.

Chosen: rows with the mark (file_sweep.OCR_GARBLED_SQL) and a file path, not a 'vision' row,
whose file is not on disk (locate_file), grouped by email. The oldest mail goes first: the
mailbox keeps about a year of it (audit code-ingest-03), so that is the next to go. `--limit`
bounds the messages a run asks for, both halves together: the lookup gets what the id half leaves.

A message is asked for under each id it went by until one answers: the directory its rows' files
were downloaded into, the email's own id, then the ids of its copies (email_aliases), newest
first, since a move to Archive gives a message a new id. Only a Graph id is asked for: the
numbers the retired Apple Mail exporter stored are not ids the mailbox knows, and a message with
none goes to the lookup. At most MAX_CONCURRENCY downloads run at once, the M365 limit the hourly
sync also keeps to.

The lookup, the second half (`--no-lookup` turns it off), finds a message with no mailbox id the
way `mail-reconcile` matches mail (src/export/mail_reconcile.py): it lists a folder over a window
of received time and compares subjects once normalised. The window is the email's stored received
time LOOKUP_WINDOW either side, a stored time with no zone read as NAIVE_TIME_ZONE's. The folder is
the one the email records, when the reconcile walks it, then the others it walks, since mail filed
from the Inbox has moved to Archive; the first folder with a match decides. Exactly one message:
its attachments are downloaded, and a row's file is put back only if its sha256 is the row's, so
a row with no hash is not looked up, nor is an email with no subject or time. Several: none is
guessed at. At most MAX_CONCURRENCY outlook-cli calls run at once here too.

What happens to each row, recorded in sync_metadata under file_sweep.REFETCH_KEY + attachment id
as JSON {state, attempts, last_attempt_at, and fetched_at or gone_at}; a record the lookup wrote
also says "by": "lookup", and in which folder the message matched:

    fetched    the download held a file with the row's sha256 (a row with no hash: its name and
               size). It is copied to the directory and name the row's path records, under the
               attachments root; attachments.file_path names it there, since the image pass
               reads that path, file_removed_at is cleared, and a missing hash is recorded. The
               mark stays, so `reextract --garbled` takes the row, and the sweep keeps the file
               until a reading of it is recorded after the fetch (file_sweep.REFETCHED_UNREAD_SQL)
    unmatched  the download held no such file. Not asked for again
    gone       every id answered 404: the mailbox no longer holds the message. For the lookup:
               nothing matched, and the email is older than RETENTION, the mail the mailbox
               keeps. Dated, and not asked for again
    ambiguous  the lookup matched several messages. Not looked up again
    not_found  the lookup matched nothing, and the email is younger than RETENTION: looked up
               again on later runs, MAX_ATTEMPTS attempts in all, then given up
    failed     outlook-cli failed otherwise (exit 5, or no answer within DOWNLOAD_TIMEOUT_S, or
               a listing's LIST_TIMEOUT_S): asked for again on later runs, MAX_ATTEMPTS attempts
               in all, then given up

A message's other files are downloaded into a temporary directory and not kept. outlook-cli exit
4 (re-authenticate) stops the run, and so does any exit but 5 (no outlook-cli, a usage or local
error), which no retry would mend: no attempt is counted, what finished is kept, the lookup does
not start, and the command exits 4 or 5. A row already fetched is skipped, so a run can be
repeated; delete its sync_metadata row to fetch it again. Producer only: the CLI refuses it on a
replica.
"""

import contextlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.config import ATTACHMENTS_DIR, DEFAULT_DB
from src.export.mail_reconcile import (
    DEFAULT_FOLDERS,
    LIST_TIMEOUT_S,
    NOT_MAIL,
    canonical_folder,
    list_folder,
    normalize_subject,
)
from src.export.outlook_cli import OutlookCliAuthRequired, OutlookCliError, run_outlook_cli
from src.store.file_hashes import locate_file, sha256_of_file
from src.store.file_sweep import OCR_GARBLED_SQL, REFETCH_KEY, SWEEP_INDEX
from src.store.schema import get_connection
from src.store.stamps import utc_now

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 200
MAX_ATTEMPTS = 3
# M365 throttles a mailbox above two concurrent calls (ApplicationThrottled).
MAX_CONCURRENCY = 2
# One call brings down every attachment of a message; a large scan outlasts the adapter's 60 s.
DOWNLOAD_TIMEOUT_S = 300

# The lookup. How far from the email's stored received time the message may lie: read in the
# right zone the two agree to the second (34 of 39 messages both exporters stored, audit
# 2026-10-11). The margin allows for a clock that drifted, and stays narrow because a scanner
# sends bursts of mail under one subject, which would all match.
LOOKUP_WINDOW = timedelta(minutes=2)
# The zone of a stored time that carries none: the retired Apple Mail exporter wrote its Mac's
# local time. Read as UTC, as mail_reconcile.parse_when reads it, it is two or three hours off.
NAIVE_TIME_ZONE = ZoneInfo("Europe/Athens")
# The mail the mailbox keeps (audit code-ingest-03: nothing older was listed). Older mail that no
# listing finds is gone; younger is looked for again.
RETENTION = timedelta(days=365)

FETCHED = "fetched"
UNMATCHED = "unmatched"
GONE = "gone"
FAILED = "failed"
STOPPED = "stopped"
AMBIGUOUS = "ambiguous"
NOT_FOUND = "not_found"

# A Graph message id is a long base64 string. Mail.app's numbers, and the negative ones documents
# carry, are not ids the mailbox knows.
_GRAPH_ID = re.compile(r"[A-Za-z0-9+/=_-]{20,}")

COUNTS = (
    "garbled",
    "on_disk",
    "already_fetched",
    "gone_before",
    "unmatched_before",
    "ambiguous_before",
    "given_up",
    "no_mailbox_id",
    "to_try",
    "messages_to_try",
    "messages_tried",
    "found",
    "fetched",
    "unmatched",
    "gone",
    "gone_files",
    "failed",
    "failed_files",
    "left",
)
# The lookup's own, in stats["lookup"], over the rows the id half found no mailbox id for.
LOOKUP_COUNTS = (
    "no_key",
    "to_try",
    "messages_to_try",
    "messages_tried",
    "found",
    "fetched",
    "unmatched",
    "ambiguous",
    "ambiguous_files",
    "gone",
    "gone_files",
    "not_found",
    "not_found_files",
    "failed",
    "failed_files",
    "left",
)
# What marks a record the lookup wrote.
LOOKED_UP = {"by": "lookup"}

NEXT_STEPS = """\
Next: `reextract --garbled` reads the fetched files again, Greek first, and the nightly image pass
(or `transcribe-images`) transcribes the fetched content images. Run this again until it has
nothing left to try."""


def mailbox_id(value) -> str | None:
    """`value` as an id the mailbox can be asked for, or None when it is not a Graph id."""
    text = "" if value is None else str(value).strip()
    if not _GRAPH_ID.fullmatch(text) or text.lstrip("-").isdigit():
        return None
    return text


@dataclass(frozen=True)
class Row:
    attachment_id: int
    file_path: str
    filename: str
    file_size: int | None
    sha256: str | None
    attempts: int = 0  # failed attempts on earlier runs

    @property
    def directory(self) -> str:
        return Path(self.file_path).parent.name

    @property
    def name(self) -> str:
        return Path(self.file_path).name


@dataclass
class Message:
    email_id: int | None
    email_message_id: object = None
    rows: list[Row] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)
    # What the lookup goes by: the folder, received time and subject the email records.
    folder: str | None = None
    received: str | None = None
    subject: str | None = None


@dataclass
class Outcome:
    state: str
    # attachment id -> (where its file now is, its sha256), for the rows whose file was put back
    placed: dict[int, tuple[Path, str]] = field(default_factory=dict)
    via: str | None = None
    error: str | None = None
    exit_code: int | None = None
    # Added to each row's record: what the lookup notes.
    note: dict = field(default_factory=dict)


def fetch_states(conn: sqlite3.Connection) -> dict[int, dict]:
    """The record kept for each attachment id. A record that is not a JSON object is passed over."""
    states: dict[int, dict] = {}
    for key, value in conn.execute(
        "SELECT key, value FROM sync_metadata WHERE key GLOB ?", (REFETCH_KEY + "*",)
    ):
        try:
            att_id = int(key[len(REFETCH_KEY) :])
            record = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(record, dict):
            states[att_id] = record
    return states


def _has(conn: sqlite3.Connection, kind: str, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?", (kind, name)
        ).fetchone()
        is not None
    )


def _marked(conn: sqlite3.Connection) -> list[tuple]:
    """The marked rows with a file path, oldest mail first. The mark sits behind extracted_text
    in attachment_content, so the sweep index (schema v32) answers the choice: through the table
    it read every stored text, 3 GB on the producer. A store from before v32 lacks the index, and
    naming it there would be an error."""
    hint = f" INDEXED BY {SWEEP_INDEX}" if _has(conn, "index", SWEEP_INDEX) else ""
    return conn.execute(
        "SELECT a.id, a.email_id, a.file_path, a.filename, a.file_size, a.sha256, e.message_id,"
        " e.mailbox_name, e.date_received, e.subject"
        f" FROM attachment_content ac{hint}"
        " JOIN attachments a ON a.id = ac.attachment_id"
        " LEFT JOIN emails e ON e.id = a.email_id"
        f" WHERE {OCR_GARBLED_SQL} AND COALESCE(ac.extraction_method, '') != 'vision'"
        " AND a.file_path IS NOT NULL AND a.file_path NOT LIKE 'text:%'"
        " ORDER BY COALESCE(e.date_received, ''), a.id"
    ).fetchall()


def _attempts(record: dict) -> int:
    try:
        return int(record.get("attempts") or 0)
    except (TypeError, ValueError):
        return 0


def plan(conn: sqlite3.Connection, root: Path, stats: dict) -> tuple[list[Message], list[Message]]:
    """The messages to ask for, oldest mail first, each with the ids it may answer to, and the
    messages with no id the mailbox knows, for the lookup. Counts in `stats` the rows passed
    over, and why."""
    states = fetch_states(conn)
    groups: dict[tuple, Message] = {}
    for (
        att_id,
        email_id,
        file_path,
        filename,
        file_size,
        sha256,
        email_message_id,
        folder,
        received,
        subject,
    ) in _marked(conn):
        stats["garbled"] += 1
        record = states.get(att_id, {})
        state = record.get("state")
        if state == FETCHED:
            stats["already_fetched"] += 1
            continue
        if locate_file(file_path, root) is not None:
            stats["on_disk"] += 1
            continue
        if state == GONE:
            stats["gone_before"] += 1
            continue
        if state == UNMATCHED:
            stats["unmatched_before"] += 1
            continue
        if state == AMBIGUOUS:
            stats["ambiguous_before"] += 1
            continue
        if state in (FAILED, NOT_FOUND) and _attempts(record) >= MAX_ATTEMPTS:
            stats["given_up"] += 1
            continue
        row = Row(att_id, file_path, filename, file_size, sha256, _attempts(record))
        key = ("email", email_id) if email_id is not None else ("directory", row.directory)
        message = Message(
            email_id, email_message_id, folder=folder, received=received, subject=subject
        )
        groups.setdefault(key, message).rows.append(row)

    aliases = _has(conn, "table", "email_aliases")
    messages = []
    no_id = []
    for message in groups.values():
        values: list = [row.directory for row in message.rows]
        if message.email_id is not None:
            values.append(message.email_message_id)
            if aliases:
                values += [
                    alias
                    for (alias,) in conn.execute(
                        "SELECT message_id FROM email_aliases WHERE email_id = ?"
                        " ORDER BY recorded_at DESC",
                        (message.email_id,),
                    )
                ]
        for value in values:
            mid = mailbox_id(value)
            if mid and mid not in message.ids:
                message.ids.append(mid)
        if not message.ids:
            stats["no_mailbox_id"] += len(message.rows)
            no_id.append(message)
            continue
        messages.append(message)
    return messages, no_id


def _not_found(e: OutlookCliError) -> bool:
    """Whether the mailbox holds no message under the id: HTTP 404, which outlook-cli reports as
    exit 5 with the status in the JSON error it writes to stderr."""
    if e.exit_code != 5:
        return False
    try:
        error = json.loads(e.stderr)["error"]
        if error.get("httpStatus") == 404 or error.get("code") == "UPSTREAM_HTTP_404":
            return True
    except (ValueError, TypeError, KeyError, AttributeError):
        pass
    return "ErrorItemNotFound" in (e.stderr or "")


def _copy_into_place(source: Path, dest: Path) -> None:
    """Copy through a temporary name beside `dest`, so no reader sees half a file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as out, open(source, "rb") as src:
            shutil.copyfileobj(src, out)
        os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _put_back(rows: list[Row], download: Path, root: Path) -> dict[int, tuple[Path, str]]:
    """Copy each row's file out of a message's download to the directory and name its path
    records, under `root`. Matched by sha256, or by name and size for a row with no hash."""
    files = [
        (f, f.stat().st_size, sha256_of_file(f)) for f in sorted(download.iterdir()) if f.is_file()
    ]
    placed: dict[int, tuple[Path, str]] = {}
    for row in rows:
        for f, size, sha in files:
            if row.sha256:
                found = sha == row.sha256
            else:
                found = f.name in (row.name, row.filename) and row.file_size in (None, size)
            if found:
                dest = root / row.directory / row.name
                _copy_into_place(f, dest)
                placed[row.attachment_id] = (dest, sha)
                break
    return placed


def fetch_message(message: Message, root: Path) -> Outcome:
    """Ask for the message under each of its ids until one answers, and put back its rows'
    files. Files only: the database is the caller's."""
    with tempfile.TemporaryDirectory(prefix="sb-refetch-") as download:
        for mid in message.ids:
            try:
                run_outlook_cli(
                    ["download-attachments", mid, "--out", download, "--include-inline"],
                    timeout_sec=DOWNLOAD_TIMEOUT_S,
                )
            except OutlookCliAuthRequired:
                return Outcome(STOPPED, error="outlook-cli needs re-authentication", exit_code=4)
            except OutlookCliError as e:
                if _not_found(e):
                    continue
                logger.warning("download-attachments failed for %s: %s", mid, e)
                if e.exit_code != 5:
                    return Outcome(STOPPED, error=str(e), exit_code=e.exit_code)
                return Outcome(FAILED, error=f"outlook-cli exit {e.exit_code}")
            except subprocess.TimeoutExpired:
                logger.warning("download-attachments timed out for %s", mid)
                return Outcome(FAILED, error=f"no answer in {DOWNLOAD_TIMEOUT_S} s")
            return Outcome(FETCHED, placed=_put_back(message.rows, Path(download), root), via=mid)
    return Outcome(GONE)


def received_span(value) -> tuple[datetime, datetime] | None:
    """The earliest and latest UTC moment a stored received time can mean, or None when it does
    not parse. A time with no zone is NAIVE_TIME_ZONE's: in the hour its clocks go back it names
    two moments, and in the hour they skip it is read both ways too."""
    try:
        when = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    if when.tzinfo is not None:
        moment = when.astimezone(UTC)
        return moment, moment
    readings = [when.replace(tzinfo=NAIVE_TIME_ZONE, fold=fold).astimezone(UTC) for fold in (0, 1)]
    return min(readings), max(readings)


def _folders(recorded: str | None) -> list[str]:
    """Where to look: the folder the email records, when mail-reconcile walks it, then the others
    it walks, since mail filed from the Inbox has moved to Archive."""
    first = canonical_folder(recorded or "")
    rest = [folder for folder in DEFAULT_FOLDERS if folder != first]
    return [first, *rest] if first in DEFAULT_FOLDERS else rest


def lookup_plan(messages: list[Message], counts: dict) -> list[Message]:
    """The messages with no mailbox id that can be looked up: mail whose email records a received
    time and a subject, with only its rows that have a sha256, since the lookup accepts a file by
    its hash alone. Counts in `counts` the rows passed over (no_key) and those to try."""
    todo = []
    for message in messages:
        hashed = [row for row in message.rows if row.sha256]
        if (
            not hashed
            or message.email_id is None
            or message.folder in NOT_MAIL
            or received_span(message.received) is None
            or not normalize_subject(message.subject)
        ):
            counts["no_key"] += len(message.rows)
            continue
        counts["no_key"] += len(message.rows) - len(hashed)
        todo.append(replace(message, rows=hashed))
    counts["messages_to_try"] = len(todo)
    counts["to_try"] = sum(len(m.rows) for m in todo)
    return todo


def look_up(message: Message, root: Path) -> Outcome:
    """Find a message with no mailbox id by folder, received time and subject, and put back its
    rows' files when exactly one message matches. Files only: the database is the caller's."""
    span = received_span(message.received)
    subject = normalize_subject(message.subject)
    if span is None or not subject:
        raise ValueError("lookup_plan passes over an email with no received time or subject")
    since = span[0] - LOOKUP_WINDOW
    # The listing's upper bound is exclusive, and a stored time is to the second.
    until = span[1] + LOOKUP_WINDOW + timedelta(seconds=1)
    for folder in _folders(message.folder):
        try:
            listed = list_folder(folder, since, until)
        except OutlookCliAuthRequired:
            return Outcome(STOPPED, error="outlook-cli needs re-authentication", exit_code=4)
        except OutlookCliError as e:
            if _not_found(e):  # the mailbox has no such folder
                continue
            logger.warning("list-mail failed for %s: %s", folder, e)
            if e.exit_code != 5:
                return Outcome(STOPPED, error=str(e), exit_code=e.exit_code)
            return Outcome(FAILED, error=f"outlook-cli exit {e.exit_code}", note=LOOKED_UP)
        except subprocess.TimeoutExpired:
            logger.warning("list-mail timed out for %s", folder)
            return Outcome(FAILED, error=f"no answer in {LIST_TIMEOUT_S} s", note=LOOKED_UP)
        matches = [m for m in listed if normalize_subject(m.get("Subject")) == subject]
        if len(matches) > 1:
            note = {**LOOKED_UP, "folder": folder, "candidates": len(matches)}
            return Outcome(AMBIGUOUS, note=note)
        if matches:
            outcome = fetch_message(replace(message, ids=[str(matches[0]["Id"])]), root)
            if outcome.state == GONE:  # moved or deleted since it was listed: look again
                outcome = Outcome(NOT_FOUND)
            outcome.note = {**LOOKED_UP, "folder": folder}
            return outcome
    past_retention = span[1] < datetime.now(UTC) - RETENTION
    return Outcome(GONE if past_retention else NOT_FOUND, note=LOOKED_UP)


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.is_file() and os.path.samefile(a, b)
    except OSError:
        return False


def _record(conn: sqlite3.Connection, message: Message, outcome: Outcome, counts: dict) -> None:
    """Write what came of one message, for each of its rows, in one transaction, and count it in
    `counts`: the id half's, or the lookup's."""
    now = utc_now()  # in UTC, as attachment_content.extracted_at is (src/store/stamps.py)
    for row in message.rows:
        record: dict = {"attempts": row.attempts + 1, "last_attempt_at": now, **outcome.note}
        if outcome.state == FETCHED and row.attachment_id in outcome.placed:
            dest, sha = outcome.placed[row.attachment_id]
            path = row.file_path if _same_file(Path(row.file_path), dest) else str(dest)
            conn.execute(
                "UPDATE attachments SET file_path = ?, file_removed_at = NULL,"
                " sha256 = COALESCE(sha256, ?) WHERE id = ?",
                (path, sha, row.attachment_id),
            )
            record.update(state=FETCHED, fetched_at=now, via=outcome.via)
            counts["fetched"] += 1
        elif outcome.state == FETCHED:
            record.update(state=UNMATCHED, via=outcome.via)
            counts["unmatched"] += 1
        elif outcome.state == GONE:
            record.update(state=GONE, gone_at=now)
        else:
            record.update(state=outcome.state)
            if outcome.error:
                record["error"] = outcome.error
        conn.execute(
            "INSERT OR REPLACE INTO sync_metadata (key, value) VALUES (?, ?)",
            (f"{REFETCH_KEY}{row.attachment_id}", json.dumps(record, sort_keys=True)),
        )
    if outcome.state == FETCHED:
        counts["found"] += 1
    else:
        counts[outcome.state] += 1
        counts[f"{outcome.state}_files"] += len(message.rows)
    counts["messages_tried"] += 1
    conn.commit()


def _work_through(
    conn: sqlite3.Connection,
    todo: list[Message],
    fetch: Callable[[Message], Outcome],
    counts: dict,
    stats: dict,
    workers: int,
) -> None:
    """Fetch each message in `todo`, at most MAX_CONCURRENCY at a time, and record what came of
    each in `counts` as it comes. Once one stops the run, no other is started, and `stats` says
    why."""
    if not todo:
        return
    stop = threading.Event()

    def work(message: Message) -> Outcome | None:
        if stop.is_set():
            return None
        outcome = fetch(message)
        if outcome.state == STOPPED:
            stop.set()
        return outcome

    pool_size = max(1, min(workers, MAX_CONCURRENCY))
    with ThreadPoolExecutor(max_workers=pool_size) as pool:
        futures = {pool.submit(work, m): m for m in todo}
        for future in as_completed(futures):
            outcome = future.result()
            if outcome is None:
                continue
            if outcome.state == STOPPED:
                if not stats["stopped"]:
                    stats.update(
                        stopped=True,
                        stop_exit_code=outcome.exit_code,
                        stop_error=outcome.error,
                    )
                continue
            _record(conn, futures[future], outcome, counts)


def refetch_garbled(
    db_path=None,
    root=None,
    limit: int = DEFAULT_LIMIT,
    dry_run: bool = False,
    workers: int = MAX_CONCURRENCY,
    lookup: bool = True,
) -> dict:
    """Fetch back the swept originals of marked rows, for up to `limit` messages: by id, then by
    folder, time and subject for the messages with no mailbox id, unless `lookup` is off. See the
    module docstring. Returns the COUNTS; `lookup`, the LOOKUP_COUNTS, or None when it is off;
    and `stopped` with `stop_exit_code` and `stop_error` when an outlook-cli failure no retry
    would mend ended the run."""
    # Absolute, since a fetched file's path is recorded; not resolved, since the registrar
    # records paths through the root's own spelling (a symlink on the producer).
    root = (Path(root) if root else ATTACHMENTS_DIR).absolute()
    stats: dict = dict.fromkeys(COUNTS, 0)
    stats.update(stopped=False, stop_exit_code=None, stop_error=None, dry_run=dry_run, lookup=None)
    conn = get_connection(str(db_path or DEFAULT_DB))
    try:
        messages, no_id = plan(conn, root, stats)
        stats["messages_to_try"] = len(messages)
        stats["to_try"] = sum(len(m.rows) for m in messages)
        todo = messages[:limit]
        if not dry_run:
            _work_through(conn, todo, lambda m: fetch_message(m, root), stats, stats, workers)
        stats["left"] = len(messages) - (len(todo) if dry_run else stats["messages_tried"])
        if lookup:
            counts = stats["lookup"] = dict.fromkeys(LOOKUP_COUNTS, 0)
            findable = lookup_plan(no_id, counts)
            lookup_todo = findable[: limit - len(todo)]
            if not dry_run and not stats["stopped"]:
                _work_through(conn, lookup_todo, lambda m: look_up(m, root), counts, stats, workers)
            done = len(lookup_todo) if dry_run else counts["messages_tried"]
            counts["left"] = len(findable) - done
    finally:
        conn.close()
    return stats


def _files(n: int) -> str:
    return f"{n:,} file" if n == 1 else f"{n:,} files"


def _print(lines: list[tuple[str, str]]) -> None:
    for label, value in lines:
        print(f"  {label:<42}: {value}")


def cmd(args) -> int:
    """`python -m src.cli refetch-garbled [--dry-run] [--limit N] [--root PATH] [--no-lookup]`.
    Exits 4 when outlook-cli needs a login, 5 when it failed (a message failed, or the run
    stopped), else 0."""
    limit = DEFAULT_LIMIT if args.limit is None else args.limit
    if limit < 1:
        print("Error: --limit must be at least 1", file=sys.stderr)
        return 2
    stats = refetch_garbled(
        args.db, root=args.root, limit=limit, dry_run=args.dry_run, lookup=not args.no_lookup
    )
    lookup = stats["lookup"]
    mode = "DRY RUN, nothing downloaded or written" if args.dry_run else f"limit {limit} messages"
    print(f"refetch-garbled ({mode}):")
    lines = [
        ("garbled texts with a file path", f"{stats['garbled']:,}"),
        ("file on disk, for reextract", f"{stats['on_disk']:,}"),
        ("skipped, already fetched", f"{stats['already_fetched']:,}"),
        ("skipped, gone from the mailbox before", f"{stats['gone_before']:,}"),
        ("skipped, not in their message before", f"{stats['unmatched_before']:,}"),
        ("skipped, several messages matched before", f"{stats['ambiguous_before']:,}"),
        (f"skipped, given up after {MAX_ATTEMPTS} attempts", f"{stats['given_up']:,}"),
    ]
    if lookup is None:
        lines.append(("skipped, no mailbox id (Apple Mail era)", f"{stats['no_mailbox_id']:,}"))
    _print(lines)
    print("By id:")
    lines = [("to try", f"{_files(stats['to_try'])} in {stats['messages_to_try']:,} messages")]
    if not args.dry_run:
        lines += [
            ("messages tried", f"{stats['messages_tried']:,}"),
            ("files fetched", f"{stats['fetched']:,}"),
            ("files not in their message", f"{stats['unmatched']:,}"),
            (
                "messages gone from the mailbox",
                f"{stats['gone']:,} ({_files(stats['gone_files'])}), not asked for again",
            ),
            (
                "messages failed",
                f"{stats['failed']:,} ({_files(stats['failed_files'])}), asked for again next run",
            ),
        ]
    lines.append(("messages left for a later run", f"{stats['left']:,}"))
    _print(lines)
    if lookup is not None:
        print("By folder, time and subject (no mailbox id):")
        lines = [
            ("to try", f"{_files(lookup['to_try'])} in {lookup['messages_to_try']:,} messages"),
            ("skipped, no time, subject or sha256", f"{lookup['no_key']:,}"),
        ]
        if not args.dry_run:
            lines += [
                ("messages looked up", f"{lookup['messages_tried']:,}"),
                ("messages found", f"{lookup['found']:,}"),
                ("files fetched", f"{lookup['fetched']:,}"),
                ("files not in their message", f"{lookup['unmatched']:,}"),
                (
                    "several messages matched",
                    f"{lookup['ambiguous']:,} ({_files(lookup['ambiguous_files'])}),"
                    " not looked up again",
                ),
                (
                    "messages gone from the mailbox",
                    f"{lookup['gone']:,} ({_files(lookup['gone_files'])}), not looked up again",
                ),
                (
                    "messages not found",
                    f"{lookup['not_found']:,} ({_files(lookup['not_found_files'])}),"
                    " looked up again next run",
                ),
                (
                    "messages failed",
                    f"{lookup['failed']:,} ({_files(lookup['failed_files'])}),"
                    " looked up again next run",
                ),
            ]
        lines.append(("messages left for a later run", f"{lookup['left']:,}"))
        _print(lines)
    print(NEXT_STEPS)
    if stats["stopped"]:
        print(f"Stopped: {stats['stop_error']}", file=sys.stderr)
        return 4 if stats["stop_exit_code"] == 4 else 5
    failed = stats["failed"] + (lookup["failed"] if lookup is not None else 0)
    return 5 if failed else 0
