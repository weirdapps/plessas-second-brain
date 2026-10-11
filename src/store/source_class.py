"""What an emails row is: mail, automation, news, a document or a session note.

The emails table holds more than mail. News items land in it under the mailbox 'News'
(src/export/news_export.py); documents, SharePoint files and pages, web imports and the notes
Claude sessions write land under 'External' (src/extract/attachment_pipeline.py); and the
reports the owner's own jobs mail him arrive as ordinary mail. What the model extracted from all
of them read as what someone at work had decided or owed: on one corpus of 144K decisions, 16%
came from automation mail and 14% from news. Only the decision and action queries left news
out, and nothing left automation out.

Each row now carries emails.source_class (schema v33), set when it is stored, by these rules,
the first that matches:

    news          mailbox 'News'
    session_note  mailbox 'External', from the session-note address
    document      any other 'External' row
    automation    mail the owner (BRAIN_USER_EMAIL_PATTERN, matched as src/store/owner.py
                  says) sent to no one but himself, not a reply or a forward, whose subject a
                  job stamped, a bracketed tag first ('[VPS] ...', '[nightly] ...') or an ISO
                  date ('... 2026-05-28'), and which names something besides the stamp
    mail          everything else, including whatever the rules are unsure of

The rule was read off the data: of the mail the owner sent to himself alone, every stamped one
was a report (a job puts its host or name in brackets, or the run's date, in the subject), while
the unstamped rest mixes reports with his own notes, so it stays mail. A tag alone proves
nothing: anyone can send '[ALERT] ...', and a tagged mail the owner also sent to someone else
is mail someone may answer.

There is one implementation: classify(), used when a row is stored and by the v33 backfill.
Every read path leaves out news and automation unless the caller passes include_news or
include_automation, reading the stored class; documents and session notes stay in. The rows
that come back say their class.

A store older than v33 has no class to read, and a replica holds one from the new code's
arrival to its next pull, an hour or more. Its reads do what they did before the class: they
leave out the News mailbox (classify()'s first rule, which needs no column) and nothing else,
and label each row by classify() without its recipients, which never makes one automation. A
process logs that once (MISSING), recall's summary carries the same sentence, and stats says
'pending migration'. The writers and reclassify() refuse such a store (SourceClassMissing):
they run where the store is built, after the migrations.
"""

import json
import logging
import re
import sqlite3
import sys
import threading
from collections import defaultdict
from collections.abc import Iterable

import src.config as config
from src.store.owner import is_owner_address
from src.store.schema import normalize_subject

MAIL = "mail"
AUTOMATION = "automation"
NEWS = "news"
DOCUMENT = "document"
SESSION_NOTE = "session_note"
CLASSES = (MAIL, AUTOMATION, NEWS, DOCUMENT, SESSION_NOTE)

NEWS_MAILBOX = "News"  # src/export/news_export.py
DOCUMENT_MAILBOX = "External"  # src/extract/attachment_pipeline.py
# ingest_text_document's sender address for the source "session-note" (src/export/session_notes.py)
SESSION_NOTE_ADDRESS = "session-note@documents.local"

# A job's stamp on a subject: a bracketed tag at the start, or an ISO date anywhere in it.
_STAMP_TAG = re.compile(r"\[[^\[\]\s]{1,24}\]")
_STAMP_DATE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")

# Rows the backfill reads, classifies and writes at a time.
BACKFILL_CHUNK = 5000

logger = logging.getLogger(__name__)

# What a store without the class says while its reads leave out news alone: once per process in
# the log and in every recall summary. stats says PENDING.
MISSING = "source_class column missing; run migrate on the producer; news-only exclusion in effect"
PENDING = "pending migration"
_warned = False
_warned_lock = threading.Lock()

# Until a store has the class: the News mailbox, classify()'s first rule, which needs no column.
_NEWS_IDS = f"SELECT id FROM emails WHERE mailbox_name = '{NEWS_MAILBOX}'"


class SourceClassMissing(RuntimeError):
    """The store has no emails.source_class: it is older than schema v33. Raised where a class
    is stored or set (the writers, reclassify()); reads fall back instead (column_missing())."""


def hidden_classes(include_news: bool = False, include_automation: bool = False) -> tuple[str, ...]:
    """The classes a read path leaves out: news and automation, unless asked for."""
    return tuple(
        name
        for name, shown in ((NEWS, include_news), (AUTOMATION, include_automation))
        if not shown
    )


def _stamped(subject: str) -> bool:
    """Whether a subject reads as a job's report: not a reply or a forward, stamped with a
    bracketed tag first or an ISO date, and naming something besides the stamp. A subject
    that is only a date or a tag is how a person titles a note as easily as a job."""
    head = subject.strip()  # tabs and no-break spaces too
    if normalize_subject(head) != head.lower():
        return False  # 'Re:', 'FW:', 'Απ:' and the like: someone answered or passed it on
    tag = _STAMP_TAG.match(head)
    if not tag and not _STAMP_DATE.search(head):
        return False
    rest = _STAMP_DATE.sub(" ", head[tag.end() :] if tag else head)
    return any(ch.isalpha() for ch in rest)


def classify(
    mailbox: str | None,
    sender_address: str | None,
    subject: str | None,
    recipients: Iterable[str | None] = (),
    owner_pattern: str | None = None,
) -> str:
    """The class of an emails row, by the rules in the module docstring.

    Args:
        mailbox: emails.mailbox_name
        sender_address: emails.sender_address
        subject: emails.subject
        recipients: the To and Cc addresses
        owner_pattern: the owner's address pattern; BRAIN_USER_EMAIL_PATTERN when None
    """
    if mailbox == NEWS_MAILBOX:
        return NEWS
    if mailbox == DOCUMENT_MAILBOX:
        if (sender_address or "").strip().lower() == SESSION_NOTE_ADDRESS:
            return SESSION_NOTE
        return DOCUMENT
    pattern = config.USER_EMAIL_PATTERN if owner_pattern is None else owner_pattern
    addresses = [address for address in recipients if address]
    if (
        is_owner_address(sender_address, pattern)
        and addresses
        and all(is_owner_address(address, pattern) for address in addresses)
        and _stamped(subject or "")
    ):
        return AUTOMATION
    return MAIL


# --- reading it -----------------------------------------------------------------------------


def has_column(conn: sqlite3.Connection) -> bool:
    """Whether this store has emails.source_class (v33)."""
    return any(r[1] == "source_class" for r in conn.execute("PRAGMA table_info(emails)"))


def column_missing(conn: sqlite3.Connection) -> bool:
    """Whether the store lacks emails.source_class, as a replica's v32 file does until its next
    pull. Its reads then leave out the News mailbox alone. The first time a process finds such
    a store, it logs MISSING; never again after."""
    global _warned
    if has_column(conn):
        return False
    with _warned_lock:
        first, _warned = not _warned, True
    if first:
        logger.warning(MISSING)
    return True


def _require_column(conn: sqlite3.Connection) -> None:
    """Refuse a store without the class where a class is stored or set."""
    if not has_column(conn):
        raise SourceClassMissing(
            "emails.source_class is missing: this store is older than schema v33. Run "
            "`python -m src.cli migrate` where the store is built; a replica gets the column "
            "with its next pull."
        )


def hidden_ids_sql(
    conn: sqlite3.Connection, include_news: bool = False, include_automation: bool = False
) -> str | None:
    """A SELECT of the ids of the emails rows a read path leaves out; None when it leaves out
    none. On a store without the class, the News mailbox's unless include_news: nothing there
    tells automation apart.

    Answered from idx_emails_source_class, never from the rows: source_class is the table's last
    column, and a column behind the body is reached through the body's overflow pages, so testing
    it row by row read the whole body of every email a query looked at.
    """
    if column_missing(conn):
        return None if include_news else _NEWS_IDS
    classes = hidden_classes(include_news, include_automation)
    if not classes:
        return None
    listed = ", ".join(f"'{name}'" for name in classes)
    return f"SELECT id FROM emails WHERE source_class IN ({listed})"


def visible_sql(
    conn: sqlite3.Connection,
    email_id: str,
    include_news: bool = False,
    include_automation: bool = False,
) -> str:
    """A WHERE condition that holds unless `email_id`, an SQL expression for an emails.id, names
    a row of a class the read path leaves out. NULL passes: a Teams, WhatsApp, calendar or
    conversation item has no email.

    On a store without the class it tests the News mailbox row by row, as reads did before the
    class: the mailbox sits ahead of the body, so each test is one lookup by id, where the set
    of News ids took a scan of the table in every statement: a keyword search on a replica's
    v32 file took 50 ms that way, 6 ms this way, as before.
    """
    if column_missing(conn):
        if include_news:
            return "1"
        return (
            f"NOT EXISTS (SELECT 1 FROM emails AS sc_news WHERE sc_news.id = {email_id}"
            f" AND sc_news.mailbox_name = '{NEWS_MAILBOX}')"
        )
    ids = hidden_ids_sql(conn, include_news, include_automation)
    return "1" if ids is None else f"IFNULL({email_id}, 0) NOT IN ({ids})"


def visible_ids(
    conn: sqlite3.Connection,
    email_ids: list,
    include_news: bool = False,
    include_automation: bool = False,
) -> list:
    """The email ids, in their order, less those of a class the read path leaves out."""
    hidden_sql = hidden_ids_sql(conn, include_news, include_automation)
    if hidden_sql is None or not email_ids:
        return list(email_ids)
    hidden = {
        row[0]
        for row in conn.execute(
            f"SELECT id FROM ({hidden_sql}) WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps([int(i) for i in email_ids]),),
        )
    }
    return [i for i in email_ids if i not in hidden]


def classes_of(conn: sqlite3.Connection, email_ids: Iterable) -> dict:
    """email id -> its class, for the ids given. On a store without the class, what classify()
    makes of the row's mailbox, sender and subject without its recipients: news, a document, a
    session note or mail, never automation."""
    ids = sorted({int(i) for i in email_ids if i is not None})
    if not ids:
        return {}
    wanted = (json.dumps(ids),)
    if column_missing(conn):
        rows = conn.execute(
            "SELECT id, mailbox_name, sender_address, subject FROM emails"
            " WHERE id IN (SELECT value FROM json_each(?))",
            wanted,
        )
        return {row[0]: classify(row[1], row[2], row[3]) for row in rows}
    rows = conn.execute(
        "SELECT id, source_class FROM emails WHERE id IN (SELECT value FROM json_each(?))", wanted
    )
    return {row[0]: row[1] for row in rows}


def with_source_class(conn: sqlite3.Connection, rows: list, key: str = "email_id") -> list:
    """Give each row (a dict) its email's class as `source_class`, None for a row with no email;
    the rows, for chaining. One query for the list, after its LIMIT: selected in the query
    itself, the column was read, through its body, for every row the query sorted."""
    classes = classes_of(conn, (row.get(key) for row in rows))
    for row in rows:
        row["source_class"] = classes.get(row.get(key))
    return rows


def class_counts(conn: sqlite3.Connection) -> dict | str:
    """How many emails rows each class holds, from idx_emails_source_class; PENDING on a store
    without the class."""
    if column_missing(conn):
        return PENDING
    counts = {
        row[0]: row[1]
        for row in conn.execute("SELECT source_class, COUNT(*) FROM emails GROUP BY source_class")
    }
    return {name: counts.get(name, 0) for name in CLASSES}


# --- storing it -----------------------------------------------------------------------------


def stored_class(
    conn: sqlite3.Connection,
    mailbox: str | None,
    sender_address: str | None,
    subject: str | None,
    recipients: Iterable[str | None] = (),
) -> str:
    """The class a writer stores with a new emails row. Raises SourceClassMissing on a store
    without the column: writers run where the store is built, after the migrations
    (load_extractions and every command that writes run them first), so there it means the
    store was never migrated."""
    _require_column(conn)
    return classify(mailbox, sender_address, subject, recipients)


def _recipients(conn: sqlite3.Connection, email_ids: list) -> dict:
    """email id -> its To and Cc addresses, from the header people (who always have an address;
    the people the model named have none)."""
    out: dict = defaultdict(list)
    rows = conn.execute(
        "SELECT ep.email_id, p.email FROM email_people ep JOIN people p ON p.id = ep.person_id"
        " WHERE ep.email_id IN (SELECT value FROM json_each(?))"
        " AND ep.role_in_email IN ('recipient', 'cc') AND COALESCE(p.email, '') <> ''",
        (json.dumps(email_ids),),
    )
    for email_id, address in rows:
        out[email_id].append(address)
    return out


def reclassify(conn: sqlite3.Connection, owner_pattern: str | None = None) -> dict:
    """Give every emails row the class classify() gives it, writing only the rows that change;
    how many rows each class holds after.

    BACKFILL_CHUNK rows at a time, by id: their mailbox, sender and subject, and the recipients
    of those the owner sent, then one UPDATE per changed row by primary key. The caller holds the
    write lock (BEGIN IMMEDIATE) and commits. emails_au, the trigger that keeps emails_fts in
    step, fires on an UPDATE of any column and deletes and re-inserts the row's whole text,
    though the class is no text the index holds: on a copy of the replica that was four times
    the work and half as much WAL again. So it is set aside for these updates and put back, as
    it was, inside the same transaction.
    """
    _require_column(conn)
    pattern = config.USER_EMAIL_PATTERN if owner_pattern is None else owner_pattern
    columns = {r[1] for r in conn.execute("PRAGMA table_info(emails)")}
    people = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name IN ('email_people', 'people')"
        )
    }
    with_recipients = bool(pattern) and "sender_address" in columns and len(people) == 2
    # A column an old store lacks reads as NULL: its rows were written before it existed.
    read = ", ".join(
        name if name in columns else "NULL"
        for name in ("mailbox_name", "sender_address", "subject")
    )
    # From the index: in the table the class sits behind the body.
    stored = {row[0]: row[1] for row in conn.execute("SELECT id, source_class FROM emails")}
    trigger = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'emails_au'"
    ).fetchone()
    set_aside = False
    last = -(2**63)
    while True:
        rows = conn.execute(
            f"SELECT id, {read} FROM emails WHERE id > ? ORDER BY id LIMIT ?",
            (last, BACKFILL_CHUNK),
        ).fetchall()
        if not rows:
            break
        last = rows[-1][0]
        owned = [r[0] for r in rows if with_recipients and is_owner_address(r[2], pattern)]
        recipients = _recipients(conn, owned) if owned else {}
        changes = []
        for email_id, mailbox, sender, subject in rows:
            wanted = classify(mailbox, sender, subject, recipients.get(email_id, ()), pattern)
            if stored.get(email_id) != wanted:
                changes.append((wanted, email_id))
        if changes:
            if trigger and not set_aside:
                conn.execute("DROP TRIGGER emails_au")
                set_aside = True
            conn.executemany("UPDATE emails SET source_class = ? WHERE id = ?", changes)
    if set_aside:
        conn.execute(trigger[0])
    counts = {
        row[0]: row[1]
        for row in conn.execute("SELECT source_class, COUNT(*) FROM emails GROUP BY source_class")
    }
    if not pattern and counts.get(MAIL):
        print(
            "source_class: BRAIN_USER_EMAIL_PATTERN is unset, so no mail is taken for the "
            "owner's automation and the reports his jobs mail him are left as 'mail'. Set it, "
            "then run `python -m src.cli classify-sources`.",
            file=sys.stderr,
        )
    return counts
