"""Key facts marked boilerplate (schema v35: text recurring in five or more threads) stay
out of keyword search's fact stage and query_emails' key-fact filter, as they stay out of
the dossiers (src/store/item_cleanup.py not_boilerplate_sql)."""

import pytest

from src.store.query import query_by_keyword, query_combined
from src.store.schema import create_database
from tests import factories


def _store(tmp_path, boilerplate: int):
    conn = create_database(str(tmp_path / "brain.db"))
    email_id = factories.email(conn, subject="Weekly", summary="Notes.", content="Body.")
    conn.execute(
        "INSERT INTO key_facts (email_id, fact, boilerplate)"
        " VALUES (?, 'okapi encryption notice', ?)",
        (email_id, boilerplate),
    )
    conn.commit()
    return conn, email_id


@pytest.mark.parametrize("search", ["keyword", "combined"])
def test_a_boilerplate_fact_finds_nothing(tmp_path, search):
    conn, _email = _store(tmp_path, boilerplate=1)

    if search == "keyword":
        assert query_by_keyword(conn, "okapi encryption", limit=5) == []
    else:
        assert query_combined(conn, keyword="okapi encryption") == []


def test_any_other_fact_is_found(tmp_path):
    conn, email_id = _store(tmp_path, boilerplate=0)

    rows = query_by_keyword(conn, "okapi encryption", limit=5)

    assert [r["email_id"] for r in rows] == [email_id]
    assert rows[0]["matched_in"] == "key_fact"
    assert [r["email_id"] for r in query_combined(conn, keyword="okapi encryption")] == [email_id]
