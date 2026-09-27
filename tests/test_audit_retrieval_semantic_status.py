"""recall says when its semantic half failed.

Any error from the semantic candidates (an expired ADC, a truncated index)
dropped the emails bucket to keyword-only with no log line and no flag, so a
keyword-only answer read as a fused one.
"""

import logging

import pytest

from src.store.recall import recall
from src.store.schema import create_database


@pytest.fixture
def conn():
    c = create_database(":memory:")
    c.execute(
        "INSERT INTO emails (id, message_id, date_received, subject, summary) "
        "VALUES (1, 1, '2026-09-01T10:00:00Z', 'Okapi budget', 'The okapi budget')"
    )
    c.commit()
    return c


def test_a_failing_semantic_half_is_logged_and_reported(conn, caplog):
    def boom(conn, query, limit):
        raise RuntimeError("ADC expired")

    with caplog.at_level(logging.WARNING, logger="src.store.recall"):
        result = recall(conn, "okapi", semantic_candidates=boom)

    assert [r["email_id"] for r in result["emails"]] == [1]  # keyword rows still come back
    assert result["summary"]["semantic"] == "unavailable: RuntimeError"
    assert "ADC expired" in caplog.text


def test_a_working_semantic_half_says_ok(conn):
    result = recall(conn, "okapi", semantic_candidates=lambda c, q, n: [1])

    assert result["summary"]["semantic"] == "ok"


def test_no_semantic_candidates_found_is_still_ok(conn):
    result = recall(conn, "okapi", semantic_candidates=lambda c, q, n: [])

    assert result["summary"]["semantic"] == "ok"
