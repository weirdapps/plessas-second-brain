"""The tool surface after issue 172: twelve tools, one filter object, one envelope.

The owner's call (2026-11-04): migrate the callers, then cut, with no alias period.
So none of the 28 names that went is listed, and no file that tells a session or a
script which tool to call (the README, the skill, the deploy guide, the CLI's help)
names one as a tool.
"""

import re
from pathlib import Path

import pytest

from src import mcp_server
from tests.mcp_listing import listed_tools, registered_tool

ROOT = Path(__file__).resolve().parents[2]

GONE = {
    "sender_brief": "person_context detail='card'",
    "email_thread": "read('email:<id>')",
    "teams_thread_context": "read('teams:<id>')",
    "conversation_context": "read('conversation:<id>')",
    "teams_chat_summary": "search source=teams",
    "get_item": "read",
    "search_emails": "search",
    "query_emails": "search with a person, topic or date filter",
    "search_attachments": "search source=attachment",
    "search_teams": "search source=teams",
    "search_whatsapp": "search source=whatsapp",
    "search_conversations": "search source=conversation",
    "attachment_image_search": "search source=image",
    "query_calendar_events": "search source=calendar",
    "query_decisions": "query_items kind=decisions",
    "query_actions": "query_items kind=actions",
    "stale_threads": "query_items kind=stale",
    "topic_page": "topic_context detail='page'",
    "sql_schema": "sql_query with no sql",
}

# Where a caller learns a tool's name.
GUIDES = ["README.md", "skill/recall.md", "docs/DEPLOY.md"]


def test_none_of_the_cut_tools_is_listed():
    assert not set(GONE) & set(listed_tools())


def _guide_text(guide: str) -> str:
    """The guide, less the README's table of what each removed tool became."""
    text = (ROOT / guide).read_text()
    head, sep, rest = text.partition("### Tools removed in issue 172")
    return head + (rest[rest.index("\n## ") :] if sep else "")


@pytest.mark.parametrize("guide", GUIDES)
def test_no_guide_calls_a_cut_tool(guide):
    text = _guide_text(guide)
    for name in GONE:
        assert not re.search(
            rf"`{name}`|`{name}\(|mcp__second-brain__{name}\b|brain tool {name}\b", text
        ), f"{guide} names {name}; it is {GONE[name]} now"


@pytest.mark.parametrize("tool", sorted(listed_tools()))
def test_no_tool_description_points_at_a_cut_tool(tool):
    """A description loads with its tool: one naming get_item sent a client to nothing."""
    from tests.mcp_listing import advertised_text

    text = advertised_text(tool)
    # Two removed names live on as result keys: query_items' stale_threads list and
    # topic_context's topic_page.
    for key in ("`stale_threads`", "`topic_page`"):
        text = text.replace(key, "")
    for name in GONE:
        assert not re.search(rf"\b{name}\b", text), f"{tool} names {name}; it is {GONE[name]}"


def test_every_filter_key_is_described():
    schema = listed_tools()["search"].input_schema
    props = schema["$defs"]["Filter"]["properties"]
    assert props and all(p.get("description") for p in props.values())
    assert schema["$defs"]["Filter"].get("additionalProperties") is False


def test_a_filter_key_a_tool_cannot_honour_is_refused_by_name(tmp_path, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="filter workspace does not apply"):
        registered_tool("person_context").fn(name_or_email="x", filter={"workspace": "y"})


def test_every_tool_answers_in_the_one_envelope():
    schemas = {name: tool.output_schema for name, tool in listed_tools().items()}
    assert len({repr(sorted(s.items())) for s in schemas.values()}) == 1


def test_the_instructions_have_a_test_of_their_length():
    assert len(mcp_server._instructions()) < 2000
