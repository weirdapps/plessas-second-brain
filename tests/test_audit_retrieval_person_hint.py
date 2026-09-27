"""recall attaches a person dossier only when the query starts a word of a name.

The pre-check was a substring test, so a topic query found a person whose name
merely contained it: 'AI' gave a Michail, 'EU' gave 'Piraeus Bank GR', and the
dossier was flagged has_person_context. The names below are synthetic.
"""

import pytest

from src.store.recall import recall
from src.store.schema import create_database


@pytest.fixture
def conn():
    c = create_database(":memory:")
    c.executemany(
        "INSERT INTO people (id, name, email) VALUES (?, ?, ?)",
        [
            (1, "Μιχαήλ (Michail)", "michail@example.com"),
            (2, "Piraeus Bank GR", "bank@example.com"),
            (3, "Iris Okapidou", "iris@example.com"),
            (4, "PRESS OFFICE", "press@example.com"),
        ],
    )
    c.execute(
        "INSERT INTO emails (id, message_id, date_received, subject, summary) "
        "VALUES (1, 1, strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 day'), 'Hello', 'A note')"
    )
    c.executemany(
        "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (1, ?, 'sender')",
        [(1,), (2,), (3,), (4,)],
    )
    c.commit()
    return c


@pytest.mark.parametrize("query", ["AI", "EU", "ess", "kapidou"])
def test_a_query_inside_a_word_of_a_name_attaches_no_one(conn, query):
    result = recall(conn, query)

    assert result["person_context"] is None
    assert result["summary"]["has_person_context"] is False


@pytest.mark.parametrize("query", ["Iris", "Okapidou", "okapid", "iris okapidou", "Michail"])
def test_a_query_at_the_start_of_a_word_still_attaches_the_person(conn, query):
    result = recall(conn, query)

    assert result["person_context"] is not None


def test_a_word_start_decoy_does_not_vouch_for_a_more_emailed_in_word_match():
    """The pre-check finds 'AI Compliance', but resolution picks the busier Michail.

    resolve_person matches anywhere in a name and prefers the most-emailed match,
    so recall must attach only a person its own pre-check would accept.
    """
    c = create_database(":memory:")
    c.executemany(
        "INSERT INTO people (id, name, email) VALUES (?, ?, ?)",
        [
            (1, "Μιχαήλ (Michail)", "michail@example.com"),
            (2, "AI Compliance", "ai-compliance@example.com"),
        ],
    )
    c.executemany(
        "INSERT INTO emails (id, message_id, date_received, subject, summary) "
        "VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 day'), 'Hello', 'A note')",
        [(i, i) for i in range(1, 5)],
    )
    c.executemany(
        "INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (?, 1, 'sender')",
        [(i,) for i in range(1, 5)],
    )
    c.execute("INSERT INTO email_people (email_id, person_id, role_in_email) VALUES (1, 2, 'cc')")
    c.commit()

    result = recall(c, "AI")

    assert result["person_context"] is None
    assert result["summary"]["has_person_context"] is False
