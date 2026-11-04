"""A server's source scope: BRAIN_SCOPE_SOURCES (issue 172 follow-up).

A bot that answers other people (ask-data) may read mail, documents, attachments and
report figures, never Teams, WhatsApp or the owner's past sessions. A tool allowlist
cannot say that: `search` and `read` take every source. So a dedicated server is
started with BRAIN_SCOPE_SOURCES naming what it may read, and every tool and resource
checks it here. Anything outside is refused by name; nothing is quietly relabelled.
A tool whose answer mixes sources it cannot keep apart (recall, the dossiers,
query_items, sql_query, stats, sharepoint_index, the pages and the schema) is refused
on any scoped server.

Unset, the server is unscoped, as every shared server is. Set, it fails closed: an
empty value or an unknown source name refuses every call.
"""

import os
from collections.abc import Iterable

from mcp.server.mcpserver.exceptions import ResourceError, ToolError

SCOPE_ENV = "BRAIN_SCOPE_SOURCES"
SOURCES = (
    "email",
    "news",
    "document",
    "attachment",
    "teams",
    "whatsapp",
    "calendar",
    "conversation",
    "image",
    "figures",
)


def scope() -> frozenset[str] | None:
    """The sources this server may read, or None when it is unscoped.

    Read at each call, so a test or a restart is all it takes. A value naming no
    known source scopes the server to nothing.
    """
    raw = os.environ.get(SCOPE_ENV)
    if raw is None:
        return None
    named = {part.strip().lower() for part in raw.replace(" ", ",").split(",") if part.strip()}
    if not named or named - set(SOURCES):
        return frozenset()
    return frozenset(named)


def _named(found: frozenset[str]) -> str:
    return ", ".join(sorted(found)) or "nothing (an empty or unknown value)"


def require(what: str, needed: Iterable[str]) -> None:
    """Refuse `what` when any of `needed` is outside the scope."""
    allowed = scope()
    if allowed is None:
        return
    outside = sorted(set(needed) - allowed)
    if outside:
        raise ToolError(
            f"{what} reads {', '.join(outside)}, outside this server's scope "
            f"({SCOPE_ENV}: {_named(allowed)}); it is refused, whatever the request says"
        )


def refuse_mixed(what: str, *, resource: bool = False) -> None:
    """Refuse `what` on any scoped server: its answer mixes sources. A resource raises
    ResourceError, the one exception whose words the SDK passes on for a resource."""
    allowed = scope()
    if allowed is None:
        return
    error = ResourceError if resource else ToolError
    raise error(
        f"{what} mixes sources a scope cannot keep apart, so a scoped server refuses it "
        f"({SCOPE_ENV}: {_named(allowed)}); use search and read within the scope"
    )


def narrow(kinds: list[str] | None, offered: Iterable[str]) -> list[str] | None:
    """The kinds a search may return: the ones asked for, each checked, or every offered
    kind within the scope when none was asked for. None when the server is unscoped."""
    allowed = scope()
    if allowed is None:
        return kinds
    if kinds:
        require("search with kinds " + ", ".join(kinds), kinds)
        return kinds
    within = [kind for kind in offered if kind in allowed]
    if not within:
        require("search", offered)
    return within
