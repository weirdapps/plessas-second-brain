"""An email can be found by the words of its own subject.

emails_fts indexed the summary and the body, not the subject, and 14% of emails
could not be found by two words of their own subject. v22 adds the folded
subject as a third column, and keyword search tries it first.
"""

import sqlite3

from src.config import CURRENT_SCHEMA_VERSION
from src.store.query import _sanitize_fts5_query, query_by_keyword
from src.store.schema import create_database, get_connection, get_schema_version, run_migrations

_EMAIL = (
    "INSERT INTO emails (message_id, date_received, subject, summary, content) "
    "VALUES (?, '2026-09-01T00:00:00Z', ?, 'a note about something else', 'nothing here')"
)


def _as_v21(path):
    """A store as v21 left it: emails_fts over the summary and the body only."""
    conn = get_connection(str(path))
    for suffix in ("ai", "ad", "au"):
        conn.execute(f"DROP TRIGGER IF EXISTS emails_{suffix}")
    conn.execute("DROP TABLE emails_fts")
    conn.execute(
        "CREATE VIRTUAL TABLE emails_fts USING fts5(summary_f, content_f, "
        "content='emails', content_rowid='id')"
    )
    conn.execute(
        "CREATE TRIGGER emails_ai AFTER INSERT ON emails BEGIN "
        "INSERT INTO emails_fts(rowid, summary_f, content_f) "
        "VALUES (new.id, new.summary_f, new.content_f); END"
    )
    conn.execute("INSERT INTO emails_fts(emails_fts) VALUES('rebuild')")
    conn.execute("UPDATE schema_version SET version = 21")
    conn.commit()
    return conn


def _subject_hits(conn, words):
    return conn.execute(
        "SELECT count(*) FROM emails_fts WHERE emails_fts.subject_f MATCH ?",
        (_sanitize_fts5_query(words),),
    ).fetchone()[0]


def test_a_fresh_store_indexes_the_subject(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    conn.execute(_EMAIL, (1, "Quarterly zebrafinch review"))
    conn.commit()

    assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION >= 22
    assert _subject_hits(conn, "zebrafinch review") == 1


def test_an_upgraded_store_indexes_the_subjects_it_already_held(tmp_path):
    path = tmp_path / "b.db"
    create_database(str(path)).close()
    conn = _as_v21(path)
    conn.execute(_EMAIL, (1, "Quarterly zebrafinch review"))
    conn.commit()

    run_migrations(conn)

    assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
    assert _subject_hits(conn, "zebrafinch") == 1
    conn.execute(_EMAIL, (2, "Okapi budget"))
    conn.execute("DELETE FROM emails WHERE message_id = 1")
    conn.commit()
    assert _subject_hits(conn, "okapi") == 1
    assert _subject_hits(conn, "zebrafinch") == 0


def test_the_migration_is_a_no_op_the_second_time(tmp_path):
    path = tmp_path / "b.db"
    create_database(str(path)).close()
    conn = _as_v21(path)
    conn.execute(_EMAIL, (1, "Quarterly zebrafinch review"))
    conn.commit()

    run_migrations(conn)
    conn.execute("UPDATE schema_version SET version = 21")
    conn.commit()
    run_migrations(conn)

    assert _subject_hits(conn, "zebrafinch") == 1


def test_the_subject_is_folded_like_the_rest(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    conn.execute(_EMAIL, (1, "Παρουσίαση πελατών"))
    conn.commit()

    for words in ("παρουσιαση πελατων", "ΠΑΡΟΥΣΙΑΣΗ", "Παρουσίαση"):
        assert _subject_hits(conn, words) == 1, words


def test_keyword_search_still_works_on_a_store_before_v22(tmp_path):
    """A replica can run this code against a database the producer has not
    migrated yet."""
    path = tmp_path / "b.db"
    create_database(str(path)).close()
    conn = _as_v21(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject, summary, content) "
        "VALUES (1, '2026-09-01T00:00:00Z', 'x', 'zebrafinch review', 'y')"
    )
    conn.commit()

    results = query_by_keyword(conn, "zebrafinch", limit=5)

    assert [r["source"] for r in results] == ["summary"]


def test_keyword_search_finds_an_email_by_its_subject_alone(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    conn.execute(_EMAIL, (1, "Quarterly zebrafinch review"))
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject, summary, content) "
        "VALUES (2, '2026-09-02T00:00:00Z', 'other', 'zebrafinch mentioned', 'x')"
    )
    conn.commit()

    results = query_by_keyword(conn, "zebrafinch review", limit=5)

    assert results[0]["email_id"] == 1
    assert results[0]["source"] == "subject"
