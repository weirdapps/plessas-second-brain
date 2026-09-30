"""Forget documents: every row they left in the store, and their vectors.

Used when a document must not answer searches any more: a synced document byte-identical to
a mail attachment (scripts/forget_duplicate_documents.py), and, in a later change, a session
note replaced by its newer version. Every foreign key into emails and attachments is NO
ACTION, so the children are deleted first. The full-text rows follow through their delete
triggers. The vectors are an email's positive id and an attachment's -attachment_content.id
(src/store/embeddings.py).
"""

import sqlite3

from src.store.embeddings import remove_vectors

# Children of emails, deleted before their parents.
_BY_EMAIL = (
    "key_facts",
    "decisions",
    "action_items",
    "email_topics",
    "email_people",
    "commitments",
    "email_html",
)
# Stays well under SQLite's bound-variable limit on any build.
CHUNK = 500


def _marks(n: int) -> str:
    return ",".join("?" * n)


def forget_documents(conn: sqlite3.Connection, message_ids) -> dict:
    """Delete the documents with these message ids, their children and their vectors."""
    ids = [int(m) for m in message_ids]
    stats = {"emails": 0, "attachments": 0, "vectors": 0}
    vectors: set[int] = set()
    for start in range(0, len(ids), CHUNK):
        chunk = ids[start : start + CHUNK]
        email_ids = [
            r[0]
            for r in conn.execute(
                f"SELECT id FROM emails WHERE message_id IN ({_marks(len(chunk))})", chunk
            )
        ]
        if not email_ids:
            continue
        em = _marks(len(email_ids))
        att_ids = [
            r[0]
            for r in conn.execute(f"SELECT id FROM attachments WHERE email_id IN ({em})", email_ids)
        ]
        vectors.update(email_ids)
        for table in _BY_EMAIL:
            conn.execute(f"DELETE FROM {table} WHERE email_id IN ({em})", email_ids)
        if att_ids:
            am = _marks(len(att_ids))
            vectors.update(
                -r[0]
                for r in conn.execute(
                    f"SELECT id FROM attachment_content WHERE attachment_id IN ({am})", att_ids
                )
            )
            conn.execute(f"DELETE FROM attachment_content WHERE attachment_id IN ({am})", att_ids)
            conn.execute(f"DELETE FROM attachments WHERE id IN ({am})", att_ids)
        conn.execute(f"DELETE FROM emails WHERE id IN ({em})", email_ids)
        conn.commit()
        stats["emails"] += len(email_ids)
        stats["attachments"] += len(att_ids)
    stats["vectors"] = remove_vectors(vectors)
    return stats
