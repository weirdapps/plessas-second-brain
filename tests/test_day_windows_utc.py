"""The `days` windows of the dossiers start N days ago in UTC, the time the store holds.

Mail is stored in UTC with a Z (all of it since schema v41). The person and topic dossiers and
query_decisions cut their window at `datetime.now() - days`, the host's wall time, which in
Athens is two or three hours ahead of UTC: the first hours of the window were left out.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.store.context import get_person_context, get_topic_context
from src.store.normalizer import find_or_create_topic
from src.store.query import query_decisions
from src.store.schema import create_database
from tests import factories
from tests.local_zone import local_zone

DAYS = 30


def _utc(**ago) -> str:
    return (datetime.now(UTC) - timedelta(**ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def conn(tmp_path):
    c = create_database(str(tmp_path / "brain.db"))
    # An hour inside the window, and an hour outside it.
    inside = factories.email(
        c, subject="inside", date_received=_utc(days=DAYS, hours=-1), conversation_id="t1"
    )
    outside = factories.email(
        c, subject="outside", date_received=_utc(days=DAYS, hours=1), conversation_id="t2"
    )
    topic = find_or_create_topic(c, "Card Pricing")
    for email_id in (inside, outside):
        c.execute("INSERT INTO email_topics (email_id, topic_id) VALUES (?, ?)", (email_id, topic))
        c.execute("INSERT INTO decisions (email_id, decision) VALUES (?, ?)", (email_id, "agreed"))
    c.commit()
    yield c
    c.close()


def test_the_person_dossier_window_starts_n_days_ago_in_utc(conn):
    with local_zone("Europe/Athens"):
        ctx = get_person_context(conn, factories.ALICE[1], days=DAYS)
    assert [e["subject"] for e in ctx["recent_emails"]] == ["inside"]


def test_the_topic_dossier_window_starts_n_days_ago_in_utc(conn):
    with local_zone("Europe/Athens"):
        ctx = get_topic_context(conn, "card pricing", days=DAYS)
    assert [e["subject"] for e in ctx["recent_emails"]] == ["inside"]


def test_the_decisions_window_starts_n_days_ago_in_utc(conn):
    with local_zone("Europe/Athens"):
        rows = query_decisions(conn, days=DAYS)
    assert len(rows) == 1
