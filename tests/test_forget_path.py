"""One forget path (follow-up to issue 174): forget_documents and forget_emails go through
src/store/retention.py, so every caller clears what the old path left behind (email aliases,
thread-id map rows, item labels, inline images no row names any more) and keeps its own
behaviour otherwise: no file is deleted and no full-text index is merged for them."""

import hashlib

import pytest

from src.store.schema import create_database
from tests.test_audit_news_dedup import _stored
from tests.test_session_notes import LONG, _session
from tests.test_sharepoint_shells import HOST, PAGE, _document, _link, _shell


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("src.store.embeddings.EMBEDDINGS_FILE", tmp_path / "embeddings.npz")
    c = create_database(str(tmp_path / "brain.db"))
    yield c
    c.close()


def _seed(conn, email_id: int) -> str:
    """The rows the old path left: an alias, a thread-id map row, a label on one of the
    email's decisions, an inline image only this email shows. Returns the image hash."""
    message_id = conn.execute("SELECT message_id FROM emails WHERE id = ?", (email_id,)).fetchone()
    sha = hashlib.sha256(str(email_id).encode()).hexdigest()
    conn.execute(
        "INSERT INTO email_aliases (message_id, email_id, recorded_at) VALUES (?, ?, 'now')",
        (f"alias-{email_id}", email_id),
    )
    conn.execute(
        "INSERT INTO thread_id_map (email_id, old_id, new_id, rule, mapped_at)"
        " VALUES (?, 'old', 'new', 'reference', 'now')",
        (email_id,),
    )
    decision = conn.execute(
        "INSERT INTO decisions (email_id, decision) VALUES (?, 'go')", (email_id,)
    ).lastrowid
    conn.execute(
        "INSERT INTO item_labels (item_table, item_id, label, method, thread_key, run_id,"
        " created_at) VALUES ('decisions', ?, 'completed_by', 'rule', 't', 'r', 'now')",
        (decision,),
    )
    conn.execute(
        "INSERT INTO inline_images (sha256, width, height, bytes, classification,"
        " classification_method, classified_at) VALUES (?, 1, 1, 1, 'content', 'rule', 'now')",
        (sha,),
    )
    conn.execute(
        "INSERT INTO inline_image_occurrences (sha256, message_id, sender_email,"
        " position_in_body) VALUES (?, ?, 'a@x', 0)",
        (sha, str(message_id[0])),
    )
    conn.commit()
    return sha


def _left(conn, email_id: int, sha: str) -> dict:
    one = lambda sql, arg: conn.execute(sql, (arg,)).fetchone()[0]  # noqa: E731
    return {
        "email_aliases": one("SELECT COUNT(*) FROM email_aliases WHERE email_id = ?", email_id),
        "thread_id_map": one("SELECT COUNT(*) FROM thread_id_map WHERE email_id = ?", email_id),
        "item_labels": one("SELECT COUNT(*) FROM item_labels WHERE item_table = ?", "decisions"),
        "inline_image_occurrences": one(
            "SELECT COUNT(*) FROM inline_image_occurrences WHERE sha256 = ?", sha
        ),
        "inline_images": one("SELECT COUNT(*) FROM inline_images WHERE sha256 = ?", sha),
    }


CLEARED = dict.fromkeys(
    ("email_aliases", "thread_id_map", "item_labels", "inline_image_occurrences", "inline_images"),
    0,
)


def _row_of(conn, message_id) -> int:
    return conn.execute("SELECT id FROM emails WHERE message_id = ?", (message_id,)).fetchone()[0]


def test_sharepoint_shells_retire_clears_what_the_old_path_left(conn):
    from src.export.sharepoint_shells import classify, retire

    page = _document(
        conn,
        "/sites/team/sitepages/card-launch.aspx",
        "[SharePoint page] Launch",
        text="The card launches in June.",
        source="sharepoint-page",
    )
    shell = _shell(conn, "aa1", f"{HOST}/sites/Team/SitePages/Card-launch.aspx")
    _link(conn, PAGE, "ok", page)
    row = _row_of(conn, shell)
    sha = _seed(conn, row)

    stats = retire(conn, classify(conn))

    assert stats["forgotten"] == 1
    assert _left(conn, row, sha) == CLEARED


def test_news_dedup_clears_what_the_old_path_left(conn):
    from src.export.news_export import dedupe_stored

    _stored(conn, 1, 10)
    _stored(conn, 2, 10, body="Acme Bank opens a new branch in the harbour district")
    copy = conn.execute("SELECT MIN(id) FROM emails").fetchone()[0]
    sha = _seed(conn, copy)

    done = dedupe_stored(conn)

    assert done["forgotten"] == 1
    assert _left(conn, copy, sha) == CLEARED


def test_a_replaced_session_note_clears_what_the_old_path_left(conn, tmp_path):
    from src.export.session_notes import ingest_session_notes

    ingest_session_notes(conn, [_session(tmp_path, "s1", LONG)])
    old = conn.execute("SELECT id FROM emails").fetchone()[0]
    sha = _seed(conn, old)

    stats = ingest_session_notes(
        conn, [_session(tmp_path, "s2", LONG + " Revised.", ts="2026-09-30T11:00:00Z")]
    )

    assert stats["replaced"] == 1
    assert _left(conn, old, sha) == CLEARED


def test_the_old_callers_delete_no_file(conn, tmp_path, monkeypatch):
    from src.store.forget import forget_documents

    data = tmp_path / "data"
    folder = data / "attachments" / "77"
    folder.mkdir(parents=True)
    kept = folder / "f.pdf"
    kept.write_bytes(b"x")
    monkeypatch.setattr("src.config.DATA_ROOT", data)
    monkeypatch.setattr("src.config.ATTACHMENTS_DIR", data / "attachments")
    eid = conn.execute(
        "INSERT INTO emails (message_id, date_received, subject) VALUES (-77, '2026-01-01', 'd')"
    ).lastrowid
    conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, file_path, exported_at)"
        " VALUES (?, -77, 'f.pdf', '/p/attachments/77/f.pdf', 'now')",
        (eid,),
    )
    conn.commit()

    stats = forget_documents(conn, [-77])

    assert stats == {"emails": 1, "attachments": 1, "vectors": 0}
    assert kept.exists()
