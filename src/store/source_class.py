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
    automation    a subject that starts with [VPS] or [ALERT]; or mail the owner
                  (BRAIN_USER_EMAIL_PATTERN) sent to no one but himself, not a reply or a
                  forward, whose subject a job stamped: a bracketed tag first ('[nightly] ...')
                  or an ISO date ('... 2026-05-28')
    mail          everything else, including whatever the rules are unsure of

The stamp rule was read off the data: of the mail the owner sent to himself alone, every
stamped one was a report (a job puts its host or name in brackets, or the run's date, in the
subject), while the unstamped rest mixes reports with his own notes, so it stays mail.

Every read path leaves out news and automation unless the caller passes include_news or
include_automation; documents and session notes stay in. The rows that come back say their
class.
"""

import json
import re
import sqlite3
import sys
from collections import defaultdict
from collections.abc import Iterable

import src.config as config
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
# Subject tags only automation writes, matched at the start in any case.
AUTOMATION_TAGS = ("[VPS]", "[ALERT]")

# A job's stamp on a subject: a bracketed tag at the start, or an ISO date anywhere in it.
_STAMP_TAG = re.compile(r"\[[^\[\]\s]{1,24}\]")
_STAMP_DATE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")


def hidden_classes(include_news: bool = False, include_automation: bool = False) -> tuple[str, ...]:
    """The classes a read path leaves out: news and automation, unless asked for."""
    return tuple(
        name
        for name, shown in ((NEWS, include_news), (AUTOMATION, include_automation))
        if not shown
    )


def _is_owner(address: str | None, pattern: str) -> bool:
    """A case-insensitive substring match, as calendar_loader matches the owner. An empty
    pattern matches no one: '' is a substring of every address."""
    return bool(pattern) and pattern.lower() in (address or "").lower()


def _stamped(subject: str) -> bool:
    """Whether a subject reads as a job's: not a reply or a forward, and carrying a stamp."""
    if normalize_subject(subject) != subject.strip().lower():
        return False
    return bool(_STAMP_TAG.match(subject) or _STAMP_DATE.search(subject))


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
    head = (subject or "").lstrip()
    if head.upper().startswith(AUTOMATION_TAGS):
        return AUTOMATION
    pattern = config.USER_EMAIL_PATTERN if owner_pattern is None else owner_pattern
    addresses = [address for address in recipients if address]
    if (
        _is_owner(sender_address, pattern)
        and addresses
        and all(_is_owner(address, pattern) for address in addresses)
        and _stamped(head)
    ):
        return AUTOMATION
    return MAIL


# --- reading it -----------------------------------------------------------------------------


def has_column(conn: sqlite3.Connection) -> bool:
    """Whether this store has emails.source_class (v33). A replica runs whatever code its
    checkout holds against whatever database it last pulled, so newer code must still read a
    store the producer has not migrated yet."""
    return any(r[1] == "source_class" for r in conn.execute("PRAGMA table_info(emails)"))


def _derived(alias: str) -> str:
    """The class read off a row of a store from before v33: every rule that needs nothing but
    the row itself, which leaves out only the owner's stamped mail to himself."""
    tagged = " OR ".join(f"LTRIM({alias}.subject) LIKE '{tag}%'" for tag in AUTOMATION_TAGS)
    return (
        f"CASE WHEN {alias}.mailbox_name = '{NEWS_MAILBOX}' THEN '{NEWS}'"
        f" WHEN {alias}.mailbox_name = '{DOCUMENT_MAILBOX}' THEN"
        f" CASE WHEN LOWER(TRIM({alias}.sender_address)) = '{SESSION_NOTE_ADDRESS}'"
        f" THEN '{SESSION_NOTE}' ELSE '{DOCUMENT}' END"
        f" WHEN {tagged} THEN '{AUTOMATION}' ELSE '{MAIL}' END"
    )


def hidden_ids_sql(
    conn: sqlite3.Connection, include_news: bool = False, include_automation: bool = False
) -> str | None:
    """A SELECT of the ids of the emails rows a read path leaves out; None when it leaves out
    none.

    Answered from idx_emails_source_class, never from the rows: source_class is the table's last
    column, and a column behind the body is reached through the body's overflow pages, so testing
    it row by row read the whole body of every email a query looked at.
    """
    classes = hidden_classes(include_news, include_automation)
    if not classes:
        return None
    listed = ", ".join(f"'{name}'" for name in classes)
    if has_column(conn):
        return f"SELECT id FROM emails WHERE source_class IN ({listed})"
    return f"SELECT id FROM emails x WHERE {_derived('x')} IN ({listed})"


def visible_sql(
    conn: sqlite3.Connection,
    email_id: str,
    include_news: bool = False,
    include_automation: bool = False,
) -> str:
    """A WHERE condition that holds unless `email_id`, an SQL expression for an emails.id, names
    a row of a class the read path leaves out. NULL passes: a Teams, WhatsApp, calendar or
    conversation item has no email."""
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
    """email id -> its class, for the ids given."""
    ids = sorted({int(i) for i in email_ids if i is not None})
    if not ids:
        return {}
    column = "source_class" if has_column(conn) else _derived("emails")
    rows = conn.execute(
        f"SELECT id, {column} FROM emails WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps(ids),),
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


# --- storing it -----------------------------------------------------------------------------


def class_insert(
    conn: sqlite3.Connection,
    mailbox: str | None,
    sender_address: str | None,
    subject: str | None,
    recipients: Iterable[str | None] = (),
) -> tuple[str, str, tuple]:
    """The column, the placeholder and the value an INSERT INTO emails adds to store the row's
    class: (", source_class", ", ?", (class,)). Nothing on a store from before v33, which has no
    column for it and is given one, classified, when it is migrated."""
    if not has_column(conn):
        return "", "", ()
    return ", source_class", ", ?", (classify(mailbox, sender_address, subject, recipients),)


def _owner_mail_recipients(conn: sqlite3.Connection, pattern: str, columns: set) -> dict:
    """email id -> the To and Cc addresses of each email the owner sent, from the header people
    (who always have an address; the people the model named have none). Empty without a
    pattern, or on a store too old to record senders and recipients."""
    out: dict = defaultdict(list)
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name IN ('email_people', 'people')"
        )
    }
    if not pattern or "sender_address" not in columns or len(tables) < 2:
        return out
    rows = conn.execute(
        "SELECT ep.email_id, p.email FROM emails e"
        " JOIN email_people ep ON ep.email_id = e.id"
        " JOIN people p ON p.id = ep.person_id"
        " WHERE instr(LOWER(e.sender_address), ?) > 0"
        " AND ep.role_in_email IN ('recipient', 'cc') AND COALESCE(p.email, '') <> ''",
        (pattern.lower(),),
    )
    for email_id, address in rows:
        out[email_id].append(address)
    return out


def reclassify(conn: sqlite3.Connection, owner_pattern: str | None = None) -> dict:
    """Give every emails row the class `classify` gives it, writing only the rows that change;
    how many rows each class holds after.

    The caller holds the write lock (BEGIN IMMEDIATE) and commits. emails_au, the trigger that
    keeps emails_fts in step, fires on an UPDATE of any column and deletes and re-inserts the
    row's whole text, though the class is no text the index holds: on a copy of the replica that
    was four times the work and half as much WAL again. So it is set aside for these updates and
    put back, as it was, inside the same transaction.
    """
    pattern = config.USER_EMAIL_PATTERN if owner_pattern is None else owner_pattern
    columns = {r[1] for r in conn.execute("PRAGMA table_info(emails)")}
    recipients = _owner_mail_recipients(conn, pattern, columns)
    # From the index: in the table the class sits behind the body.
    stored = {row[0]: row[1] for row in conn.execute("SELECT id, source_class FROM emails")}
    # A column an old store lacks reads as NULL: its rows were written before it existed.
    read = ", ".join(
        name if name in columns else "NULL"
        for name in ("mailbox_name", "sender_address", "subject")
    )
    changes = []
    for email_id, mailbox, sender, subject in conn.execute(
        f"SELECT id, {read} FROM emails"
    ).fetchall():
        wanted = classify(mailbox, sender, subject, recipients.get(email_id, ()), pattern)
        if stored.get(email_id) != wanted:
            changes.append((wanted, email_id))
    if changes:
        trigger = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'emails_au'"
        ).fetchone()
        if trigger:
            conn.execute("DROP TRIGGER emails_au")
        conn.executemany("UPDATE emails SET source_class = ? WHERE id = ?", changes)
        if trigger:
            conn.execute(trigger[0])
    counts = {
        row[0]: row[1]
        for row in conn.execute("SELECT source_class, COUNT(*) FROM emails GROUP BY source_class")
    }
    if not pattern and counts.get(MAIL):
        print(
            "source_class: BRAIN_USER_EMAIL_PATTERN is unset, so the reports the owner's jobs "
            "mail him without a [VPS] or [ALERT] tag are left as 'mail'. Set it, then run "
            "`python -m src.cli classify-sources`.",
            file=sys.stderr,
        )
    return counts
