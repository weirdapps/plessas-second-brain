"""What a session sees of the brain before it calls anything.

Under tool search only tool names and the server instructions load at session start,
so a ToolSearch turn came first in 187 of 192 sessions that used the brain. Five tools
carry most calls and are the usual first ones (sql_query, search_emails,
search_attachments and recall are 76% of calls; stats is the most common first call),
so they load upfront through `_meta["anthropic/alwaysLoad"]`. Claude Code keeps 4,096
characters of the instructions since v2.1.296, not 2,048, so the routing that was cut
to fit comes back, under 4,000.
"""

import pytest

from src import mcp_server
from tests.mcp_listing import listed_tools

UPFRONT = {"sql_query", "search_emails", "search_attachments", "recall", "stats"}


def test_the_five_hot_tools_load_upfront_and_no_other():
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


def test_the_instructions_fit_what_claude_code_keeps(instructions):
    assert len(instructions) <= 4000
    assert instructions.startswith("Routing.")
    # The last paragraph is the first to go if they ever run long again.
    assert "Not covered:" in instructions


@pytest.mark.parametrize(
    "phrase",
    [
        "get_item",  # one reader for every row's ref
        'detail="concise"',
        "truncated",
        "`reason` and `coverage`",
        "partial_match",
        "resolved_by",
        "emails.date_received",
        "MATCH",
        "Deprecated",
        "not an empty answer",
    ],
)
def test_the_instructions_route_what_was_cut_to_fit_2048(instructions, phrase):
    assert phrase in instructions


def test_the_comment_no_longer_assumes_2048():
    import inspect

    source = inspect.getsource(mcp_server)
    assert "under 2,000 characters" not in source
    assert "4,096" in source
