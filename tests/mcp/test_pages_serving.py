"""The compiled pages (issue 169) through the existing tools and as MCP resources.

person_context, topic_context and meeting_prep lead with the entity's page when one is held,
recall shows the card of an entity the query names, and brain://pages/{kind}/{key} reads a
page whole. No tool is renamed and no answer changes on a store without the page layer.
"""

import pytest

from src import config, mcp_server
from src.store import pages, query
from src.store.schema import create_database
from tests import factories as f
from tests import seeded_store

OWNER = ("Owner Example", "owner@example.com")


def _seed(conn):
    for i in range(5):
        f.email(
            conn,
            subject="Budget 2027" if i == 0 else "Re: Budget 2027",
            sender=f.ALICE if i % 2 == 0 else OWNER,
            to=(OWNER,) if i % 2 == 0 else (f.ALICE,),
            topics=["okapi budget"],
            conversation_id="cid-budget",
            date_received=f.ago(days=3, hours=-i),
            source_class="mail",
        )
    for d in (21, 14, 7, -7):
        f.calendar_event(
            conn,
            subject="Weekly cards steering",
            start_at=f.ago(days=d),
            recurrence_master_id="master-1",
            is_recurring=1,
            attendees=(f.ALICE, OWNER),
        )


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "USER_EMAIL_PATTERN", OWNER[1])
    monkeypatch.setattr(query, "USER_EMAIL_PATTERN", OWNER[1])
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    _seed(conn)
    pages.refresh(conn)
    conn.commit()
    conn.close()
    seeded_store.serve(path, monkeypatch)
    return path


@pytest.fixture
def bare(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "USER_EMAIL_PATTERN", OWNER[1])
    monkeypatch.setattr(query, "USER_EMAIL_PATTERN", OWNER[1])
    path = tmp_path / "old.db"
    conn = create_database(str(path), gated=False)
    _seed(conn)
    conn.close()
    seeded_store.serve(path, monkeypatch)
    return path


def test_person_context_leads_with_the_page(store):
    out = mcp_server.person_context(f.ALICE[1])
    page = out["page"]
    assert page["kind"] == "person" and page["derived"] is True
    assert page["uri"].startswith("brain://pages/person/")
    assert all("[email:" in line or "[calendar:" in line for line in page["lines"])


def test_topic_context_leads_with_the_topic_page(store):
    out = mcp_server.topic_context("okapi budget")
    assert out["page"]["kind"] == "topic"


def test_meeting_prep_gives_each_attendee_page_and_the_series_page(store):
    out = mcp_server.meeting_prep(f.ALICE[1])
    assert out["attendees"][0]["page"]["kind"] == "person"
    assert [p["key"] for p in out["series_pages"]] == ["master-1"]


def test_recall_shows_the_card_of_an_entity_the_query_names(store):
    out = mcp_server.recall("Alice Smith")
    assert out["pages"][0]["kind"] == "person"


def test_a_page_reads_whole_as_a_resource(store):
    # The functions the two resources call (brain://pages, brain://pages/{kind}/{key}).
    assert "master-1" in mcp_server.pages_index()
    body = mcp_server.page_resource("series", "master-1")
    assert body.startswith("# Weekly cards steering (series page)")
    assert "[calendar:" in body
    assert mcp_server.page_resource("series", "nope").startswith("No series page nope")


def test_without_the_page_layer_no_answer_changes(bare):
    assert "page" not in mcp_server.person_context(f.ALICE[1])
    assert "page" not in mcp_server.topic_context("okapi budget")
    prep = mcp_server.meeting_prep(f.ALICE[1])
    assert "series_pages" not in prep and "page" not in prep["attendees"][0]
    assert "pages" not in mcp_server.recall("Alice Smith")
