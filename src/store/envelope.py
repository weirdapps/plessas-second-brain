"""What a row a search or a list returns says about the item it stands for.

Rows came back in the shape of the table they were read from. A search_emails row
was email_id, date, subject, summary, snippet and `source`, the field the words
matched in, so a news article, a standalone document and an internal email looked
alike, and on the replica most "email" hits were news and documents. Nothing said
which mailbox a row sat in or who sent it, a Teams row was dated `started_at` and a
calendar row `start_at`, and none said which id the tool that reads the item takes.
Every such row now also carries, under the same names wherever it comes from:

    kind         email, news, document, attachment, teams, whatsapp, calendar,
                 conversation or image. A decision, action, commitment or key fact
                 takes the kind of the item it was extracted from, since that is
                 where it was said, and the rest of its envelope describes that item.
    mailbox      the email's folder, for the kinds that have one (an attachment
                 its email's)
    who          the sender, author or organiser as the store has them: "Name
                 <address>", or whichever of the two it holds, else null. A Teams or
                 WhatsApp row names the sender of the message that matched, else
                 whoever wrote first in the thread.
    ref          the id the tool that reads the item takes: emails.id (email_thread),
                 attachments.id, teams_threads.id (teams_thread_context),
                 whatsapp_threads.id, calendar_events.id, a conversation's session_id
                 (conversation_context), an image's sha256
    date         the item's own date as stored: the email's arrival, the session's
                 or meeting's start
    ingested_at  when the store took the item in, where it records that: an
                 attachment's export, a calendar event's ingest, a conversation's
                 load. The store keeps no such time for mail, Teams or WhatsApp.
    matched_in   on a row a search matched, the field the words matched in: the
                 email's `source` (subject, summary, content, mixed, key_fact,
                 attachment), Teams' and WhatsApp's `match` (thread, message), a
                 conversation's `match_type` (summary, turn_content), or semantic

A key a row already has stays as it is: `email_id` beside `ref`, `source` beside
`matched_in` (for one release, see the README), a decision's own `date`, and the
'owner' or 'agent' `kind` a conversation row says under include_agents. Each kind
is filled by one query for the whole list after its LIMIT, as source_class is.
"""

import json
import sqlite3
from collections.abc import Callable, Iterable

from src.store.source_class import (
    AUTOMATION,
    CLASSES,
    DOCUMENT,
    MAIL,
    NEWS,
    SESSION_NOTE,
    classes_of,
)

KINDS = (
    "email",
    "news",
    "document",
    "attachment",
    "teams",
    "whatsapp",
    "calendar",
    "conversation",
    "image",
)
# The kinds a row of the emails table can be, by its source_class.
EMAIL_KINDS = ("email", "news", "document")
_KIND_OF_CLASS = {
    MAIL: "email",
    AUTOMATION: "email",
    NEWS: "news",
    DOCUMENT: "document",
    SESSION_NOTE: "document",
}
_CLASSES_OF_KIND = {
    "email": (MAIL, AUTOMATION),
    "news": (NEWS,),
    "document": (DOCUMENT, SESSION_NOTE),
}
SEMANTIC = "semantic"
# Keyword search's stages, and recall's fused rows: the values of `source` that name the
# field a search matched in. On a decision or action row `source` is its parent's kind.
MATCH_STAGES = frozenset(
    {"subject", "summary", "content", "mixed", "key_fact", "attachment", SEMANTIC}
)

# What a row whose item cannot be found says.
_UNKNOWN = {"kind": None, "who": None, "ref": None, "date": None}

Fields = dict[object, dict]


def kind_of_class(source_class: str | None) -> str:
    """The kind of an emails row of this class: news, document, or email."""
    return _KIND_OF_CLASS.get(source_class or MAIL, "email")


def classes_for(kinds: Iterable[str] | None) -> tuple[str, ...] | None:
    """The source classes an emails row of one of `kinds` may have; None when `kinds` is."""
    if kinds is None:
        return None
    wanted = {name for kind in kinds for name in _CLASSES_OF_KIND.get(kind, ())}
    return tuple(name for name in CLASSES if name in wanted)


def who(name: object, address: object) -> str | None:
    """'Name <address>' as the store has them, or whichever of the two it holds."""
    name = " ".join(str(name or "").split())
    address = str(address or "").strip()
    if name and address and name.lower() != address.lower():
        return f"{name} <{address}>"
    return name or address or None


def _ids(rows: list[dict], key: str) -> list:
    return sorted({row[key] for row in rows if row.get(key) is not None}, key=str)


def _in(ids: Iterable) -> str:
    """The JSON list an `IN (SELECT value FROM json_each(?))` takes."""
    return json.dumps(list(ids))


def _put(row: dict, fields: dict) -> None:
    for key, value in fields.items():
        if key not in row:  # a key the row already has stays as it is
            row[key] = value


# ------------------------------------------------------------ one query per kind


def _email_fields(conn: sqlite3.Connection, ids: list, classes: dict) -> Fields:
    """id -> envelope of an emails row. `classes` are those the rows already carry;
    source_class sits behind the body, so it is read only for the rest."""
    if not ids:
        return {}
    missing = [i for i in ids if i not in classes]
    if missing:
        classes = {**classes, **classes_of(conn, missing)}
    rows = conn.execute(
        "SELECT id, mailbox_name, sender_name, sender_address, date_received FROM emails"
        " WHERE id IN (SELECT value FROM json_each(?))",
        (_in(ids),),
    )
    return {
        r[0]: {
            "kind": kind_of_class(classes.get(r[0])),
            "mailbox": r[1],
            "who": who(r[2], r[3]),
            "ref": r[0],
            "date": r[4],
        }
        for r in rows
    }


def _attachment_fields(conn: sqlite3.Connection, ids: list) -> Fields:
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT a.id, a.exported_at, e.mailbox_name, e.sender_name, e.sender_address,"
        " e.date_received FROM attachments a LEFT JOIN emails e ON e.id = a.email_id"
        " WHERE a.id IN (SELECT value FROM json_each(?))",
        (_in(ids),),
    )
    return {
        r[0]: {
            "kind": "attachment",
            "mailbox": r[2],
            "who": who(r[3], r[4]),
            "ref": r[0],
            "date": r[5],
            "ingested_at": r[1],
        }
        for r in rows
    }


def _thread_fields(conn: sqlite3.Connection, ids: list, kind: str) -> Fields:
    """id -> envelope of a Teams or WhatsApp thread, `who` the first to write in it."""
    if not ids:
        return {}
    if kind == "teams":
        first = (
            "SELECT m.sender_display_name FROM teams_messages m WHERE m.thread_id = t.id"
            " AND m.is_system = 0 ORDER BY m.composed_at, m.id LIMIT 1"
        )
        table = "teams_threads"
    else:
        first = (
            "SELECT m.sender_name FROM whatsapp_messages m WHERE m.thread_id = t.id"
            " ORDER BY m.sent_at, m.id LIMIT 1"
        )
        table = "whatsapp_threads"
    try:
        rows = conn.execute(
            f"SELECT t.id, t.started_at, ({first}) FROM {table} t"
            " WHERE t.id IN (SELECT value FROM json_each(?))",
            (_in(ids),),
        ).fetchall()
    except sqlite3.OperationalError:  # a store without the table
        return {}
    return {r[0]: {"kind": kind, "who": who(r[2], None), "ref": r[0], "date": r[1]} for r in rows}


def _calendar_fields(conn: sqlite3.Connection, ids: list) -> Fields:
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT id, organizer_name, organizer_email, start_at, ingested_at FROM calendar_events"
        " WHERE id IN (SELECT value FROM json_each(?))",
        (_in(ids),),
    )
    return {
        r[0]: {
            "kind": "calendar",
            "who": who(r[1], r[2]),
            "ref": r[0],
            "date": r[3],
            "ingested_at": r[4],
        }
        for r in rows
    }


def _conversation_fields(conn: sqlite3.Connection, ids: list) -> Fields:
    """id -> envelope of a conversation. Its sessions are the owner's (or an agent's),
    whom the store does not name, so `who` is null."""
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT id, session_id, started_at, created_at FROM conversations"
        " WHERE id IN (SELECT value FROM json_each(?))",
        (_in(ids),),
    )
    return {
        r[0]: {
            "kind": "conversation",
            "who": None,
            "ref": r[1],
            "date": r[2],
            "ingested_at": r[3],
        }
        for r in rows
    }


def _image_fields(conn: sqlite3.Connection, shas: list) -> Fields:
    """sha256 -> envelope of an inline image, by the first email it appeared in."""
    if not shas:
        return {}
    # The bare columns come from the occurrence whose email is oldest (SQLite's MIN).
    first = (
        "SELECT o.sha256, MIN(e.date_received), e.sender_name, o.sender_email"
        " FROM inline_image_occurrences o LEFT JOIN emails e ON e.message_id = o.message_id"
        " WHERE o.sha256 IN (SELECT value FROM json_each(?)) GROUP BY o.sha256"
    )
    try:
        rows = conn.execute(first, (_in(shas),)).fetchall()
    except sqlite3.OperationalError:  # images kept apart from any mail: the sender alone
        rows = conn.execute(
            "SELECT sha256, NULL, NULL, MIN(sender_email) FROM inline_image_occurrences"
            " WHERE sha256 IN (SELECT value FROM json_each(?)) GROUP BY sha256",
            (_in(shas),),
        ).fetchall()
    return {
        r[0]: {"kind": "image", "who": who(r[2], r[3]), "ref": r[0], "date": r[1]} for r in rows
    }


# ---------------------------------------------------------------- rows by kind


def emails(conn: sqlite3.Connection, rows: list[dict], key: str = "email_id") -> list[dict]:
    """Rows of the emails table (mail, news, documents), or rows extracted from one
    and keyed by its id; the rows, for chaining."""
    classes = {
        row[key]: row["source_class"]
        for row in rows
        if row.get(key) is not None and "source_class" in row
    }
    fields = _email_fields(conn, _ids(rows, key), classes)
    for row in rows:
        if row.get("source") in MATCH_STAGES:
            row.setdefault("matched_in", row["source"])
        _put(row, fields.get(row.get(key)) or _UNKNOWN)
    return rows


def attachments(
    conn: sqlite3.Connection, rows: list[dict], key: str = "attachment_id"
) -> list[dict]:
    fields = _attachment_fields(conn, _ids(rows, key))
    for row in rows:
        _put(row, fields.get(row.get(key)) or _UNKNOWN)
    return rows


def _threads(conn: sqlite3.Connection, rows: list[dict], key: str, kind: str) -> list[dict]:
    fields = _thread_fields(conn, _ids(rows, key), kind)
    for row in rows:
        if "match" in row:
            row.setdefault("matched_in", row["match"])
        if row.get("matched_sender"):
            row.setdefault("who", row["matched_sender"])
        # A message not grouped into a thread yet: its own sender and time.
        loose = {"kind": kind, "who": None, "ref": None, "date": row.get("matched_at")}
        _put(row, fields.get(row.get(key)) or loose)
    return rows


def teams(conn: sqlite3.Connection, rows: list[dict], key: str = "thread_id") -> list[dict]:
    return _threads(conn, rows, key, "teams")


def whatsapp(conn: sqlite3.Connection, rows: list[dict], key: str = "thread_id") -> list[dict]:
    return _threads(conn, rows, key, "whatsapp")


def calendar(conn: sqlite3.Connection, rows: list[dict], key: str = "id") -> list[dict]:
    fields = _calendar_fields(conn, _ids(rows, key))
    for row in rows:
        _put(row, fields.get(row.get(key)) or _UNKNOWN)
    return rows


def conversations(
    conn: sqlite3.Connection, rows: list[dict], key: str = "conversation_id"
) -> list[dict]:
    fields = _conversation_fields(conn, _ids(rows, key))
    for row in rows:
        if "match_type" in row:
            row.setdefault("matched_in", row["match_type"])
        _put(row, fields.get(row.get(key)) or _UNKNOWN)
    return rows


def images(conn: sqlite3.Connection, rows: list[dict], key: str = "sha256") -> list[dict]:
    fields = _image_fields(conn, _ids(rows, key))
    for row in rows:
        _put(row, fields.get(row.get(key)) or _UNKNOWN)
    return rows


# The rows query_semantic returns, by their `type`: how each is enveloped and its id.
_SEMANTIC_TYPES: dict[str, tuple[Callable, str]] = {
    "email": (emails, "email_id"),
    "attachment": (attachments, "attachment_id"),
    "teams_thread": (teams, "id"),
    "whatsapp_thread": (whatsapp, "id"),
    "conversation": (conversations, "id"),
}


def semantic(conn: sqlite3.Connection, rows: list[dict]) -> list[dict]:
    """Rows a similarity search ranked, of any kind, each by its `type`."""
    for row in rows:
        row.setdefault("matched_in", SEMANTIC)
    for type_, (envelope, key) in _SEMANTIC_TYPES.items():
        envelope(conn, [row for row in rows if row.get("type") == type_], key)
    return rows


# -------------------------------------------- what was extracted from an item

# The tables of extracted items, and the order their parent is taken in: the order
# of the `source` CASE every query of them uses, email first.
_EXTRACTED = ("decisions", "action_items", "commitments", "key_facts")
_PARENTS = (
    ("email_id", "email"),
    ("teams_thread_id", "teams"),
    ("whatsapp_thread_id", "whatsapp"),
    ("event_id", "calendar"),
)


def extracted(conn: sqlite3.Connection, rows: list[dict], table: str, key: str) -> list[dict]:
    """Rows of decisions, action_items, commitments or key_facts, keyed by their own id
    at `key`, each given the envelope of the item it was extracted from: an email, a
    Teams or WhatsApp thread, a meeting, or a conversation. A row whose parent is gone
    says kind null, as its `source` says 'orphan'."""
    if table not in _EXTRACTED:
        raise ValueError(f"not a table of extracted items: {table}")
    ids = _ids(rows, key)
    if not ids:
        for row in rows:
            _put(row, _UNKNOWN)
        return rows
    columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    select = [f"x.{name}" if name in columns else "NULL" for name, _ in _PARENTS]
    if "conversation_turn_id" in columns:
        select.append("ct.conversation_id")
        join = " LEFT JOIN conversation_turns ct ON ct.id = x.conversation_turn_id"
    else:
        select.append("NULL")
        join = ""
    parents = {
        r[0]: r[1:]
        for r in conn.execute(
            f"SELECT x.id, {', '.join(select)} FROM {table} x{join}"
            " WHERE x.id IN (SELECT value FROM json_each(?))",
            (_in(ids),),
        )
    }

    def wanted(slot: int) -> list:
        return sorted({p[slot] for p in parents.values() if p[slot] is not None}, key=str)

    email_classes = {
        parents[row[key]][0]: row["source_class"]
        for row in rows
        if row.get(key) in parents
        and parents[row[key]][0] is not None
        and row.get("source_class") is not None
    }
    found = (
        _email_fields(conn, wanted(0), email_classes),
        _thread_fields(conn, wanted(1), "teams"),
        _thread_fields(conn, wanted(2), "whatsapp"),
        _calendar_fields(conn, wanted(3)),
        _conversation_fields(conn, wanted(4)),
    )
    for row in rows:
        chosen = None
        for slot, parent in enumerate(parents.get(row.get(key), ())):
            if parent is not None and parent in found[slot]:
                chosen = found[slot][parent]
                break
        _put(row, chosen or _UNKNOWN)
    return rows
