"""An email can be found by the words of its own subject.

emails_fts indexed the summary and the body, not the subject, and 14% of emails
could not be found by two words of their own subject. v22 adds the folded
subject as a third column, and keyword search tries it first.
"""

import sqlite3

import pytest

from src.config import CURRENT_SCHEMA_VERSION
from src.store.query import _sanitize_fts5_query, query_by_keyword
from src.store.schema import (
    create_database,
    get_connection,
    get_schema_version,
    migrate_index_email_subjects,
    run_migrations,
)

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

    first: list[str] = []
    conn.set_trace_callback(first.append)
    run_migrations(conn)
    conn.execute("UPDATE schema_version SET version = 21")
    conn.commit()
    again: list[str] = []
    conn.set_trace_callback(again.append)
    run_migrations(conn)
    conn.set_trace_callback(None)

    assert _subject_hits(conn, "zebrafinch") == 1
    # Immediate: a second unit started with the first waits for it, then finds
    # the work done instead of rebuilding the table the first has just built.
    assert "BEGIN IMMEDIATE" in first
    assert [s for s in again if "DROP TABLE" in s or "'rebuild'" in s] == []


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


def test_a_failed_migration_raises_its_own_error(tmp_path):
    """On a full disk SQLite rolls the transaction back itself, and an explicit
    ROLLBACK then raised 'no transaction is active' in place of the cause."""
    path = tmp_path / "b.db"
    create_database(str(path)).close()
    conn = _as_v21(path)
    for n in range(300):
        conn.execute(_EMAIL, (n, " ".join(f"s{n}w{j}" for j in range(60))))
    conn.commit()
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    conn.execute(f"PRAGMA max_page_count = {pages + 5}")

    with pytest.raises(sqlite3.OperationalError, match="full"):
        migrate_index_email_subjects(conn)

    assert not conn.in_transaction
    assert [r[1] for r in conn.execute("PRAGMA table_info(emails_fts)")] == [
        "summary_f",
        "content_f",
    ]


def _mail(
    conn, n, subject, summary="a note about something else", content="nothing here", thread=None
):
    conn.execute(
        "INSERT INTO emails (id, message_id, date_received, subject, summary, content, "
        "conversation_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (n, n, f"2026-09-01T00:{n:02d}:00Z", subject, summary, content, thread),
    )


def test_one_long_thread_takes_one_slot(tmp_path):
    """Every email of a thread carries its subject: 30 replies filled the page
    and left no room for the emails whose summary was about the words."""
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    for n in range(1, 31):
        _mail(conn, n, ("RE: " if n > 1 else "") + "Okapi budget planning", thread="CONV-1")
    for n in range(31, 36):
        _mail(conn, n, "Board", summary="The board approved the okapi budget", thread=f"C{n}")
    conn.commit()

    results = query_by_keyword(conn, "okapi budget", limit=5)

    assert len(results) == 5
    assert sum(r["email_id"] <= 30 for r in results) == 1
    assert sum(r["source"] == "summary" and r["email_id"] > 30 for r in results) == 4


def test_emails_with_no_thread_are_each_their_own(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    _mail(conn, 1, "Okapi budget", thread=None)
    _mail(conn, 2, "Okapi budget", thread="")
    _mail(conn, 3, "Okapi budget", thread=None)
    _mail(conn, 4, "Okapi budget", thread="")
    conn.commit()

    results = query_by_keyword(conn, "okapi budget", limit=5)

    assert sorted(r["email_id"] for r in results) == [1, 2, 3, 4]


def test_summary_matches_come_before_body_matches(tmp_path):
    """The subject hits matched in the summary too and used up the summary
    stage's slots, so summary-only emails lost to body-only ones."""
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    _mail(conn, 1, "Kiwi plan", summary="kiwi", thread="A")
    _mail(conn, 2, "Kiwi plan two", summary="kiwi", thread="B")
    _mail(conn, 3, "Other", summary="notes on the kiwi crop this year", thread="C")
    _mail(conn, 4, "Another", summary="notes on the kiwi harvest this year", thread="D")
    _mail(conn, 5, "Third", content="kiwi", thread="E")
    _mail(conn, 6, "Fourth", content="kiwi", thread="F")
    conn.commit()

    results = query_by_keyword(conn, "kiwi", limit=4)

    assert sorted(r["email_id"] for r in results) == [1, 2, 3, 4]
    assert "content" not in {r["source"] for r in results}


def test_a_later_source_fills_the_page_past_rows_already_found(tmp_path):
    """Asked only for the slots left, the body search spent them on emails the
    summary search had already returned, and the page came back short."""
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    _mail(conn, 1, "Alpha", summary="kiwi", thread="A")
    _mail(conn, 2, "Beta", summary="kiwi notes", content="kiwi", thread="B")
    _mail(conn, 3, "Gamma", content="the kiwi was mentioned in passing here", thread="C")
    conn.commit()

    results = query_by_keyword(conn, "kiwi", limit=3)

    assert sorted(r["email_id"] for r in results) == [1, 2, 3]


def _found_by(conn, n, source, text):
    if source == "key_fact":
        conn.execute("INSERT INTO key_facts (email_id, fact) VALUES (?, ?)", (n, text))
        return
    conn.execute(
        "INSERT INTO attachments (id, email_id, message_id, filename, file_path, exported_at) "
        "VALUES (?, ?, ?, 'a.pdf', '/tmp/a.pdf', '2026-09-01')",
        (n, n, n),
    )
    conn.execute(
        "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_status) "
        "VALUES (?, ?, 'done')",
        (n, text),
    )


@pytest.mark.parametrize("source", ["key_fact", "attachment"])
def test_the_last_sources_fill_the_page_past_rows_already_found(tmp_path, source):
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    _mail(conn, 1, "Alpha", summary="kiwi", thread="A")
    _mail(conn, 2, "Beta", summary="kiwi notes", thread="B")
    _found_by(conn, 2, source, "kiwi")
    _mail(conn, 3, "Gamma", thread="C")
    _found_by(conn, 3, source, "the kiwi was mentioned in passing here")
    conn.commit()

    results = query_by_keyword(conn, "kiwi", limit=3)

    assert sorted(r["email_id"] for r in results) == [1, 2, 3]
    assert results[-1]["source"] == source
