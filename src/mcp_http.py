"""Streamable-HTTP serving for the MCP server: bearer auth and the app factory.

stdio stays the default (`python -m src.mcp_server`). `--http HOST:PORT` serves
the same tools to a client that cannot spawn the server itself, such as a bot
that would otherwise load the embedding index (about 1.6 GB) once per message.
Loopback only, and every request must carry the bearer token: anything else on
the host could otherwise read the whole store.
"""

from __future__ import annotations

import hmac
import stat
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server import MCPServer
    from starlette.types import ASGIApp, Receive, Scope, Send

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
MIN_TOKEN_CHARS = 32


def load_token(path: str) -> str:
    """The bearer token from `path`: owner-only permissions, at least 32 characters."""
    if not path:
        raise ValueError("BRAIN_MCP_TOKEN_FILE is not set; --http needs a bearer token file")
    token_file = Path(path).expanduser()
    try:
        mode = token_file.stat().st_mode
        token = token_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"cannot read the token file {token_file}: {exc.strerror}") from exc
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError(f"{token_file} is readable by others; chmod 600 it")
    if len(token) < MIN_TOKEN_CHARS:
        raise ValueError(
            f"{token_file} holds {len(token)} characters, fewer than {MIN_TOKEN_CHARS}; "
            "write a new one with: openssl rand -hex 32"
        )
    return token


def _header(scope: Scope, name: bytes) -> bytes:
    for key, value in scope.get("headers", []):
        if key == name:
            return bytes(value)
    return b""


class BearerAuth:
    """ASGI middleware: an HTTP request without `Authorization: Bearer <token>` gets 401.

    Lifespan messages pass straight through, so the wrapped app still starts its
    session manager.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not hmac.compare_digest(
            _header(scope, b"authorization"), self._expected
        ):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"text/plain; charset=utf-8"),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b"unauthorized\n"})
            return
        await self.app(scope, receive, send)


def build_http_app(server: MCPServer, token: str, host: str = "127.0.0.1") -> BearerAuth:
    """The streamable-HTTP app for `server`, behind bearer auth, for a loopback host."""
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"refusing to serve on {host!r}: HTTP mode binds loopback only")
    # A loopback host also turns on the SDK's DNS-rebinding protection (Host and
    # Origin checks) in streamable_http_app.
    return BearerAuth(server.streamable_http_app(host=host), token)
