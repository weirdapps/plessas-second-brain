"""One reader for the item behind any row's `ref`, for the get_item MCP tool.

Every row a search returns says its `kind` and `ref` (src/store/envelope.py), and most
kinds had no reader: an attachment's text, a WhatsApp session and a meeting by id
could only be had through sql_query, where agents paged extracted text with substr().
read_item takes any kind a row can be and gives the item's metadata, what was
extracted from it, and its text as one string, which the caller pages:

    email, news, document  the body, with the thread, the people on it and the
                           attachments (their refs and sizes)
    attachment             the extracted text, with the email it came with and, for a
                           long document, where each of its chunks lies
    teams, whatsapp        the messages in order, one line each
    calendar               the invitation's text, then every attendee
    conversation           the turns in full, one after another
    image                  the vision transcription, else the description

Readers only read; a store too old for a column or table raises sqlite3's error,
which the MCP layer turns into words.
"""

import json
import sqlite3
from typing import Any

from src.store import envelope

# How many emails of a thread come with an email: centred on it, as email_thread
# gives them, with thread_total counting the rest.
THREAD_ROWS = 20
# How many rows of each extracted list and of an email's people come with an item.
EXTRACTED_ROWS = 100


class ItemNotFound(LookupError):
    """No item of that kind has that ref; the message says where refs come from."""


class ItemRefused(ValueError):
    """A ref of the wrong shape, or an item that needs a switch; the message says which."""


# Where a ref of each kind comes from, for the message when it matches nothing.
_FROM = {
    "email": "search_emails, query_emails or recall",
    "attachment": "search_attachments or recall",
    "teams": "search_teams or recall",
    "whatsapp": "search_whatsapp or recall",
    "calendar": "query_calendar_events or recall",
    "conversation": "search_conversations or recall",
    "image": "attachment_image_search or recall",
}


def _missing(kind: str, ref: Any) -> ItemNotFound:
    family = "email" if kind in envelope.EMAIL_KINDS else kind
    return ItemNotFound(f"no {kind} has ref {ref!r}; take ref from a {_FROM[family]} row")


def _int_ref(kind: str, ref: Any) -> int:
    """A ref that is a row id, as an int."""
    if isinstance(ref, bool):
        raise ItemRefused(f"a {kind} ref is a number, as its rows give it; got {ref!r}")
    try:
        return int(str(ref).strip())
    except ValueError:
        raise ItemRefused(f"a {kind} ref is a number, as its rows give it; got {ref!r}") from None


def _rows(conn: sqlite3.Connection, sql: str, *args: Any) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, args).fetchall()]


def _extracted(conn: sqlite3.Connection, column: str, item_id: Any, *, facts: bool = True) -> dict:
    """The decisions, action items and key facts whose parent `column` is `item_id`."""
    out = {
        "decisions": _rows(
            conn,
            f"SELECT decision, decided_by, decision_date, attachment_id FROM decisions "
            f"WHERE {column} = ? ORDER BY id LIMIT {EXTRACTED_ROWS}",
            item_id,
        ),
        "action_items": _rows(
            conn,
            f"SELECT task, owner, deadline, status, attachment_id FROM action_items "
            f"WHERE {column} = ? ORDER BY id LIMIT {EXTRACTED_ROWS}",
            item_id,
        ),
    }
    if facts:
        out["key_facts"] = _rows(
            conn,
            f"SELECT fact, attachment_id FROM key_facts WHERE {column} = ? "
            f"ORDER BY id LIMIT {EXTRACTED_ROWS}",
            item_id,
        )
    return out


def _unfolded(row: dict) -> dict:
    """A row without the folded *_f copies the full-text indexes read."""
    return {key: value for key, value in row.items() if not key.endswith("_f")}


# ------------------------------------------------------------------ email


def _email(conn: sqlite3.Connection, kind: str, ref: Any, **_: Any) -> dict:
    from src.store.query import count_thread, query_thread

    email_id = _int_ref(kind, ref)
    row = conn.execute(
        "SELECT id, date_received, subject, sender_name, sender_address, mailbox_name, "
        "source_class, conversation_id, summary, sentiment, urgency, language, content "
        "FROM emails WHERE id = ?",
        (email_id,),
    ).fetchone()
    if row is None:
        raise _missing(kind, ref)
    item = {
        "kind": envelope.kind_of_class(row["source_class"]),
        "subject": row["subject"],
        "date": row["date_received"],
        "who": envelope.who(row["sender_name"], row["sender_address"]),
        "mailbox": row["mailbox_name"],
        "source_class": row["source_class"],
        "conversation_id": row["conversation_id"],
        "summary": row["summary"],
        "sentiment": row["sentiment"],
        "urgency": row["urgency"],
        "language": row["language"],
        "people": _rows(
            conn,
            "SELECT p.name, p.email, ep.role_in_email AS role FROM email_people ep "
            "JOIN people p ON p.id = ep.person_id WHERE ep.email_id = ? "
            f"ORDER BY ep.role_in_email <> 'sender', ep.role_in_email, p.name "
            f"LIMIT {EXTRACTED_ROWS}",
            email_id,
        ),
        "attachments": _rows(
            conn,
            "SELECT a.id AS ref, a.filename, a.mime_type, a.file_size, a.is_inline, "
            "LENGTH(ac.extracted_text) AS text_chars, ac.extraction_status "
            "FROM attachments a LEFT JOIN attachment_content ac ON ac.attachment_id = a.id "
            f"WHERE a.email_id = ? ORDER BY a.id LIMIT {EXTRACTED_ROWS}",
            email_id,
        ),
        "thread_total": count_thread(conn, email_id),
        "thread": [
            {key: t.get(key) for key in ("kind", "ref", "date", "who", "subject", "summary")}
            for t in query_thread(conn, email_id, limit=THREAD_ROWS)
        ],
        "extracted": {
            **_extracted(conn, "email_id", email_id),
            "commitments": _rows(
                conn,
                "SELECT commitment, by_person, to_person FROM commitments WHERE email_id = ? "
                f"ORDER BY id LIMIT {EXTRACTED_ROWS}",
                email_id,
            ),
        },
    }
    return {"item": item, "text": row["content"] or ""}


# ------------------------------------------------------------------ attachment


def _attachment(conn: sqlite3.Connection, kind: str, ref: Any, **_: Any) -> dict:
    attachment_id = _int_ref(kind, ref)
    row = conn.execute(
        "SELECT a.id, a.email_id, a.filename, a.mime_type, a.file_size, a.is_inline, "
        "a.exported_at, a.sha256, ac.extracted_text, ac.extraction_method, "
        "ac.extraction_status, ac.extraction_error, ac.summary, ac.language, ac.llm_status "
        "FROM attachments a LEFT JOIN attachment_content ac ON ac.attachment_id = a.id "
        "WHERE a.id = ? ORDER BY ac.id DESC LIMIT 1",
        (attachment_id,),
    ).fetchone()
    if row is None:
        raise _missing(kind, ref)
    email = conn.execute(
        "SELECT id, date_received, subject, sender_name, sender_address, mailbox_name, "
        "source_class FROM emails WHERE id = ?",
        (row["email_id"],),
    ).fetchone()
    item = {
        key: row[key]
        for key in (
            "filename",
            "mime_type",
            "file_size",
            "is_inline",
            "exported_at",
            "sha256",
            "extraction_method",
            "extraction_status",
            "extraction_error",
            "language",
            "llm_status",
            "summary",
        )
    }
    item["email"] = None
    if email is not None:
        item["email"] = {
            "kind": envelope.kind_of_class(email["source_class"]),
            "ref": email["id"],
            "date": email["date_received"],
            "who": envelope.who(email["sender_name"], email["sender_address"]),
            "subject": email["subject"],
            "mailbox": email["mailbox_name"],
            "source_class": email["source_class"],
        }
    item["extracted"] = _extracted(conn, "attachment_id", attachment_id)
    item.update(_chunks(conn, attachment_id))
    return {"item": item, "text": row["extracted_text"] or ""}


def _chunks(conn: sqlite3.Connection, attachment_id: int) -> dict:
    """The passages a long document was cut into for its chunk vectors (schema v39), where
    each lies in the text: a search row whose passage matched gives its chunk_offset, the
    `offset` that opens it here. {} for a document with none, and on a store before v39."""
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM attachment_chunks WHERE attachment_id = ?", (attachment_id,)
        ).fetchone()[0]
    except sqlite3.OperationalError:  # a store before v39
        return {}
    if not total:
        return {}
    rows = _rows(
        conn,
        "SELECT chunk_no, start_char AS chunk_offset, end_char AS chunk_end FROM "
        f"attachment_chunks WHERE attachment_id = ? ORDER BY chunk_no LIMIT {EXTRACTED_ROWS}",
        attachment_id,
    )
    return {"chunks_total": total, "chunks": rows}


# ------------------------------------------------------------------ Teams, WhatsApp


def _lines(messages: list[dict], when: str, who: str, text: str) -> str:
    """Messages as a transcript, one line each: [time] sender: text."""
    return "\n".join(
        f"[{m.get(when) or ''}] {m.get(who) or '(unknown)'}: {m.get(text) or ''}" for m in messages
    )


def _teams(conn: sqlite3.Connection, kind: str, ref: Any, **_: Any) -> dict:
    from src.store.teams_query import thread_context

    thread_id = _int_ref(kind, ref)
    found = thread_context(conn, thread_id)
    if "error" in found:
        raise _missing(kind, ref)
    item = _unfolded(found["thread"])
    item["extracted"] = {
        "decisions": found["decisions"][:EXTRACTED_ROWS],
        "action_items": found["action_items"][:EXTRACTED_ROWS],
        "key_facts": found["key_facts"][:EXTRACTED_ROWS],
    }
    messages = [
        {**m, "sender_display_name": f"{m['sender_display_name'] or 'system'} (system)"}
        if m.get("is_system")
        else m
        for m in found["messages"]
    ]
    return {
        "item": item,
        "text": _lines(messages, "composed_at", "sender_display_name", "content_text"),
    }


def _whatsapp(conn: sqlite3.Connection, kind: str, ref: Any, **_: Any) -> dict:
    thread_id = _int_ref(kind, ref)
    row = conn.execute(
        "SELECT t.id, t.chat_id, c.name AS chat_name, c.chat_kind, c.chat_jid, t.started_at, "
        "t.ended_at, t.message_count, t.participant_names, t.title, t.summary, t.sentiment, "
        "t.language FROM whatsapp_threads t JOIN whatsapp_chats c ON c.id = t.chat_id "
        "WHERE t.id = ?",
        (thread_id,),
    ).fetchone()
    if row is None:
        raise _missing(kind, ref)
    item = dict(row)
    try:
        item["participant_names"] = json.loads(item["participant_names"] or "[]")
    except ValueError:
        pass  # kept as stored
    item["extracted"] = _extracted(conn, "whatsapp_thread_id", thread_id)
    messages = []
    for m in _rows(
        conn,
        "SELECT sent_at, sender_name, is_from_me, content, media_type, filename "
        "FROM whatsapp_messages WHERE thread_id = ? ORDER BY sent_at, id",
        thread_id,
    ):
        text = m["content"] or ""
        if m["media_type"]:
            media = " ".join(part for part in (m["media_type"], m["filename"]) if part)
            text = f"{text} [{media}]".strip()
        sender = m["sender_name"] or ("(me)" if m["is_from_me"] else None)
        messages.append({"sent_at": m["sent_at"], "sender": sender, "text": text})
    return {"item": item, "text": _lines(messages, "sent_at", "sender", "text")}


# ------------------------------------------------------------------ calendar


def _calendar(conn: sqlite3.Connection, kind: str, ref: Any, **_: Any) -> dict:
    from src.extract.html_text import html_to_text, looks_like_html
    from src.store.calendar_query import athens_time

    event_id = _int_ref(kind, ref)
    row = conn.execute(
        "SELECT id, subject, start_at, end_at, location, organizer_name, organizer_email, "
        "is_recurring, recurrence_master_id, response_status, is_self_organized, "
        "is_cancelled, body_summary, body, ingested_at FROM calendar_events WHERE id = ?",
        (event_id,),
    ).fetchone()
    if row is None:
        raise _missing(kind, ref)
    attendees = _rows(
        conn,
        "SELECT name, email, response_status, is_organizer, is_self FROM event_attendees "
        "WHERE event_id = ? ORDER BY is_self DESC, is_organizer DESC, id",
        event_id,
    )
    item = {key: row[key] for key in row.keys() if key != "body"}
    item["start_local"] = athens_time(row["start_at"])
    item["end_local"] = athens_time(row["end_at"])
    item["who"] = envelope.who(row["organizer_name"], row["organizer_email"])
    item["attendees_total"] = len(attendees)
    item["extracted"] = _extracted(conn, "event_id", event_id, facts=False)
    body = row["body"] or ""
    if looks_like_html(body):
        body = html_to_text(body)
    lines = []
    for a in attendees:
        marks = "".join(
            mark
            for flag, mark in ((a["is_organizer"], " (organiser)"), (a["is_self"], " (you)"))
            if flag
        )
        lines.append(f"{envelope.who(a['name'], a['email'])}: {a['response_status'] or ''}{marks}")
    roster = f"Attendees ({len(attendees)}):\n" + "\n".join(lines)
    return {"item": item, "text": f"{body.strip()}\n\n{roster}".strip()}


# ------------------------------------------------------------------ conversation


def _conversation(
    conn: sqlite3.Connection, kind: str, ref: Any, *, include_agents: bool = False, **_: Any
) -> dict:
    from src.store.conversation_query import get_conversation_context

    found = get_conversation_context(conn, str(ref), include_agents=include_agents)
    if "error" in found:
        if found.get("kind") == "agent":
            raise ItemRefused(found["error"])
        raise _missing(kind, ref)
    item = {key: value for key, value in found.items() if key != "turns"}
    item["extracted"] = {
        "decisions": item.pop("decisions")[:EXTRACTED_ROWS],
        "action_items": item.pop("action_items")[:EXTRACTED_ROWS],
        "key_facts": item.pop("key_facts")[:EXTRACTED_ROWS],
    }
    turns = _rows(
        conn,
        "SELECT timestamp, speaker, content FROM conversation_turns "
        "WHERE conversation_id = ? ORDER BY turn_index",
        found["id"],
    )
    text = "\n\n".join(
        f"[{t['timestamp'] or ''}] {t['speaker']}: {t['content'] or ''}" for t in turns
    )
    return {"item": item, "text": text}


# ------------------------------------------------------------------ image


def _image(conn: sqlite3.Connection, kind: str, ref: Any, **_: Any) -> dict:
    row = conn.execute(
        "SELECT sha256, width, height, bytes, classification, classified_at, "
        "vision_description, vision_transcription FROM inline_images WHERE sha256 = ?",
        (str(ref),),
    ).fetchone()
    if row is None:
        raise _missing(kind, ref)
    occurrences = _rows(
        conn,
        "SELECT o.message_id, o.sender_email, o.position_in_body, "
        "(SELECT e.id FROM emails e WHERE e.message_id = o.message_id LIMIT 1) AS email_ref "
        f"FROM inline_image_occurrences o WHERE o.sha256 = ? ORDER BY o.message_id "
        f"LIMIT {EXTRACTED_ROWS}",
        str(ref),
    )
    item = {key: row[key] for key in row.keys() if key != "vision_transcription"}
    item["description"] = item.pop("vision_description")
    item["occurrences"] = occurrences
    text = row["vision_transcription"] or row["vision_description"] or ""
    return {"item": item, "text": text}


_READERS = {
    "email": _email,
    "news": _email,
    "document": _email,
    "attachment": _attachment,
    "teams": _teams,
    "whatsapp": _whatsapp,
    "calendar": _calendar,
    "conversation": _conversation,
    "image": _image,
}


def read_item(
    conn: sqlite3.Connection, kind: str, ref: Any, *, include_agents: bool = False
) -> dict:
    """{"item": metadata and what was extracted, "text": the item's text, whole}.

    Raises ItemNotFound for a ref no item of `kind` has, and ItemRefused for a ref of
    the wrong shape or an agent session without include_agents.
    """
    if kind not in _READERS:
        raise ItemRefused(f"kind must be one of {', '.join(envelope.KINDS)}; got {kind!r}")
    return _READERS[kind](conn, kind, ref, include_agents=include_agents)
