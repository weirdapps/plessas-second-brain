"""Forgetting a document removes every row it left and its vectors, and nothing else.

Every foreign key into emails and attachments is NO ACTION, so the children go first. The
full-text rows go with their delete triggers. The vectors are removed from embeddings.npz,
because build_index only ever adds them and a forgotten document would otherwise keep
answering searches.
"""

import hashlib
import importlib.util
import sqlite3
import sys
from collections.abc import Generator
from pathlib import Path

import numpy as np
import pytest

from src.config import DOCUMENT_TREES
from src.store.schema import create_database

_SPEC = importlib.util.spec_from_file_location(
    "forget_duplicate_documents",
    Path(__file__).resolve().parent.parent / "scripts" / "forget_duplicate_documents.py",
)
assert _SPEC and _SPEC.loader
_SCRIPT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_SCRIPT)


@pytest.fixture
def index(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "embeddings.npz"
    monkeypatch.setattr("src.store.embeddings.EMBEDDINGS_FILE", path)
    return path


@pytest.fixture
def db(tmp_path) -> Generator[sqlite3.Connection, None, None]:
    conn = create_database(str(tmp_path / "brain.db"))
    yield conn
    conn.close()


def _document(
    db, message_id: int, subject: str, text: str = "quarterly figures"
) -> tuple[int, int]:
    """An email anchor with one attachment, content and every child row.

    Returns (email id, attachment_content id).
    """
    email_id = db.execute(
        "INSERT INTO emails (message_id, date_received, subject, mailbox_name)"
        " VALUES (?, '2026-09-30', ?, 'External')",
        (message_id, subject),
    ).lastrowid
    att = db.execute(
        "INSERT INTO attachments (email_id, message_id, filename, file_path, exported_at)"
        " VALUES (?, ?, 'f.pdf', ?, 'now')",
        (email_id, message_id, f"/att/{abs(message_id)}/f.pdf"),
    ).lastrowid
    content = db.execute(
        "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_status,"
        " llm_status) VALUES (?, ?, 'extracted', 'extracted')",
        (att, text),
    ).lastrowid
    topic = db.execute(
        "INSERT INTO topics (name, display_name) VALUES (?, ?)", (f"t{message_id}", "T")
    ).lastrowid
    person = db.execute("INSERT INTO people (name) VALUES (?)", (f"p{message_id}",)).lastrowid
    db.execute("INSERT INTO email_topics (email_id, topic_id) VALUES (?, ?)", (email_id, topic))
    db.execute("INSERT INTO email_people (email_id, person_id) VALUES (?, ?)", (email_id, person))
    db.execute("INSERT INTO key_facts (email_id, fact) VALUES (?, 'a fact')", (email_id,))
    db.execute("INSERT INTO decisions (email_id, decision) VALUES (?, 'a decision')", (email_id,))
    db.execute("INSERT INTO action_items (email_id, task) VALUES (?, 'a task')", (email_id,))
    db.execute(
        "INSERT INTO commitments (email_id, commitment) VALUES (?, 'a promise')", (email_id,)
    )
    db.execute("INSERT INTO email_html (email_id, html) VALUES (?, ?)", (email_id, b"<p>x</p>"))
    db.commit()
    return email_id, content


def _mail_attachment(db, sha: str, status: str = "extracted", message_id: str = "AAMk") -> None:
    """A mail attachment with these bytes, whose Phase 1 ended in `status`."""
    mail = db.execute(
        "INSERT INTO emails (message_id, date_received, mailbox_name) VALUES (?, 'now', 'Inbox')",
        (message_id,),
    ).lastrowid
    att = db.execute(
        "INSERT INTO attachments (email_id, message_id, filename, file_path, exported_at, sha256)"
        " VALUES (?, ?, 'deck.pptx', '/x', 'now', ?)",
        (mail, message_id, sha),
    ).lastrowid
    db.execute(
        "INSERT INTO attachment_content (attachment_id, extraction_status, llm_status)"
        " VALUES (?, ?, ?)",
        (att, status, "extracted" if status == "extracted" else "pending"),
    )
    db.commit()


def _count(db, table: str, where: str = "1", args=()) -> int:
    return db.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]


def test_forgets_every_row_a_document_left(db, index):
    from src.store.forget import forget_documents

    gone_email, _ = _document(db, -111, "[Document] a")
    kept_email, _ = _document(db, -222, "[Document] b")

    stats = forget_documents(db, [-111])

    assert stats["emails"] == 1 and stats["attachments"] == 1
    for table in (
        "key_facts",
        "decisions",
        "action_items",
        "email_topics",
        "email_people",
        "commitments",
        "email_html",
    ):
        assert _count(db, table, "email_id = ?", (gone_email,)) == 0, table
        assert _count(db, table, "email_id = ?", (kept_email,)) == 1, table
    assert _count(db, "emails", "message_id = -111") == 0
    assert _count(db, "attachments", "message_id = -111") == 0
    assert _count(db, "emails", "message_id = -222") == 1


def test_its_full_text_rows_go_with_it(db, index):
    from src.store.forget import forget_documents

    _, content = _document(db, -111, "[Document] a")

    forget_documents(db, [-111])

    assert _count(db, "attachment_content_fts", "rowid = ?", (content,)) == 0


def test_its_vectors_leave_the_index(db, index):
    from src.store.embeddings import _atomic_savez
    from src.store.forget import forget_documents

    email_id, content = _document(db, -111, "[Document] a")
    _atomic_savez(index, [email_id, -content, 999], np.eye(3, 4, dtype=np.float32))

    stats = forget_documents(db, [-111])

    data = np.load(index)
    assert [int(x) for x in data["ids"]] == [999]
    assert data["vectors"].shape == (1, 4)
    assert stats["vectors"] == 2


def test_remove_vectors_is_a_no_op_without_an_index(index):
    from src.store.embeddings import remove_vectors

    assert remove_vectors({1, 2}) == 0
    assert not index.exists()


def test_forgets_in_chunks_past_the_variable_limit(db, index, monkeypatch):
    import src.store.forget as forget

    monkeypatch.setattr(forget, "CHUNK", 2)
    for n in range(1, 6):
        _document(db, -n, f"[Document] {n}")

    stats = forget.forget_documents(db, [-1, -2, -3, -4, -5])

    assert stats["emails"] == 5
    assert _count(db, "emails") == 0


def test_finds_documents_identical_to_a_mail_attachment(db):
    tree = DOCUMENT_TREES[0]
    same = hashlib.sha256(b"deck").hexdigest()
    other = hashlib.sha256(b"notes").hexdigest()
    _document(db, -abs(int(same[:15], 16)), f"[Document] {tree}/sample")
    _document(db, -abs(int(other[:15], 16)), f"[Document] {tree}/sample")
    _mail_attachment(db, same)

    found = _SCRIPT.find_duplicate_documents(db)

    assert [m for m, _ in found] == [-abs(int(same[:15], 16))]


def test_the_script_is_a_dry_run_by_default(db, tmp_path, capsys, monkeypatch):
    tree = DOCUMENT_TREES[0]
    same = hashlib.sha256(b"deck").hexdigest()
    _document(db, -abs(int(same[:15], 16)), f"[Document] {tree}/sample")
    _mail_attachment(db, same)
    monkeypatch.setattr(
        sys, "argv", ["forget_duplicate_documents.py", "--db", str(tmp_path / "brain.db")]
    )

    assert _SCRIPT.main() == 0

    out = capsys.readouterr().out
    assert "identical to a mail attachment: 1" in out and "DRY RUN" in out
    assert _count(db, "emails", "mailbox_name = 'External'") == 1


def test_a_twin_without_extracted_text_does_not_count(db):
    """The same bytes can extract differently under another name; keep the copy with the text."""
    tree = DOCUMENT_TREES[0]
    same = hashlib.sha256(b"deck").hexdigest()
    _document(db, -abs(int(same[:15], 16)), f"[Document] {tree}/sample")
    _mail_attachment(db, same, status="skipped")

    assert _SCRIPT.find_duplicate_documents(db) == []
