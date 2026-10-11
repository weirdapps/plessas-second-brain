"""`brain tool`: any MCP tool, called in this process the way the server calls it.

The CLI mirrored the read tools by hand, seventeen commands over the same store
functions, and fell behind them: its `stats` printed seven counts without the
freshness, coverage or staleness the tool returns, and nothing read WhatsApp, the
calendar, recall, conversations or SQL. Sessions ran the mirror six times in five
weeks. This calls a tool through MCPServer.call_tool, the call the server makes for
tools/call: the tool's input schema checks the arguments, its function reads through
the read-only connection, its answer keeps the freshness header and the response
budget, and what is printed is the structured result the server would send, as JSON.

Exit codes: 0 the answer is on stdout; 1 the tool refused or failed, its message on
stderr; 2 no such tool, or arguments its input schema rejects, the schema on stderr.
"""

import contextlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any


class ToolCallError(Exception):
    """A call that printed no answer; `code` is its exit code."""

    code = 1


class UnknownTool(ToolCallError):
    code = 2


class InvalidInput(ToolCallError):
    """Arguments the tool's input schema rejects, or that are not one JSON object."""

    code = 2

    def __init__(self, message: str, schema: dict):
        super().__init__(message)
        self.schema = schema


def listing() -> list[dict]:
    """The tools as tools/list gives them to a client, without their output schemas:
    name, description, inputSchema and annotations."""
    import anyio

    from src.mcp_server import mcp

    tools = anyio.run(mcp.list_tools)
    return [
        tool.model_dump(mode="json", by_alias=True, exclude_none=True, exclude={"output_schema"})
        for tool in tools
    ]


def call(name: str, arguments: Any) -> dict:
    """The structured result of the tool `name`, as the server would send it.

    `arguments` is the tool's arguments: a dict, or JSON text holding one. Raises
    UnknownTool, InvalidInput (with the tool's input schema) or ToolCallError.
    """
    import anyio
    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
    from mcp.types import CallToolResult
    from pydantic import ValidationError

    from src.mcp_server import mcp

    tools = {tool["name"]: tool for tool in listing()}
    if name not in tools:
        raise UnknownTool(
            f"no tool is named {name!r}. The tools: {', '.join(sorted(tools))} "
            "(brain tool --list describes them)"
        )
    schema = tools[name]["inputSchema"]
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError as exc:
            raise InvalidInput(f"{name}: the arguments are not JSON ({exc})", schema) from None
    if not isinstance(arguments, dict):
        raise InvalidInput(
            f'{name}: the arguments must be one JSON object, as in \'{{"query": "budget"}}\'',
            schema,
        )
    try:
        result = anyio.run(mcp.call_tool, name, arguments)
    except UnexpectedToolError as exc:
        # The server tells a client only "Error executing tool <name>"; whoever runs the
        # command on this host gets the cause as well.
        cause = exc.__cause__
        raise ToolCallError(f"{name} failed: {type(cause).__name__}: {cause}") from exc
    except ToolError as exc:
        if isinstance(exc.__cause__, ValidationError):
            raise InvalidInput(_rejected(name, exc.__cause__), schema) from None
        # The tool's own words, which the server sends a client as an error result.
        raise ToolCallError(str(exc.__cause__ or exc)) from None
    if not isinstance(result, CallToolResult) or result.structured_content is None:
        # Every tool declares an output schema, and none asks the client for input.
        raise ToolCallError(f"{name} gave no structured result")
    return result.structured_content


def _rejected(name: str, error) -> str:
    """One line per field the schema refused, with pydantic's reason and not the value."""
    fields = "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or 'arguments'}: {item['msg']}"
        for item in error.errors()
    )
    return f"{name} rejected its arguments: {fields}"


@contextlib.contextmanager
def _serving(db: Path | str | None) -> Iterator[None]:
    """The tools read the store at `db` (the CLI's --db) for the length of a call.

    The server reads the configured store through two module-level paths, its own and
    sql_readonly's; both are put back afterwards.
    """
    if db is None:
        yield
        return
    from src import mcp_server
    from src.store import sql_readonly

    saved = mcp_server.DEFAULT_DB, sql_readonly.DEFAULT_DB
    mcp_server.DEFAULT_DB = sql_readonly.DEFAULT_DB = Path(db)
    try:
        yield
    finally:
        mcp_server.DEFAULT_DB, sql_readonly.DEFAULT_DB = saved


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def run(name: str, arguments: Any = None, db: Path | str | None = None) -> int:
    """Call the tool `name` and print its answer as JSON; the exit code.

    A refusal or failure goes to stderr, and for input the schema rejects the schema
    follows it, so the caller sees what the tool takes.
    """
    try:
        with _serving(db):
            answer = call(name, {} if arguments is None else arguments)
    except ToolCallError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if isinstance(exc, InvalidInput):
            print(f"{name} takes:", file=sys.stderr)
            print(json.dumps(exc.schema, ensure_ascii=False, indent=2), file=sys.stderr)
        return exc.code
    _print(answer)
    return 0


def command(name: str | None, arguments: str | None, *, list_tools: bool, db) -> int:
    """`brain tool [--list] [NAME [JSON]]`."""
    if list_tools or not name:
        if list_tools and not name:
            _print({"tools": listing()})
            return 0
        print(
            "Error: name one tool and its arguments as JSON, as in "
            'brain tool search_emails \'{"query": "budget"}\', or list them with '
            "brain tool --list",
            file=sys.stderr,
        )
        return 2
    return run(name, arguments, db=db)
