"""How extraction calls are built: call shape v1, as they were, or v2 (issue 163).

`BRAIN_EXTRACT_CALL_SHAPE` picks it at each call: `v1` when unset, and `v1` keeps every
request byte for byte as it was. `v2` stays off until the extraction eval
(scripts/extraction_eval.py --call-shape) has run both shapes on the owner's set and he
has chosen. Under v2, per route (ROUTES):

- The instructions and two worked examples go in `system`, the same bytes on every call
  of a route, with `cache_control` (one hour) on its last block. The email and the
  document stay in the user turn, inside the random fence (src/extract/untrusted.py),
  after the cached prefix, so the tag drawn per call never breaks the cache.
- `output_config.format` holds the reply to schema v2 (src/extract/schema_v2.py) where
  the route has one. The parser still validates and normalises the reply, for a refusal
  or a reply cut short, which the schema does not cover.
- `output_config.effort` is pinned per route, so cost and truncation no longer move with
  the model's default, and a changed default cannot invalidate the cache.
- A reply stopped at max_tokens is asked once more at twice the budget
  (max_tokens_attempts): thinking counts toward max_tokens, and an identical retry would
  stop at the same place.

Live on Vertex AI for claude-sonnet-5-5, per the Claude API platform table and Google's
pages for the model: prompt caching (5-minute and 1-hour), structured outputs through
`output_config.format`, and `output_config.effort`. Effort has run in production since
image transcription pinned it (src/extract/image_vision.py); caching and structured
outputs are documented, not yet exercised here, and the eval's first v2 run is the check:
its usage shows cache reads and its rows schema-valid replies, or its errors say why not.
"""

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any

from src.extract.schema_v2 import ATTACHMENT_SCHEMA_V2, EMAIL_SCHEMA_V2

CALL_SHAPE_VAR = "BRAIN_EXTRACT_CALL_SHAPE"
SHAPES = ("v1", "v2")

# One hour, not the five-minute default: the sync runs hourly, so a one-hour entry
# written by one run is still warm for the next (a read refreshes it), where a
# five-minute one would be written again by every run.
CACHE_CONTROL = {"type": "ephemeral", "ttl": "1h"}

# A reply stopped at max_tokens is sent again once, at this multiple of the budget.
RETRY_FACTOR = 2


def call_shape() -> str:
    """`v1` or `v2`, from BRAIN_EXTRACT_CALL_SHAPE; unset or blank is `v1`.

    Any other value stops the call with ClaudeNotConfigured, which leaves the work
    pending as an unset model does: a typo must not run v1 while the owner believes v2
    is on.
    """
    raw = os.environ.get(CALL_SHAPE_VAR, "")
    value = raw.strip().lower() or "v1"
    if value not in SHAPES:
        from src.extract.claude_extract import ClaudeNotConfigured

        raise ClaudeNotConfigured(
            f"{CALL_SHAPE_VAR}={raw!r} is not a call shape: set it to v1 or v2, or unset it for v1"
        )
    return value


@dataclass(frozen=True)
class Route:
    """What a v2 call on one route pins: its effort, its output budget, its schema."""

    name: str
    effort: str
    max_tokens: int
    schema: dict | None = None

    def output_config(self) -> dict:
        config: dict[str, Any] = {"effort": self.effort}
        if self.schema is not None:
            config["format"] = {"type": "json_schema", "schema": self.schema}
        return config


# Every route is pinned at `high`, the effort today's calls already run at as Sonnet
# 5.5's default. Turning v2 on therefore changes the shape of the request and not how
# hard the model thinks, so the eval's v1-against-v2 comparison measures the shape
# alone; sending the default explicitly is equivalent to omitting it. A lower effort is
# a separate decision, for `scripts/extraction_eval.py --effort` to measure first.
ROUTES = {
    # The route the eval grades, so the first whose effort a sweep can lower. 8,192 is
    # today's budget: the JSON of a long thread runs long.
    "email": Route("email", effort="high", max_tokens=8192, schema=EMAIL_SCHEMA_V2),
    # One text, a spreadsheet digest, a part of a long document, or the merge of the
    # parts. 55-70% of the spend, so the first route to sweep once the eval covers
    # documents; dense Greek text until then, where a lower effort risks dropped facts.
    "attachment": Route("attachment", effort="high", max_tokens=8192, schema=ATTACHMENT_SCHEMA_V2),
    # Teams and WhatsApp: thinking counts toward max_tokens, and at 2,048 it cut 13% of
    # Teams calls short, so the budget is raised and the effort kept: what failed was the
    # room, not the effort. No schema: their prompts and parsers are unchanged, and their
    # system prompts are too short to cache (512 tokens is the least Sonnet 5.5 caches),
    # so no marker either.
    "teams": Route("teams", effort="high", max_tokens=8192),
    "whatsapp": Route("whatsapp", effort="high", max_tokens=8192),
}


def max_tokens_attempts(route: Route) -> tuple[int, int]:
    """The budgets a call on `route` is sent with: once, and once more at RETRY_FACTOR
    times as much if the first reply stopped at max_tokens."""
    return route.max_tokens, route.max_tokens * RETRY_FACTOR


def thread_call(route_name: str, v1_max_tokens: int) -> tuple[tuple[int, ...], dict]:
    """The output budgets a call on a route with an unchanged prompt (Teams, WhatsApp) is
    sent with, and what call shape v2 adds to it: under v1 one call at `v1_max_tokens`
    and nothing added; under v2 the route's budgets and its output_config."""
    if call_shape() == "v1":
        return (v1_max_tokens,), {}
    route = ROUTES[route_name]
    return max_tokens_attempts(route), {"output_config": route.output_config()}


def system_blocks(instructions: str, examples: str) -> list[dict]:
    """The system prompt as two text blocks, cached at the last: the cache is a prefix,
    so the one marker covers both."""
    return [
        {"type": "text", "text": instructions},
        {"type": "text", "text": examples, "cache_control": dict(CACHE_CONTROL)},
    ]


def prompt_hash(system: Any, output_format: Any) -> str | None:
    """A 12-hex fingerprint of a request's fixed part: its system prompt and its output
    schema. Equal hashes mean byte-identical instructions, which is what the cache needs
    and what says which prompt produced a row. None for a call that has neither."""
    if not system and not output_format:
        return None
    canonical = json.dumps(
        {"system": system, "format": output_format},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class Request:
    """One call in shape v2: its route, the system blocks, the user turn, and `source`,
    the third-party text the user turn fences, which evidence quotes are checked against."""

    route: Route
    system: tuple[dict, ...]
    user: str
    source: str = ""

    def kwargs(self, max_tokens: int) -> dict:
        """What claude_extract.complete() is given for this call."""
        return {
            "max_tokens": max_tokens,
            "system": [dict(block) for block in self.system],
            "messages": [{"role": "user", "content": self.user}],
            "output_config": self.route.output_config(),
        }

    @property
    def chars(self) -> int:
        """The characters sent, system prompt included: what a token budget holds room for."""
        return sum(len(block["text"]) for block in self.system) + len(self.user)
