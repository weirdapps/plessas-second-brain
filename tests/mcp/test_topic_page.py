"""topic_page: where a live topic stands, from the page held, with the findings filed about it.

A status question was the costliest the owner asked: a median of 18 brain calls. A page
answers it in one, and topic_context leads with it when one is held. A page and a finding
are both derived, and say so.
"""

import pytest

from src import config, mcp_server
from src.mcp_budget import RESPONSE_BUDGET_CHARS, json_chars
from src.store import query
from src.store.schema import get_connection
from tests import seeded_store


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = seeded_store.build(tmp_path / "brain.db")
    seeded_store.serve(path, monkeypatch)
    monkeypatch.setattr(config, "USER_EMAIL_PATTERN", "")
    monkeypatch.setattr(query, "USER_EMAIL_PATTERN", "")
    return path


def test_a_held_page_comes_back_with_its_citations_and_findings(store):
    out = mcp_server.topic_page("Okapi")

    assert list(out)[:2] == ["data_as_of", "stale"]
    page = out["topic_page"]
    assert page["topic"] == "okapi" and "[email:1]" in page["body"]
    assert page["citations"][0]["ref"] == "email:1"
    (finding,) = out["findings"]
    assert finding["ref"].startswith("finding:") and finding["sourced"] is True
    assert "reason" not in out


def test_a_topic_without_a_page_says_why_and_lists_the_pages_held(store):
    out = mcp_server.topic_page("zebra crossing")

    assert out["topic_page"] is None and out["findings"] == []
    assert "No page for this topic" in out["reason"]
    assert [p["topic"] for p in out["pages"]] == ["okapi"]


def test_topic_context_leads_with_the_page(store):
    out = mcp_server.topic_context("okapi")

    keys = [k for k in out if k not in ("data_as_of", "stale", "_stale_warning")]
    assert keys[0] == "topic_page"
    assert out["topic_page"]["topic"] == "okapi"
    assert out["decisions"]  # the items stay the evidence, after the page


def test_topic_context_has_no_page_for_a_topic_without_one(store):
    conn = get_connection(str(store))
    conn.execute("DELETE FROM topic_pages")
    conn.commit()
    conn.close()

    assert "topic_page" not in mcp_server.topic_context("okapi")


def test_a_long_page_is_held_to_the_budget(store):
    conn = get_connection(str(store))
    for i in range(60):
        conn.execute(
            "INSERT INTO findings (note_path, ordinal, claim, value, source_ref, sourced,"
            " source_check, loaded_at, written_at)"
            " VALUES ('/notes/long.md', ?, ?, '1', 'email:1', 1, 'ok', '2026-10-01', '2026-10-01')",
            (i, "okapi " + "word " * 300),
        )
    conn.commit()
    conn.close()

    out = mcp_server.topic_page("okapi")

    assert json_chars(out) <= RESPONSE_BUDGET_CHARS
