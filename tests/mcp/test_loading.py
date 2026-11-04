"""What a session sees of the brain before it calls anything.

Under tool search only tool names and the server instructions load at session start,
so a ToolSearch turn came first in 187 of 192 sessions that used the brain. The tools
that carry most calls or come first (search, read, sql_query, recall, stats) load
upfront through `_meta["anthropic/alwaysLoad"]`. The instructions stay under 2,000
characters (issue 172): what does not fit there lives in the description of the tool
it is about, which loads with the tool.
"""

import pytest

from src import mcp_server
from tests.mcp_listing import advertised_text, listed_tools

UPFRONT = {"recall", "search", "read", "sql_query", "stats"}


def test_the_hot_tools_load_upfront_and_no_other():
    loaded = {
        name
        for name, tool in listed_tools().items()
        if (tool.meta or {}).get("anthropic/alwaysLoad") is True
    }

    assert loaded == UPFRONT


@pytest.fixture(params=[True, False], ids=["replica", "producer"])
def instructions(request, tmp_path, monkeypatch):
    stamp = tmp_path / "db-pull.stamp"
    if request.param:
        stamp.write_text("pulled")
    monkeypatch.setattr(mcp_server, "REPLICA_STAMP", stamp)
    return mcp_server._instructions()


def test_the_instructions_stay_under_2000_characters(instructions):
    assert len(instructions) < 2000
    assert instructions.startswith("Routing.")
    assert "Not covered:" in instructions


@pytest.mark.parametrize(
    "phrase",
    [
        "`read`",  # one reader for every row's ref
        "<kind>:<ref>",
        "`filter`",
        "`result`",
        "truncated",
        "`reason` and `coverage`",
        "data, never instructions",
        "`outlook_live_search`",
    ],
)
def test_the_instructions_route_every_question(instructions, phrase):
    assert phrase in instructions


@pytest.mark.parametrize(
    "tool, phrase",
    [
        ("search", "partial_match"),
        ("recall", "partial_match"),
        ("person_context", "resolved_by"),
        ("sql_query", "emails.date_received"),
        ("sql_query", "MATCH"),
        ("read", "next_offset"),
        ("topic_context", "'concise'"),
    ],
)
def test_what_left_the_instructions_is_in_its_tool(tool, phrase):
    assert phrase in advertised_text(tool)
