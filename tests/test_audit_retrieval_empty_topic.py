"""An empty or punctuation-only topic matches no topic.

normalize_topic turns '   ', '-' and '.' into '', and LIKE '%%' matched every
topic, so the most-used one came back as the answer, and recall attached it.
"""

import pytest

from src.store.context import get_topic_context
from src.store.recall import recall
from src.store.schema import create_database


@pytest.fixture
def conn():
    c = create_database(":memory:")
    c.execute(
        "INSERT INTO emails (id, message_id, date_received, subject, summary) "
        "VALUES (1, 1, strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 day'), 'Hi', 'A note')"
    )
    c.execute("INSERT INTO topics (id, name, display_name) VALUES (1, 'okapi banking', 'Okapi')")
    c.execute("INSERT INTO email_topics (email_id, topic_id) VALUES (1, 1)")
    c.commit()
    return c


@pytest.mark.parametrize("topic", ["", "   ", "-", ".", "%", "_/_"])
def test_no_alphanumeric_character_is_no_topic(conn, topic):
    assert get_topic_context(conn, topic)["topic"] is None

    result = recall(conn, topic, include_context=True)
    assert result["topic_context"] is None
    assert result["summary"]["has_topic_context"] is False


def test_a_real_topic_still_resolves(conn):
    assert get_topic_context(conn, "okapi")["topic"]["name"] == "okapi banking"
    assert recall(conn, "okapi", include_context=True)["topic_context"] is not None
