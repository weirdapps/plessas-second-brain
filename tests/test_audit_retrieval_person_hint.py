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


def test_the_person_attached_is_the_one_resolve_person_finds():
    """'AI' starts a word of 'AI Compliance' and sits inside the busier Michail.

    recall attaches the person resolve_person finds, which matches at the start
    of a word, so it is the one person_context gives for the same words, and the
    busier in-word match is never attached.
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

    assert result["person_context"]["person"]["name"] == "AI Compliance"
    assert result["summary"]["has_person_context"] is True
