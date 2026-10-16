"""A tool that fails says what happened and what to do, never a bare "Error executing tool".

The SDK sends a client only "Error executing tool <name>" for any exception that is
not a ToolError, and logs the cause on the server. Ten calls in six weeks came back
that way (search_emails, recall, search_attachments, sql_query, outlook_live_search),
seven of them on 11-13 Oct, and the agent could neither tell a broken server from an
empty answer nor do anything about it. Every failure now arrives as a ToolError that
names the exception and the next step, whatever raised it: a tool body, a store
function, or a result that does not fit the tool's own output schema.
"""

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from src import mcp_server
from tests import seeded_store
from tests.mcp_listing import allow_an_event_loop, listed_tools


@pytest.fixture
def store(tmp_path, monkeypatch):
    allow_an_event_loop(monkeypatch)
    path = seeded_store.build(tmp_path / "brain.db")
    seeded_store.serve(path, monkeypatch)
    return path


def _failure(name: str, arguments: dict) -> ToolError:
    """The error a tools/call of `name` raises: the handler sends its text as the
    is_error result, word for word."""
    with pytest.raises(ToolError) as caught:
        anyio.run(mcp_server.mcp.call_tool, name, arguments)
    return caught.value


def test_a_crash_in_a_store_function_says_what_and_what_next(store, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("the index is on fire")

    monkeypatch.setattr("src.store.query.query_by_keyword", boom)

    error = _failure("search_emails", {"query": "okapi"})

    assert not isinstance(error, UnexpectedToolError)
    text = str(error)
    assert text.startswith("Error executing tool search_emails: ")
    assert "RuntimeError: the index is on fire" in text
    assert "not an empty answer" in text
    assert "Retry" in text


def test_code_changed_under_a_running_server_says_to_restart_it(store, monkeypatch):
    """The 2026-10-11 failure: a module loaded after a pull asked an old one in memory
    for a name it did not have yet."""

    def stale(*args, **kwargs):
        raise ImportError("cannot import name 'query_stem' from 'src.store.greek'")

    monkeypatch.setattr("src.store.query.search_attachments", stale)

    text = str(_failure("search_attachments", {"query": "okapi"}))

    assert "ImportError: cannot import name 'query_stem'" in text
    assert "restart" in text.lower()


def test_a_result_that_breaks_the_output_schema_names_the_field(store, monkeypatch):
    """Output conversion runs after the tool returns, outside anything the tool can
    catch, and failed as a bare error too."""
    monkeypatch.setattr(mcp_server, "_header", lambda conn: {})

    text = str(_failure("query_decisions", {}))

    assert "data_as_of" in text
    assert "output schema" in text


def test_a_missing_store_says_which_and_what_to_do(store, tmp_path, monkeypatch):
    absent = tmp_path / "absent" / "brain.db"
    monkeypatch.setattr(mcp_server, "DEFAULT_DB", absent)

    text = str(_failure("stats", {}))

    assert str(absent) in text
    assert "pull" in text


def test_a_live_search_without_a_session_says_to_sign_in(store, monkeypatch):
    from src.export import outlook_cli

    def expired(args):
        raise outlook_cli.OutlookCliAuthRequired("session expired")

    monkeypatch.setattr(outlook_cli, "run_outlook_cli", expired)

    text = str(_failure("outlook_live_search", {}))

    assert "sign" in text.lower()
    assert "search_emails" in text


@pytest.mark.parametrize("name", sorted(seeded_store.CALLS))
def test_every_tool_reports_a_crash_in_words(store, monkeypatch, name):
    def boom(*args, **kwargs):
        raise RuntimeError("forced")

    monkeypatch.setattr(mcp_server, "_header", boom)
    monkeypatch.setattr("src.export.outlook_cli.run_outlook_cli", boom)

    error = _failure(name, seeded_store.CALLS[name])

    assert not isinstance(error, UnexpectedToolError), name
    assert "RuntimeError: forced" in str(error), name


def test_the_sample_calls_cover_every_tool():
    assert set(seeded_store.CALLS) == set(listed_tools())


def test_brain_tool_prints_the_same_words(store, monkeypatch, capsys):
    from src import cli
    from tests.brain_cli import brain

    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    monkeypatch.setattr(mcp_server, "_header", lambda conn: {})

    code, out, err = brain(monkeypatch, capsys, "--db", str(store), "tool", "query_decisions")

    assert (code, out) == (1, "")
    assert "data_as_of" in err
    assert "output schema" in err
