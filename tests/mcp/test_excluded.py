"""A search says how many of its matches the default class filter left out.

News and the owner's automation mail are left out of every read unless asked for, and
nothing said a match had been: one question's answer was the owner's own summary mail,
classed automation, invisible in every mode, and the agent never learned it was there.
Every search that leaves a class out now says, under `excluded`, how many matches of
each class it left out and the switch that brings them in.
"""

import pytest

from src import mcp_coverage, mcp_server
from src.store.schema import get_connection
from tests import seeded_store

NEWS = {"rows": 1, "switch": "include_news=true"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    """The seeded store, whose one news item is about okapi, plus an automation report
    about okapi with an attachment, a decision and an action of its own."""
    mcp_coverage.clear()
    path = seeded_store.build(tmp_path / "brain.db")
    conn = get_connection(str(path))
    conn.executescript(
        """
        INSERT INTO emails (id, message_id, date_received, subject, summary, content,
                            sender_name, sender_address, conversation_id, mailbox_name,
                            source_class)
            VALUES (5, 5, strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 day'),
                    'Okapi quarterly report', 'The okapi quarterly numbers', 'okapi body',
                    'Reports', 'reports@example.com', 'auto1', 'Inbox', 'automation');
        INSERT INTO attachments (id, email_id, message_id, filename, file_path, exported_at,
                                 mime_type, file_size)
            VALUES (5, 5, 5, 'okapi-quarterly.pdf', '/x', '2026-10-01T09:00:00Z',
                    'application/pdf', 1);
        INSERT INTO attachment_content (attachment_id, extracted_text, extraction_status,
                                        summary, llm_status)
            VALUES (5, 'the okapi quarterly figures', 'done', 'Okapi quarterly', 'extracted');
        INSERT INTO decisions (email_id, decision, decided_by, decision_date)
            VALUES (5, 'Report the okapi quarter', 'Reports', date('now', '-1 day'));
        INSERT INTO action_items (email_id, task, owner, deadline, status)
            VALUES (5, 'File the okapi quarter', 'Reports', date('now', '+2 days'), 'open');
        """
    )
    conn.commit()
    conn.close()
    seeded_store.serve(path, monkeypatch)
    return path


def _automation(n: int) -> dict:
    return {"rows": n, "switch": "include_automation=true"}


@pytest.mark.parametrize(
    "call, expected",
    [
        (lambda: mcp_server.search_emails("okapi"), {"news": NEWS, "automation": _automation(1)}),
        (
            lambda: mcp_server.query_emails(keyword="okapi"),
            {"news": NEWS, "automation": _automation(1)},
        ),
        (lambda: mcp_server.search_attachments("okapi"), {"automation": _automation(1)}),
        (lambda: mcp_server.query_decisions(), {"automation": _automation(1)}),
        (lambda: mcp_server.query_actions(), {"automation": _automation(1)}),
        (lambda: mcp_server.recall("okapi"), {"news": NEWS, "automation": _automation(2)}),
    ],
)
def test_a_search_counts_what_the_class_filter_left_out(store, call, expected):
    assert call()["excluded"] == expected


def test_a_switch_turned_on_is_no_longer_counted(store):
    out = mcp_server.search_emails("okapi", include_news=True)

    assert out["excluded"] == {"automation": _automation(1)}
    assert any(row["source_class"] == "news" for row in out["result"])


def test_a_call_that_lets_every_class_in_has_nothing_to_report(store):
    out = mcp_server.search_emails("okapi", include_news=True, include_automation=True)

    assert "excluded" not in out


def test_kinds_that_no_left_out_class_belongs_to_report_nothing(store):
    assert "excluded" not in mcp_server.search_emails("okapi", kinds=["document"])


def test_a_search_with_nothing_left_out_says_so(store):
    assert mcp_server.search_emails("zzyzx")["excluded"] == {}


def test_a_search_that_only_matched_what_is_left_out_says_so_in_its_reason(store):
    """The case that cost an answer: the one match was automation mail."""
    out = mcp_server.search_emails("quarterly")

    assert out["result"] == []
    assert out["excluded"] == {"automation": _automation(1)}
    assert "include_automation=true" in out["reason"]
    assert out["coverage"]


def test_semantic_mode_counts_the_keyword_matches_left_out(store, monkeypatch):
    """A second embedding call per search would double its cost; the keyword match
    stands in."""
    import src.store.embeddings as embeddings

    monkeypatch.setattr(embeddings, "query_semantic", lambda conn, query, **kw: [])

    out = mcp_server.search_emails("okapi", search_type="semantic")

    assert out["excluded"] == {"news": NEWS, "automation": _automation(1)}


def test_a_count_that_reached_the_probes_limit_says_it_is_a_floor():
    rows = [{"source_class": "automation"}] * mcp_server._EXCLUDED_PROBE

    out = mcp_server._excluded(("news", "automation"), rows)

    assert out == {
        "automation": {
            "rows": mcp_server._EXCLUDED_PROBE,
            "switch": "include_automation=true",
            "at_least": True,
        }
    }
