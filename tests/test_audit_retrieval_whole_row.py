"""An email holding every word of a query across its fields is a whole match.

Each stage of the keyword waterfall matched one column, so a query naming a
person and a subject ('Okapides apple pay') missed the email with the name in
its summary and the subject words in its body, and fell back to partial rows
holding one word each. The names below are synthetic.
"""

import sqlite3

from src.store.query import query_by_keyword
from src.store.schema import create_database


def _mail(conn, n, subject, summary="a note", content="nothing here", thread=None):
    conn.execute(
        "INSERT INTO emails (id, message_id, date_received, subject, summary, content, "
        "conversation_id, mailbox_name) VALUES (?, ?, ?, ?, ?, ?, ?, 'Inbox')",
        (n, n, f"2026-09-01T00:{n:02d}:00Z", subject, summary, content, thread),
    )


def _store(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    conn.row_factory = sqlite3.Row
    return conn


def test_the_name_in_the_summary_and_the_subject_in_the_body_is_a_whole_match(tmp_path):
    conn = _store(tmp_path)
    _mail(conn, 1, "Weekly", summary="Okapides asked for an update", content="the apple pay launch")
    _mail(conn, 2, "Apple news", summary="unrelated", thread="N2")  # one word only
    _mail(conn, 3, "Evaluation Okapides", thread="N3")  # one word only
    conn.commit()

    results = query_by_keyword(conn, "Okapides apple pay", limit=10)

    assert [r["email_id"] for r in results] == [1]
    assert results[0]["source"] == "mixed"
    assert not results[0].get("partial_match")
    assert "<b>apple</b>" in results[0]["snippet"]


def test_a_single_field_match_still_ranks_ahead_of_a_mixed_one(tmp_path):
    conn = _store(tmp_path)
    _mail(conn, 1, "Weekly", summary="Okapides asked", content="the apple pay launch", thread="A")
    _mail(conn, 2, "Weekly", content="Okapides on the apple pay launch", thread="B")
    conn.commit()

    results = query_by_keyword(conn, "Okapides apple pay", limit=10)

    assert [(r["email_id"], r["source"]) for r in results] == [(2, "content"), (1, "mixed")]


def test_thread_matches_counts_whole_row_matches(tmp_path):
    conn = _store(tmp_path)
    _mail(conn, 1, "Weekly", summary="Okapides asked", content="the apple pay launch", thread="T")
    _mail(conn, 2, "RE: Weekly", summary="Okapides replied", content="apple pay again", thread="T")
    conn.commit()

    results = query_by_keyword(conn, "Okapides apple pay", limit=10)

    assert [(r["email_id"], r["source"]) for r in results] == [(2, "mixed")]
    assert not results[0].get("partial_match")
    assert results[0]["thread_matches"] == 2


def test_thread_matches_counts_a_whole_row_match_beside_a_single_field_one(tmp_path):
    conn = _store(tmp_path)
    _mail(conn, 1, "Weekly", content="Okapides on the apple pay launch", thread="T")
    _mail(conn, 2, "RE: Weekly", summary="Okapides replied", content="apple pay again", thread="T")
    conn.commit()

    results = query_by_keyword(conn, "Okapides apple pay", limit=10)

    assert [(r["email_id"], r["source"]) for r in results] == [(1, "content")]
    assert results[0]["thread_matches"] == 2


def test_a_content_only_search_does_not_match_across_fields(tmp_path):
    conn = _store(tmp_path)
    _mail(conn, 1, "Weekly", summary="Okapides asked", content="the apple pay launch")
    conn.commit()

    results = query_by_keyword(conn, "Okapides apple pay", limit=10, search_content_only=True)

    assert all(r.get("partial_match") for r in results)
