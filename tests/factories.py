"""Rows for a test store: one call per row, with the rows it cannot exist without.

The suite set its rows up with raw INSERTs, hundreds of them across a hundred-odd
files, each spelling out every NOT NULL column, and with ten-odd private copies of
an "insert an email" helper. A column added or renamed meant editing all of them.
These functions write the rows the tests insert most, through the connection a test
already holds, with defaults that look like what the loaders write, and return the
new row's id:

    conn = create_database(str(tmp_path / "brain.db"))
    email_id = factories.email(conn, subject="Okapi plan", mailbox_name="Archive")
    factories.attachment(conn, email_id=email_id, text="the okapi figures")
    factories.decision(conn, "Adopt the okapi plan", email_id=email_id)

Every keyword a function does not name is a column of its table. One that names no
column fails the insert, so a test that sets a column after a rename fails instead of
quietly testing the default. Each call commits, so another connection (a tool opening
the store by path) sees the row.

Every value is invented: example.com addresses and made-up names.
"""

import mimetypes
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from src.store.schema import subject_to_conversation_id

Contact = tuple[str, str]  # (display name, address)

ALICE: Contact = ("Alice Smith", "alice@example.com")
BOB: Contact = ("Bob Jones", "bob@example.com")
OLGA: Contact = ("Olga Organiser", "olga@example.com")

# The columns that say where a decision or an action item came from.
_SOURCES = (
    "email_id",
    "teams_thread_id",
    "event_id",
    "whatsapp_thread_id",
    "attachment_id",
    "conversation_turn_id",
)


def ago(days: float = 0, hours: float = 0) -> str:
    """A UTC time this long before now (negative for the future), as the loaders store it."""
    when = datetime.now(UTC) - timedelta(days=days, hours=hours)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert(conn: sqlite3.Connection, table: str, values: dict) -> int:
    # Quoted: emails has a column named "references".
    columns = ", ".join(f'"{name}"' for name in values)
    marks = ", ".join("?" for _ in values)
    cursor = conn.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values())
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _next(conn: sqlite3.Connection, table: str, column: str = "id") -> int:
    """One past the largest value in use, so ids never collide with rows a test wrote itself."""
    return conn.execute(f"SELECT COALESCE(MAX({column}), 0) + 1 FROM {table}").fetchone()[0]


def _person_id(conn: sqlite3.Connection, address: str) -> int | None:
    row = conn.execute("SELECT id FROM people WHERE lower(email) = lower(?)", (address,)).fetchone()
    return row[0] if row else None


def person(
    conn: sqlite3.Connection, name: str = ALICE[0], email: str | None = ALICE[1], **columns
) -> int:
    """The person with this address, created when there is none; found case-blind, as the
    loader finds them."""
    if email and (found := _person_id(conn, email)) is not None:
        return found
    person_id = _insert(conn, "people", {"name": name, "email": email, **columns})
    conn.commit()
    return person_id


def topic(conn: sqlite3.Connection, name: str) -> int:
    """The topic of this name, created when there is none."""
    conn.execute(
        "INSERT OR IGNORE INTO topics (name, display_name) VALUES (?, ?)", (name, name.title())
    )
    return conn.execute("SELECT id FROM topics WHERE name = ?", (name,)).fetchone()[0]


def email(
    conn: sqlite3.Connection,
    *,
    subject: str = "Quarterly plan",
    sender: Contact | None = ALICE,
    to: Iterable[Contact] = (BOB,),
    cc: Iterable[Contact] = (),
    topics: Iterable[str] = (),
    **columns,
) -> int:
    """An email with its people (sender, recipients, cc) and its thread.

    The thread is the one the loader falls back to when Outlook gives none: the subject
    with its Re:/Fwd: prefixes stripped, so a reply joins the mail it answers. Pass
    conversation_id for another.
    """
    values = {
        "message_id": _next(conn, "emails", "message_id"),
        "date_received": ago(days=1),
        "subject": subject,
        "sender_name": sender[0] if sender else None,
        "sender_address": sender[1] if sender else None,
        "summary": f"Summary of {subject}",
        "content": f"Body of {subject}",
        "mailbox_name": "Inbox",
        "conversation_id": subject_to_conversation_id(subject),
        **columns,
    }
    email_id = _insert(conn, "emails", values)
    links = [("sender", sender)] if sender else []
    links += [("recipient", contact) for contact in to]
    links += [("cc", contact) for contact in cc]
    for role, (name, address) in links:
        conn.execute(
            "INSERT OR IGNORE INTO email_people (email_id, person_id, role_in_email)"
            " VALUES (?, ?, ?)",
            (email_id, person(conn, name, address), role),
        )
    for name in topics:
        conn.execute(
            "INSERT OR IGNORE INTO email_topics (email_id, topic_id) VALUES (?, ?)",
            (email_id, topic(conn, name)),
        )
    conn.commit()
    return email_id


def attachment(
    conn: sqlite3.Connection,
    *,
    email_id: int | None = None,
    filename: str = "report.pdf",
    text: str | None = "The text of the report.",
    summary: str | None = None,
    content_columns: dict | None = None,
    **columns,
) -> int:
    """An attachment of an email (a new one unless email_id is given) with its content.

    The content row holds the text extracted from the file; text=None registers the file
    with no content yet, as the export leaves it. A summary marks the content summarised
    (llm_status 'extracted'); without one it waits ('pending'). content_columns sets any
    other column of attachment_content.
    """
    if email_id is None:
        email_id = email(conn)
    message_id = conn.execute("SELECT message_id FROM emails WHERE id = ?", (email_id,)).fetchone()[
        0
    ]
    values = {
        "email_id": email_id,
        "message_id": message_id,
        "filename": filename,
        "mime_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
        "file_size": 1024,
        "file_path": f"/attachments/{message_id}/{filename}",
        "exported_at": ago(days=1),
        **columns,
    }
    attachment_id = _insert(conn, "attachments", values)
    if text is not None:
        content = {
            "attachment_id": attachment_id,
            "extracted_text": text,
            "extraction_method": "pymupdf",
            "extraction_status": "extracted",
            "extracted_at": ago(days=1),
            "summary": summary,
            "llm_status": "extracted" if summary else "pending",
            "llm_extracted_at": ago(days=1) if summary else None,
            **(content_columns or {}),
        }
        _insert(conn, "attachment_content", content)
    conn.commit()
    return attachment_id


def teams_chat(conn: sqlite3.Connection, **columns) -> int:
    """A Teams group chat."""
    n = _next(conn, "teams_chats")
    values = {
        "teams_chat_id": f"19:chat-{n}@thread.v2",
        "chat_kind": "group",
        "topic": "Project chat",
        "first_seen_at": ago(days=7),
        **columns,
    }
    chat_id = _insert(conn, "teams_chats", values)
    conn.commit()
    return chat_id


def teams_thread(conn: sqlite3.Connection, **columns) -> int:
    """A summarised Teams conversation, in a new chat unless chat_id is given."""
    if "chat_id" not in columns:
        columns["chat_id"] = teams_chat(conn)
    values = {
        "thread_kind": "chat_session",
        "started_at": ago(days=1, hours=1),
        "ended_at": ago(days=1),
        "message_count": 0,
        "title": "Project update",
        "summary": "The team agreed the next steps.",
        "extraction_status": "extracted",
        **columns,
    }
    thread_id = _insert(conn, "teams_threads", values)
    conn.commit()
    return thread_id


def teams_message(conn: sqlite3.Connection, *, sender: Contact = ALICE, **columns) -> int:
    """A Teams message in a thread and its chat, both new unless given.

    Given thread_id alone, the message lands in that thread's chat. Pass thread_id=None
    for a message not yet threaded.
    """
    if "chat_id" not in columns:
        if columns.get("thread_id") is not None:
            columns["chat_id"] = conn.execute(
                "SELECT chat_id FROM teams_threads WHERE id = ?", (columns["thread_id"],)
            ).fetchone()[0]
        else:
            columns["chat_id"] = teams_chat(conn)
    if "thread_id" not in columns:
        columns["thread_id"] = teams_thread(conn, chat_id=columns["chat_id"])
    n = _next(conn, "teams_messages")
    values = {
        "teams_message_id": f"{1_700_000_000_000 + n}",
        "sender_display_name": sender[0],
        "composed_at": ago(days=1),
        "content_text": "Morning all.",
        "is_system": 0,
        **columns,
    }
    message_id = _insert(conn, "teams_messages", values)
    conn.commit()
    return message_id


def _an_hour_after(start: str) -> str:
    try:
        when = datetime.fromisoformat(start.replace("Z", "+00:00"))
    except ValueError:
        return start
    return (when + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def calendar_event(
    conn: sqlite3.Connection,
    *,
    organizer: Contact = OLGA,
    attendees: Iterable[Contact] = (ALICE,),
    **columns,
) -> int:
    """A one-hour meeting tomorrow, with its organiser and attendees.

    The organiser is listed among the attendees, as the export lists them. A body_summary
    marks the event summarised (llm_status 'extracted').
    """
    n = _next(conn, "calendar_events")
    start = columns.pop("start_at", ago(days=-1))
    values = {
        "outlook_event_id": f"event-{n}",
        "subject": "Steering committee",
        "organizer_name": organizer[0],
        "organizer_email": organizer[1],
        "start_at": start,
        "end_at": _an_hour_after(start),
        "response_status": "Accepted",
        "ingested_at": ago(days=1),
        **columns,
    }
    if values.get("body_summary") and "llm_status" not in columns:
        values["llm_status"] = "extracted"
    event_id = _insert(conn, "calendar_events", values)
    listed = [(organizer, "Organizer", 1)] + [(contact, "Accepted", 0) for contact in attendees]
    for (name, address), response, is_organizer in listed:
        conn.execute(
            "INSERT INTO event_attendees (event_id, person_id, email, name, response_status,"
            " is_organizer) VALUES (?, ?, ?, ?, ?, ?)",
            (event_id, _person_id(conn, address), address, name, response, is_organizer),
        )
    conn.commit()
    return event_id


def _from_somewhere(conn: sqlite3.Connection, columns: dict) -> dict:
    """An email as the source, unless the caller named one (None included)."""
    if not any(key in columns for key in _SOURCES):
        columns["email_id"] = email(conn)
    return columns


def decision(conn: sqlite3.Connection, decision: str = "Adopt the plan", **columns) -> int:
    """A decision, taken in a new email unless a source column says where."""
    values = {
        "decision": decision,
        "decided_by": ALICE[0],
        "decision_date": ago(days=1)[:10],
        **_from_somewhere(conn, columns),
    }
    decision_id = _insert(conn, "decisions", values)
    conn.commit()
    return decision_id


def action_item(conn: sqlite3.Connection, task: str = "Draft the budget", **columns) -> int:
    """An open action item due in a week, from a new email unless a source column says where."""
    values = {
        "task": task,
        "owner": ALICE[0],
        "deadline": ago(days=-7)[:10],
        "status": "open",
        **_from_somewhere(conn, columns),
    }
    action_id = _insert(conn, "action_items", values)
    conn.commit()
    return action_id
