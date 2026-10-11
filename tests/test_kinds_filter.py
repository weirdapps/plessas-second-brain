"""The search tools take `kinds`: only rows of those kinds come back.

search_emails is named for email and returned news articles and standalone
documents under the same name (a keyword query on the replica: 14 news rows of 20),
and its semantic mode Teams, WhatsApp, conversation and attachment rows besides. A
caller had no way to ask for mail only, or documents only, except by filtering a
page afterwards, which leaves it short. `kinds` is applied in the query, before the
limit. Leaving news out needs no switch of its own: include_news is false unless
set, and naming news in `kinds` lets it in.
"""

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from src import mcp_server
from src.store.envelope import KINDS
from src.store.schema import get_connection
from tests import seeded_store
from tests.mcp_listing import listed_tools, registered_tool

# recall takes every kind a row can say it is.
ALL_KINDS = list(KINDS)


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = seeded_store.build(tmp_path / "brain.db")
    seeded_store.serve(path, monkeypatch)
    return path


def _more_mail(path, n=30):
    """Mail that matches 'okapi' better than the document does, newest first."""
    conn = get_connection(str(path))
    for i in range(100, 100 + n):
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received, subject, summary, "
            "sender_address, conversation_id, mailbox_name, source_class) VALUES "
            "(?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), 'Okapi okapi', 'okapi', "
            "'x@example.com', ?, 'Inbox', 'mail')",
            (i, i, f"m{i}"),
        )
    conn.commit()
    conn.close()


@pytest.mark.parametrize(
    "tool, allowed",
    [
        ("recall", ALL_KINDS),
        ("search_emails", [k for k in ALL_KINDS if k not in ("calendar", "image")]),
        ("query_emails", ["email", "news", "document"]),
    ],
)
def test_kinds_is_an_optional_list_of_named_kinds(tool, allowed):
    schema = listed_tools()[tool].input_schema["properties"]["kinds"]

    branch = next(b for b in schema["anyOf"] if b.get("type") == "array")
    assert branch["items"]["enum"] == allowed
    assert branch["minItems"] == 1
    assert schema["description"]


def test_an_empty_or_unknown_kind_is_refused_before_the_tool_runs():
    for kinds in ([], ["mail"]):
        with pytest.raises(ValidationError):
            registered_tool("search_emails").fn_metadata.validate_arguments(
                {"query": "x", "kinds": kinds}
            )


def test_documents_only_finds_the_document_behind_a_page_of_mail(store):
    """Filtered after the limit, a page of better-matching mail left nothing."""
    _more_mail(store)

    rows = mcp_server.search_emails("okapi", limit=5, kinds=["document"])["result"]

    assert [row["email_id"] for row in rows] == [4]
    assert rows[0]["kind"] == "document"


def test_email_only_leaves_out_documents(store):
    rows = mcp_server.search_emails("okapi", kinds=["email"])["result"]

    assert rows and {row["kind"] for row in rows} == {"email"}


def test_naming_news_lets_it_in(store):
    rows = mcp_server.search_emails("okapi", kinds=["news"])["result"]

    assert [(row["email_id"], row["kind"]) for row in rows] == [(3, "news")]


def test_keyword_search_refuses_a_kind_it_cannot_hold(store):
    with pytest.raises(ToolError, match="search_teams"):
        mcp_server.search_emails("okapi", kinds=["teams"])


def test_query_emails_takes_kinds(store):
    rows = mcp_server.query_emails(keyword="okapi", kinds=["document"])["result"]

    assert [row["email_id"] for row in rows] == [4]


def test_recall_returns_only_the_kinds_asked_for(store):
    out = mcp_server.recall("okapi", kinds=["teams"])

    assert [row["kind"] for row in out["teams"]] == ["teams"]
    # What was decided and given in Teams, and nothing from mail.
    assert [row["decision"] for row in out["decisions"]] == ["Pilot the okapi tracker"]
    assert [row["task"] for row in out["actions"]] == ["Book the okapi room"]
    for bucket in mcp_server._RECALL_KINDS:
        if bucket not in ("teams", "decisions", "actions"):
            assert out[bucket] == [], bucket
    assert out["summary"]["kinds_with_results"] == ["decisions", "actions", "teams"]


def test_recall_documents_only_narrows_the_emails_bucket(store):
    _more_mail(store, n=10)

    out = mcp_server.recall("okapi", limit_per_kind=3, kinds=["document"])

    assert [row["email_id"] for row in out["emails"]] == [4]
    assert out["decisions"] == [] and out["commitments"] == []


@pytest.mark.parametrize(
    "kinds, expected",
    [(["document"], [4]), (["news"], [3]), (["email"], [2]), (["email", "document"], [2, 4])],
)
def test_a_store_before_the_class_narrows_by_mailbox(store, monkeypatch, kinds, expected):
    """A replica reads a v32 file with new code until its next pull, which brings the
    column. There the mailbox alone tells: News is news, External a document."""
    from src.store import source_class

    conn = get_connection(str(store))
    conn.execute("DROP INDEX idx_emails_source_class")
    conn.execute("ALTER TABLE emails DROP COLUMN source_class")
    conn.commit()
    conn.close()
    monkeypatch.setattr(source_class, "_warned", False)

    rows = mcp_server.search_emails("okapi", kinds=kinds)["result"]

    assert sorted(row["email_id"] for row in rows) == expected
    assert {row["kind"] for row in rows} == set(kinds)


def test_recall_without_kinds_is_unchanged(store):
    plain = mcp_server.recall("okapi")
    every = mcp_server.recall("okapi", kinds=[k for k in ALL_KINDS if k != "news"])

    for bucket in mcp_server._RECALL_KINDS:
        assert plain[bucket] == every[bucket], bucket
