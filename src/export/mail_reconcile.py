"""Which Outlook messages the store lacks, and staging them again.

The export walks each folder forward from a cursor, so a message it never listed
or never fetched is lost without a trace: on 2026-10-07 a full comparison of
Outlook's Archive, Sent Items and Inbox with the store found 190 messages missing,
142 of them received 2026-05-02..05. Nothing had compared the two before.

A listed message counts as held when the store has it:

    by_id            under its Graph id, or an alias of it (src/store/loader.py)
    by_rfc822_id     under its RFC822 Message-ID, compared without brackets or case
    staged           in a staging batch, on its way in (the producer's)
    by_subject_time  for a row stored before Message-IDs were captured (about
                     2026-04-20) only: the same subject, normalised on both sides,
                     within SUBJECT_TIME_TOLERANCE. Old rows hold naive local time,
                     newer ones UTC.
    newer            received after the store's newest mail: not synced yet

and as missing otherwise. Listing goes through outlook-cli; the store is opened
read-only, so the report runs on a replica. Two writes are the producer's alone:
refetch() stages the missing messages by Graph id through the export's own path,
so the next sync extracts and loads them, and record_aliases() notes the Graph id
of every copy found by its Message-ID under another id, which lets the attachment
registrar claim the directories downloaded under those ids.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import unicodedata
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.export.outlook_cli import run_outlook_cli
from src.export.outlook_export import (
    commit_messages_to_db,
    download_attachments_for_messages,
    fetch_bodies_concurrent,
)

logger = logging.getLogger(__name__)

DEFAULT_FOLDERS = ("Archive", "Sent Items", "Inbox")
# The export's own spelling, which the store's mailbox_name carries.
_FOLDER_SPELLING = {"archive": "Archive", "inbox": "Inbox", "sentitems": "Sent Items"}

LIST_SELECT = "Id,InternetMessageId,ReceivedDateTime,Subject"
LIST_WINDOW = timedelta(days=31)
LIST_MAX = 100_000  # outlook-cli's own ceiling for --all
LIST_PAGE = 1000
LIST_TIMEOUT_S = 900

SUBJECT_TIME_TOLERANCE = timedelta(hours=4)
# Mailboxes that are not mail: news digests and documents.
NOT_MAIL = ("News", "External")

# M365 is shared with every other job on the host.
MAX_CONCURRENCY = 2
REFETCH_CHUNK = 50

KINDS = ("by_id", "by_rfc822_id", "staged", "by_subject_time", "newer", "missing")

# Tags a mail gateway puts on a subject, on one side and not the other.
_GATEWAY_TAG = re.compile(
    r"\[\s*(?:external(?: email)?|warning:[^\]]*|suspected spam)\s*\]", re.IGNORECASE
)
# Reply prefixes and [tags] ahead of the subject proper. The two sides can order
# them differently ("Re: [list] Re: x" against "Re: Re: [list] x"), so all go.
_LEADING = re.compile(r"^\s*(?:(?:re|fw|fwd|απ|πρ|σχετ)\s*:|\[[^\]]*\])\s*", re.IGNORECASE)


class TruncatedListing(RuntimeError):
    """A listing window reached outlook-cli's cap: what is absent from it proves nothing."""


def canonical_folder(name: str) -> str:
    """The folder as the export spells it: 'SentItems' is staged as 'Sent Items'."""
    key = re.sub(r"\s+", "", name).lower()
    return _FOLDER_SPELLING.get(key, name.strip())


def normalize_subject(subject: str | None) -> str:
    """A subject without gateway tags, leading prefixes and tags, accents, case,
    invisible characters (a zero-width space trails some) or extra spaces."""
    decomposed = unicodedata.normalize("NFKD", subject or "")
    text = "".join(
        c for c in decomposed if not unicodedata.combining(c) and unicodedata.category(c) != "Cf"
    )
    text = _GATEWAY_TAG.sub(" ", text)
    while True:
        stripped = _LEADING.sub("", text)
        if stripped == text:
            break
        text = stripped
    return " ".join(text.split()).casefold()


def normalize_imid(value: str | None) -> str:
    """An RFC822 Message-ID without brackets, padding or case."""
    return (value or "").strip().strip("<>").strip().lower()


def parse_when(value) -> datetime | None:
    """An ISO time as UTC; one without a zone is read as UTC."""
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Store:
    """What the store holds, read once."""

    message_ids: set[str] = field(default_factory=set)  # with the aliases
    imids: dict[str, int] = field(default_factory=dict)  # normalised Message-ID -> emails.id
    subject_times: dict[str, list[datetime]] = field(default_factory=dict)
    newest: datetime | None = None

    def near(self, subject: str, when: datetime | None) -> bool:
        """Whether a row without a Message-ID has this subject within the tolerance."""
        times = self.subject_times.get(subject)
        if not times or when is None:
            return False
        i = bisect_left(times, when - SUBJECT_TIME_TOLERANCE)
        return i < len(times) and times[i] <= when + SUBJECT_TIME_TOLERANCE


def read_store(conn: sqlite3.Connection) -> Store:
    """Read the store's ids, Message-IDs and, for rows without one, subjects and times.

    Two passes, so neither reads past the body: the Message-ID is the last column,
    after content, and its partial index answers the first pass alone.
    """
    store = Store()
    with_imid: set[int] = set()
    for email_id, imid in conn.execute(
        "SELECT id, internet_message_id FROM emails WHERE internet_message_id IS NOT NULL"
    ):
        key = normalize_imid(imid)
        if key:
            store.imids[key] = email_id
            with_imid.add(email_id)
    subject_times: dict[str, list[datetime]] = defaultdict(list)
    for email_id, message_id, subject, received, mailbox in conn.execute(
        "SELECT id, message_id, subject, date_received, mailbox_name FROM emails"
    ):
        store.message_ids.add(str(message_id))
        if mailbox in NOT_MAIL:
            continue
        when = parse_when(received)
        if when is None:
            continue
        if store.newest is None or when > store.newest:
            store.newest = when
        if email_id not in with_imid:
            subject_times[normalize_subject(subject)].append(when)
    store.subject_times = {k: sorted(v) for k, v in subject_times.items()}
    has_aliases = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'email_aliases'"
    ).fetchone()
    if has_aliases:
        store.message_ids.update(
            str(r[0]) for r in conn.execute("SELECT message_id FROM email_aliases")
        )
    return store


def read_staged(staging_dir: Path | None) -> tuple[set[str], set[str]]:
    """(Graph ids, normalised Message-IDs) of what the staging batches hold.

    Read only: an unreadable batch is passed over, not quarantined.
    """
    ids: set[str] = set()
    imids: set[str] = set()
    if staging_dir is None or not staging_dir.is_dir():
        return ids, imids
    for batch_file in sorted(staging_dir.glob("batch-*.json")):
        try:
            batch = json.loads(batch_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        emails = batch.get("emails", []) if isinstance(batch, dict) else batch
        for email in emails if isinstance(emails, list) else []:
            if not isinstance(email, dict):
                continue
            ids.add(str(email.get("message_id", "")))
            key = normalize_imid(email.get("internet_message_id"))
            if key:
                imids.add(key)
    return ids, imids


def list_folder(folder: str, since: datetime, until: datetime) -> list[dict]:
    """Every message in `folder` received in [since, until), a window at a time."""
    items: dict[str, dict] = {}
    start = since
    while start < until:
        end = min(start + LIST_WINDOW, until)
        listing = run_outlook_cli(
            [
                "list-mail",
                "--folder",
                folder,
                "--since",
                _iso(start),
                "--until",
                _iso(end),
                "--all",
                "--max",
                str(LIST_MAX),
                "--top",
                str(LIST_PAGE),
                "--select",
                LIST_SELECT,
            ],
            timeout_sec=LIST_TIMEOUT_S,
        )
        if isinstance(listing, dict):
            listing = listing.get("value", [])
        page = [m for m in listing or [] if isinstance(m, dict) and m.get("Id")]
        if len(page) >= LIST_MAX:
            raise TruncatedListing(
                f"{folder}: {_iso(start)}..{_iso(end)} returned {len(page)} messages, "
                "outlook-cli's cap; absence from it proves nothing"
            )
        for message in page:
            items[message["Id"]] = message
        start = end
    return list(items.values())


def classify(
    messages: list[dict],
    store: Store,
    staged: tuple[set[str], set[str]] = (set(), set()),
    cutoff: datetime | None = None,
) -> dict[str, list[dict]]:
    """The listed messages by how the store holds them (see the module docstring)."""
    staged_ids, staged_imids = staged
    result: dict[str, list[dict]] = {kind: [] for kind in KINDS}
    for message in messages:
        message_id = str(message.get("Id") or "")
        imid = normalize_imid(message.get("InternetMessageId"))
        when = parse_when(message.get("ReceivedDateTime"))
        if message_id in store.message_ids:
            kind = "by_id"
        elif imid and imid in store.imids:
            kind = "by_rfc822_id"
        elif message_id in staged_ids or (imid and imid in staged_imids):
            kind = "staged"
        elif store.near(normalize_subject(message.get("Subject")), when):
            kind = "by_subject_time"
        elif cutoff is not None and when is not None and when > cutoff:
            kind = "newer"
        else:
            kind = "missing"
        result[kind].append(message)
    return result


@dataclass
class Report:
    since: datetime
    until: datetime
    cutoff: datetime | None
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    missing: list[dict] = field(default_factory=list)
    # (Graph id, emails.id) of each copy found by its Message-ID under another id.
    copies: list[tuple[str, int]] = field(default_factory=list)

    def as_json(self) -> dict:
        return {
            "since": _iso(self.since),
            "until": _iso(self.until),
            "cutoff": _iso(self.cutoff) if self.cutoff else None,
            "counts": self.counts,
            "missing": self.missing,
        }


def reconcile(
    db_path: Path | str,
    folders: list[str],
    since: datetime,
    until: datetime,
    staging_dir: Path | None = None,
) -> Report:
    """List each folder and set it against the store, which it only reads."""
    from src.store.sql_readonly import connect_read_only

    conn = connect_read_only(Path(db_path))
    try:
        store = read_store(conn)
    finally:
        conn.close()
    staged = read_staged(staging_dir)
    cutoff = min(store.newest, until) if store.newest else None
    report = Report(since=since, until=until, cutoff=cutoff)
    for name in folders:
        folder = canonical_folder(name)
        listed = list_folder(folder, since, until)
        kinds = classify(listed, store, staged, cutoff)
        report.counts[folder] = {"listed": len(listed), **{k: len(v) for k, v in kinds.items()}}
        report.missing.extend(
            {
                "folder": folder,
                "Id": m.get("Id"),
                "InternetMessageId": m.get("InternetMessageId"),
                "ReceivedDateTime": m.get("ReceivedDateTime"),
                "Subject": m.get("Subject"),
            }
            for m in kinds["missing"]
        )
        report.copies.extend(
            (m["Id"], store.imids[normalize_imid(m.get("InternetMessageId"))])
            for m in kinds["by_rfc822_id"]
        )
    report.missing.sort(key=lambda m: str(m.get("ReceivedDateTime") or ""))
    return report


def record_aliases(conn: sqlite3.Connection, copies: list[tuple[str, int]]) -> int:
    """Note each copy's Graph id as an alias of the email it duplicates; how many were new."""
    from src.store.loader import record_alias

    added = sum(record_alias(conn, alias, email_id) for alias, email_id in copies)
    conn.commit()
    return added


def refetch(missing: list[dict], concurrency: int = MAX_CONCURRENCY, limit: int = 0) -> dict:
    """Stage the missing messages by Graph id, as the hourly export stages new mail.

    The next sync extracts and loads them; their attachments are downloaded as the
    export downloads them. At most MAX_CONCURRENCY get-mail calls at once.
    """
    todo = missing[:limit] if limit > 0 else list(missing)
    workers = max(1, min(concurrency, MAX_CONCURRENCY))
    stats: dict = {"requested": len(todo), "staged": 0, "failed": [], "attachments": 0}
    by_folder: dict[str, list[str]] = defaultdict(list)
    for message in todo:
        by_folder[message["folder"]].append(message["Id"])
    for folder, ids in by_folder.items():
        for start in range(0, len(ids), REFETCH_CHUNK):
            chunk = ids[start : start + REFETCH_CHUNK]
            fetched = [
                m for m in fetch_bodies_concurrent(chunk, concurrency=workers) if m is not None
            ]
            got = {m["Id"] for m in fetched}
            stats["failed"].extend(i for i in chunk if i not in got)
            if not fetched:
                continue
            commit_messages_to_db(fetched, folder=folder)
            stats["staged"] += len(fetched)
            downloaded = download_attachments_for_messages(fetched)
            stats["attachments"] += downloaded.get("downloaded_messages", 0)
            logger.info("%s: staged %d of %d", folder, len(fetched), len(chunk))
    return stats
