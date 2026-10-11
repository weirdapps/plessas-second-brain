"""The MCP tools as a client lists them, read without an event loop.

The suite blocks sockets, and an asyncio loop needs a socketpair. Listing tools
never awaits anything, so the coroutine finishes on its first step. A test that
calls a tool the way the server does needs a loop: allow_an_event_loop.
"""


def allow_an_event_loop(monkeypatch) -> None:
    """Let this test run an asyncio event loop, as MCPServer.call_tool needs.

    A loop opens a local socket pair for its self-pipe, and tests/conftest.py blocks
    every socket. Only socketpair is let through, as the C-level pair, which cannot
    reach the network: a socket that could is still refused.
    """
    import _socket
    import socket

    monkeypatch.setattr(socket, "socketpair", _socket.socketpair)


def listed_tools() -> dict:
    """{name: Tool} exactly as `tools/list` returns them."""
    from src.mcp_server import mcp

    step = mcp.list_tools()
    try:
        step.send(None)
    except StopIteration as done:
        return {tool.name: tool for tool in done.value}
    step.close()
    raise AssertionError("listing the tools suspended; it used to finish in one step")


def registered_tool(name: str):
    """The SDK's own record of a tool, for its argument validation and result
    conversion: the two steps `tools/call` runs around the function."""
    from src.mcp_server import mcp

    return mcp._tool_manager.get_tool(name)


def advertised_text(name: str) -> str:
    """What a client shows a model for one tool: its description and every
    parameter's description, whitespace collapsed."""
    tool = listed_tools()[name]
    parts = [tool.description or ""]
    parts += [p.get("description", "") for p in tool.input_schema.get("properties", {}).values()]
    return " ".join(" ".join(parts).split())
