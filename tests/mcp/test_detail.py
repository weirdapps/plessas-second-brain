"""`detail`: a short answer on request, from recall and the dossier tools.

recall's median answer was 32,500 characters after the response budget, about 8,000
tokens a call, most of it summaries and snippets an agent scanning for "is there
anything, and where" never reads. detail="concise" keeps one short line per row (what
it is, where to read it, who and when) and holds the answer to 10,000 characters;
get_item reads any row whole. person_context also takes detail="card", the short
card sender_brief gave, so that tool can go.
"""

import pytest

from src import mcp_budget, mcp_coverage, mcp_server
from src.mcp_budget import json_chars
from tests import seeded_store
from tests.mcp_listing import listed_tools, registered_tool

ENVELOPE = {"kind", "ref", "date", "who"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    mcp_coverage.clear()
    path = seeded_store.build(tmp_path / "brain.db")
    seeded_store.serve(path, monkeypatch)
    return path


@pytest.mark.parametrize(
    "tool, values",
    [
        ("recall", ["full", "concise"]),
        ("person_context", ["full", "concise", "card"]),
        ("topic_context", ["full", "concise"]),
        ("meeting_prep", ["full", "concise"]),
    ],
)
def test_detail_is_an_enum_defaulting_to_full(tool, values):
    schema = listed_tools()[tool].input_schema["properties"]["detail"]

    assert schema["enum"] == values
    assert schema["default"] == "full"


def test_the_default_answer_is_the_full_one(store):
    assert mcp_server.recall("okapi") == mcp_server.recall("okapi", detail="full")


def test_concise_recall_rows_keep_the_envelope_and_one_short_line(store):
    out = mcp_server.recall("okapi", detail="concise")

    assert out["summary"]["total_hits"]
    rows = [row for kind in mcp_server._RECALL_KINDS for row in out[kind]]
    assert rows
    for row in rows:
        assert row.keys() <= mcp_budget.CONCISE_KEYS | set(mcp_budget.CONCISE_TITLES), row
        assert {"kind", "ref"} <= row.keys(), row
        assert "snippet" not in row, row
        titles = [key for key in mcp_budget.CONCISE_TITLES if key in row]
        # One line says what it is; a conversation, which has no title, by its summary.
        assert len(titles) == 1, row
        assert len(row[titles[0]]) <= mcp_budget.CONCISE_TITLE_CHARS
        assert "<" not in (row.get("who") or ""), row  # the name alone


def test_a_long_title_is_cut_with_a_mark(store):
    row = {"kind": "email", "ref": 1, "subject": "x" * 500, "snippet": "y"}

    (out,) = mcp_budget.concise_rows([row])

    assert out["subject"].endswith("…")
    assert len(out["subject"]) == mcp_budget.CONCISE_TITLE_CHARS


def test_a_count_another_change_adds_is_kept_in_concise():
    """older_editions counts the older editions of a report collapsed into a row."""
    row = {"kind": "attachment", "ref": 3, "filename": "report.xlsx", "older_editions": 4}

    (out,) = mcp_budget.concise_rows([row])

    assert out["older_editions"] == 4


def test_concise_recall_fits_ten_thousand_characters(store, monkeypatch):
    monkeypatch.setattr(mcp_budget, "CONCISE_BUDGET_CHARS", 1_500)

    out = mcp_server.recall("okapi", detail="concise")

    assert json_chars(out) <= 1_500
    assert out["truncated"]
    # The header still leads, as in a full answer.
    assert list(out)[:4] == ["summary", "data_as_of", "stale", "truncated"]


def test_the_budget_is_ten_thousand():
    assert mcp_budget.CONCISE_BUDGET_CHARS == 10_000


def test_concise_recall_is_a_fraction_of_the_full_one(store):
    full = json_chars(mcp_server.recall("okapi", include_context=True))
    short = json_chars(mcp_server.recall("okapi", include_context=True, detail="concise"))

    # The seeded rows are a line each; on a real store concise is about a third.
    assert short < full * 0.6


@pytest.mark.parametrize(
    "call",
    [
        lambda: mcp_server.person_context("alice@example.com", detail="concise"),
        lambda: mcp_server.topic_context("okapi", detail="concise"),
        lambda: mcp_server.meeting_prep("alice@example.com", topic="okapi", detail="concise"),
    ],
)
def test_a_concise_dossier_keeps_its_counts_and_drops_the_long_fields(store, call):
    out = call()

    assert json_chars(out) <= mcp_budget.CONCISE_BUDGET_CHARS

    def rows(value):
        if isinstance(value, dict):
            for inner in value.values():
                yield from rows(inner)
        elif isinstance(value, list):
            for row in value:
                if isinstance(row, dict) and "ref" in row:
                    yield row
                else:
                    yield from rows(row)

    found = list(rows(out))
    assert found
    for row in found:
        assert "summary" not in row and "snippet" not in row, row


def test_a_concise_person_keeps_who_they_are_and_the_totals(store):
    full = mcp_server.person_context("alice@example.com")
    short = mcp_server.person_context("alice@example.com", detail="concise")

    assert short["person"] == full["person"]
    assert short["resolved_by"] == full["resolved_by"]
    for key in ("decisions_total", "open_actions_total", "email_count"):
        assert short[key] == full[key]


def test_the_card_is_what_sender_brief_gave(store):
    card = mcp_server.person_context("alice@example.com", detail="card")
    brief = mcp_server.sender_brief("alice@example.com")

    assert card == brief
    assert card["known"] is True


def test_an_unknown_card_says_why(store):
    card = mcp_server.person_context("nobody@example.com", detail="card")

    assert card["known"] is False
    assert card["reason"]


def test_the_card_goes_out_through_the_output_schema(store):
    card = mcp_server.person_context("alice@example.com", detail="card")

    sent = registered_tool("person_context").fn_metadata.convert_result(card)

    assert sent.structured_content["known"] is True
