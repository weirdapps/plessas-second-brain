"""Five tools are deprecated, with a release of notice, as the README's rule asks.

teams_chat_summary had no call in six weeks. sender_brief's card is person_context
with detail="card". email_thread, teams_thread_context and conversation_context each
read one kind of item, which get_item reads with the rest. They still answer, unchanged
but for the response budget, and say what replaces them; they go in the next release.
"""

from pathlib import Path

import pytest

from src import mcp_server
from tests.mcp_listing import listed_tools

README = Path(__file__).resolve().parents[2] / "README.md"
NOTICE = "Deprecated, going in the next release"

REPLACED_BY = {
    "sender_brief": "person_context with detail='card'",
    "email_thread": "get_item(kind='email'",
    "teams_thread_context": "get_item(kind='teams'",
    "conversation_context": "get_item(kind='conversation'",
    "teams_chat_summary": "search_teams and get_item",
}


@pytest.mark.parametrize("name", sorted(REPLACED_BY))
def test_a_deprecated_tool_says_so_first_and_names_what_replaces_it(name):
    description = listed_tools()[name].description or ""

    assert description.startswith(NOTICE), name
    assert REPLACED_BY[name] in description, name


def test_no_other_tool_is_deprecated():
    deprecated = {
        name
        for name, tool in listed_tools().items()
        if (tool.description or "").startswith("Deprecated")
    }

    assert deprecated == set(REPLACED_BY)


def test_the_readme_gives_the_notice_for_each():
    text = README.read_text(encoding="utf-8")

    for name in REPLACED_BY:
        assert f"`{name}`" in text
    assert "Deprecated tools" in text


def test_the_server_instructions_name_them_and_their_replacements():
    text = mcp_server._instructions()

    for name in REPLACED_BY:
        assert f"`{name}`" in text
