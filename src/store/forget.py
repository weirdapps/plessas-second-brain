"""Forget documents: every row they left in the store, and their vectors.

Used when a document must not answer searches any more: a synced document byte-identical to
a mail attachment (scripts/forget_duplicate_documents.py), and, in a later change, a session
note replaced by its newer version. Since the follow-up to issue 174 the deleting is
src/store/retention.py's, the one forget path: the children before their parents (every
foreign key into emails and attachments is NO ACTION), the full-text rows through their delete
triggers, the vectors with their chunks, the figures of an attachment with its series read
again, and the aliases, thread-id map rows, item labels and inline images this module left.
"""

import sqlite3

# Stays well under SQLite's bound-variable limit on any build.
CHUNK = 500


def _marks(n: int) -> str:
    return ",".join("?" * n)


def forget_documents(conn: sqlite3.Connection, message_ids) -> dict:
    """Delete the documents with these message ids, their children and their vectors."""
    ids = [int(m) for m in message_ids]
    email_ids: list[int] = []
    for start in range(0, len(ids), CHUNK):
        chunk = ids[start : start + CHUNK]
        email_ids += [
            r[0]
            for r in conn.execute(
                f"SELECT id FROM emails WHERE message_id IN ({_marks(len(chunk))})", chunk
            )
        ]
    return forget_emails(conn, email_ids)


def forget_emails(conn: sqlite3.Connection, ids) -> dict:
    """Delete the rows of emails with these row ids (a document or a news item is one),
    everything they left and their vectors, through the one forget path
    (src/store/retention.py, issue 174). Files stay on disk and no full-text index is
    merged: the callers forget a replaced or duplicate document in passing."""
    from src.store.retention import forget

    ids = [int(i) for i in ids]
    if not ids:
        return {"emails": 0, "attachments": 0, "vectors": 0}
    result = forget(conn, email_ids=ids, apply=True, files=False, purge=False)
    return {
        "emails": result.tables.get("emails", 0),
        "attachments": result.tables.get("attachments", 0),
        "vectors": result.vectors,
    }
