"""Thread rollups (issue 179): one row per mail thread, rebuilt by rule when the thread grows.

thread_rollups holds, per conversation_id: the subject, the first and last message, the
count, the participants (by address, most messages first), a summary, the thread's current
decisions and its open questions (what is still owed: the open action items and commitments,
as the topic pages define them). All of it is read from the stored rows, so no model is
called: the summary is the newest message's extracted summary, which the extraction already
wrote in the light of the thread's earlier state (issue 164).

The table comes with the gated step thread_identity. On a store without it nothing here
writes. The loader refreshes the thread of each new email; `brain maint thread-rollups`
fills the threads of history in batches, newest first.
"""

import json
import sqlite3
from datetime import UTC, datetime

from src.store.schema import subject_to_conversation_id

TABLE = """CREATE TABLE IF NOT EXISTS thread_rollups (
    conversation_id TEXT PRIMARY KEY,
    subject TEXT,
    first_at TEXT,
    last_at TEXT,
    emails INTEGER NOT NULL,
    participants TEXT NOT NULL,
    summary TEXT,
    decisions TEXT NOT NULL,
    open_questions TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT 'rule',
    updated_at TEXT NOT NULL
)"""
INDEX = "CREATE INDEX IF NOT EXISTS idx_thread_rollups_last ON thread_rollups(last_at)"

MAX_PARTICIPANTS = 20
MAX_ITEMS = 20
_BLANK = subject_to_conversation_id("")
# An item is current when no consolidation label hid it (the v35 state columns).
_CURRENT = "{a}.canonical_id IS NULL AND {a}.superseded_by IS NULL AND {a}.invalid_at IS NULL"


def create(conn: sqlite3.Connection) -> None:
    conn.execute(TABLE)
    conn.execute(INDEX)


def enabled(conn: sqlite3.Connection) -> bool:
    from src.store import thread_identity

    return thread_identity.enabled(conn)


def _threaded(conversation_id: str | None) -> bool:
    cid = (conversation_id or "").strip()
    return bool(cid) and cid != _BLANK and not cid.startswith("news:")


def build(conn: sqlite3.Connection, conversation_id: str) -> dict | None:
    """The rollup of one thread from its stored rows, or None when it holds no mail."""
    emails = conn.execute(
        "SELECT id, date_received, subject, summary FROM emails WHERE conversation_id = ?"
        " AND COALESCE(source_class, 'mail') IN ('mail', 'automation')"
        " ORDER BY date_received, id",
        (conversation_id,),
    ).fetchall()
    if not emails:
        return None
    ids = json.dumps([e[0] for e in emails])
    inside = "SELECT value FROM json_each(?)"
    participants = [
        {"name": name, "address": address, "emails": n}
        for name, address, n in conn.execute(
            "SELECT p.name, p.email, COUNT(DISTINCT ep.email_id) n FROM email_people ep"
            f" JOIN people p ON p.id = ep.person_id WHERE ep.email_id IN ({inside})"
            " GROUP BY p.id ORDER BY n DESC, p.name LIMIT ?",
            (ids, MAX_PARTICIPANTS),
        )
    ]
    decisions = [
        {"id": i, "decision": text, "decided_by": by, "date": date}
        for i, text, by, date in conn.execute(
            "SELECT d.id, d.decision, d.decided_by, COALESCE(d.decision_date, e.date_received)"
            f" FROM decisions d JOIN emails e ON e.id = d.email_id WHERE d.email_id IN ({inside})"
            f" AND {_CURRENT.format(a='d')} ORDER BY e.date_received DESC, d.id DESC LIMIT ?",
            (ids, MAX_ITEMS),
        )
    ]
    open_questions = [
        {"kind": "action", "id": i, "text": text, "owner": owner, "due": due}
        for i, text, owner, due in conn.execute(
            "SELECT a.id, a.task, a.owner, a.deadline FROM action_items a"
            f" WHERE a.email_id IN ({inside}) AND COALESCE(a.status, 'open') = 'open'"
            f" AND {_CURRENT.format(a='a')} ORDER BY a.id DESC LIMIT ?",
            (ids, MAX_ITEMS),
        )
    ]
    open_questions += [
        {"kind": "commitment", "id": i, "text": text, "owner": by, "due": due}
        for i, text, by, due in conn.execute(
            "SELECT c.id, c.commitment, c.by_person, c.due FROM commitments c"
            f" WHERE c.email_id IN ({inside}) AND COALESCE(c.status, 'open') = 'open'"
            f" AND {_CURRENT.format(a='c')} ORDER BY c.id DESC LIMIT ?",
            (ids, MAX_ITEMS),
        )
    ]
    summary = next((e[3] for e in reversed(emails) if e[3]), None)
    return {
        "conversation_id": conversation_id,
        "subject": emails[0][2],
        "first_at": emails[0][1],
        "last_at": emails[-1][1],
        "emails": len(emails),
        "participants": participants,
        "summary": summary,
        "decisions": decisions,
        "open_questions": open_questions[:MAX_ITEMS],
    }


def refresh(conn: sqlite3.Connection, conversation_id: str | None) -> bool:
    """Rebuild one thread's rollup; whether a row was written. Nothing on a store without
    the step, for news, the blank-subject thread or an empty id. The caller commits."""
    if not _threaded(conversation_id) or not enabled(conn):
        return False
    assert conversation_id is not None
    rollup = build(conn, conversation_id)
    if rollup is None:
        conn.execute("DELETE FROM thread_rollups WHERE conversation_id = ?", (conversation_id,))
        return False
    conn.execute(
        "INSERT OR REPLACE INTO thread_rollups (conversation_id, subject, first_at, last_at,"
        " emails, participants, summary, decisions, open_questions, method, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'rule', ?)",
        (
            conversation_id,
            rollup["subject"],
            rollup["first_at"],
            rollup["last_at"],
            rollup["emails"],
            json.dumps(rollup["participants"], ensure_ascii=False),
            rollup["summary"],
            json.dumps(rollup["decisions"], ensure_ascii=False),
            json.dumps(rollup["open_questions"], ensure_ascii=False),
            datetime.now(UTC).isoformat(timespec="seconds"),
        ),
    )
    return True


def backfill(conn: sqlite3.Connection, limit: int = 500) -> int:
    """Rollups for up to `limit` threads that have none or whose newest mail is newer than
    theirs, newest first; the rows written. The caller commits."""
    if not enabled(conn):
        return 0
    keys = [
        r[0]
        for r in conn.execute(
            "SELECT e.conversation_id, MAX(e.date_received) last FROM emails e"
            " LEFT JOIN thread_rollups r ON r.conversation_id = e.conversation_id"
            " WHERE COALESCE(e.source_class, 'mail') IN ('mail', 'automation')"
            " AND TRIM(COALESCE(e.conversation_id, '')) <> '' AND e.conversation_id <> ?"
            " GROUP BY e.conversation_id"
            " HAVING MAX(r.last_at) IS NULL OR MAX(e.date_received) > MAX(r.last_at)"
            " ORDER BY last DESC LIMIT ?",
            (_BLANK, limit),
        )
    ]
    return sum(refresh(conn, key) for key in keys)
