"""Tests for the HTTP serving mode: token file, bearer middleware, loopback rule, CLI entry."""

from unittest.mock import patch

import pytest

from src import mcp_http, mcp_server

TOKEN = "t" * 40


def _drive(coro) -> None:
    """Run an ASGI call to completion without an event loop.

    The suite blocks sockets and an asyncio loop needs a socketpair, so these
    tests step the coroutine by hand. The fakes below never suspend.
    """
    try:
        coro.send(None)
    except StopIteration:
        return
    raise AssertionError("the ASGI call suspended; a fake awaited something real")


async def _receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


class _Inner:
    """Stands in for the Starlette app: records the scopes that reach it."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, scope, receive, send) -> None:
        self.calls.append(scope["type"])
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})


def _http(app, headers: list[tuple[bytes, bytes]]) -> list[dict]:
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    _drive(
        app({"type": "http", "method": "POST", "path": "/mcp", "headers": headers}, _receive, send)
    )
    return sent


def test_missing_header_gets_401():
    inner = _Inner()
    sent = _http(mcp_http.BearerAuth(inner, TOKEN), [])
    assert sent[0]["status"] == 401
    assert inner.calls == []


def test_wrong_token_gets_401():
    inner = _Inner()
    sent = _http(mcp_http.BearerAuth(inner, TOKEN), [(b"authorization", b"Bearer nope")])
    assert sent[0]["status"] == 401
    assert inner.calls == []


def test_right_token_reaches_the_app():
    inner = _Inner()
    sent = _http(
        mcp_http.BearerAuth(inner, TOKEN), [(b"authorization", f"Bearer {TOKEN}".encode())]
    )
    assert inner.calls == ["http"]
    assert sent[0]["status"] == 200


def test_lifespan_passes_through_without_a_token():
    inner = _Inner()

    async def send(message: dict) -> None:
        return None

    _drive(mcp_http.BearerAuth(inner, TOKEN)({"type": "lifespan"}, _receive, send))
    assert inner.calls == ["lifespan"]


def _token_file(tmp_path, text: str, mode: int = 0o600) -> str:
    path = tmp_path / "mcp-token"
    path.write_text(text)
    path.chmod(mode)
    return str(path)


def test_token_unset():
    with pytest.raises(ValueError, match="BRAIN_MCP_TOKEN_FILE"):
        mcp_http.load_token("")


def test_token_missing_file(tmp_path):
    with pytest.raises(ValueError, match="cannot read"):
        mcp_http.load_token(str(tmp_path / "absent"))


def test_token_readable_by_others(tmp_path):
    with pytest.raises(ValueError, match="chmod 600"):
        mcp_http.load_token(_token_file(tmp_path, "a" * 64, mode=0o644))


def test_token_too_short(tmp_path):
    with pytest.raises(ValueError, match="openssl rand"):
        mcp_http.load_token(_token_file(tmp_path, "abc"))


def test_token_ok(tmp_path):
    assert mcp_http.load_token(_token_file(tmp_path, "a" * 64 + "\n")) == "a" * 64


def test_build_refuses_a_public_host():
    with pytest.raises(ValueError, match="loopback"):
        mcp_http.build_http_app(object(), TOKEN, host="0.0.0.0")


def test_build_wraps_the_streamable_app():
    app = mcp_http.build_http_app(mcp_server.mcp, TOKEN)
    assert isinstance(app, mcp_http.BearerAuth)
    assert type(app.app).__name__ == "Starlette"


def test_no_arguments_serves_stdio_as_before():
    with patch.object(mcp_server.mcp, "run") as run:
        assert mcp_server.main([]) == 0
    run.assert_called_once_with()


def test_http_without_token_file_refuses(monkeypatch, capsys):
    monkeypatch.delenv("BRAIN_MCP_TOKEN_FILE", raising=False)
    with patch("uvicorn.run") as run:
        assert mcp_server.main(["--http", "127.0.0.1:8765"]) == 2
    run.assert_not_called()
    assert "BRAIN_MCP_TOKEN_FILE" in capsys.readouterr().err


def test_http_on_a_public_host_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_MCP_TOKEN_FILE", _token_file(tmp_path, "a" * 64))
    with patch("uvicorn.run") as run:
        assert mcp_server.main(["--http", "0.0.0.0:8765"]) == 2
    run.assert_not_called()


def test_http_serves_on_loopback(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_MCP_TOKEN_FILE", _token_file(tmp_path, "a" * 64))
    with (
        patch("src.mcp_http.build_http_app", return_value="APP") as build,
        patch("uvicorn.run") as run,
    ):
        assert mcp_server.main(["--http", "127.0.0.1:8765"]) == 0
    build.assert_called_once_with(mcp_server.mcp, "a" * 64, host="127.0.0.1")
    run.assert_called_once_with("APP", host="127.0.0.1", port=8765, log_level="warning")


def test_http_accepts_bracketed_ipv6(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_MCP_TOKEN_FILE", _token_file(tmp_path, "a" * 64))
    with patch("src.mcp_http.build_http_app", return_value="APP"), patch("uvicorn.run") as run:
        assert mcp_server.main(["--http", "[::1]:8765"]) == 0
    assert run.call_args.kwargs["host"] == "::1"


@pytest.mark.parametrize("bad", ["8765", "127.0.0.1:", "127.0.0.1:http", "127.0.0.1:70000"])
def test_http_rejects_a_malformed_address(bad):
    with pytest.raises(SystemExit):
        mcp_server.main(["--http", bad])
