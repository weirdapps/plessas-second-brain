"""How extraction calls are built: call shape v1, as they were, v2 (issue 163), or
v2-text, v2 without structured outputs, the default.

`BRAIN_EXTRACT_CALL_SHAPE` picks it at each call: `v2-text` when unset. `v1` keeps every
request byte for byte as it was before issue 163, and is the way back. Under v2 and
v2-text, per route (ROUTES):

- The instructions and two worked examples go in `system`, the same bytes on every call
  of a route, with `cache_control` (one hour) on its last block. The email and the
  document stay in the user turn, inside the random fence (src/extract/untrusted.py),
  after the cached prefix, so the tag drawn per call never breaks the cache.
- The reply is held to schema v2 (src/extract/schema_v2.py) where the route has one: under
  v2 by `output_config.format` (structured outputs); under v2-text by the schema itself,
  stated in a system block between the instructions and the cached examples, with the
  reply read tolerantly. The parser validates and normalises the reply either way, for a
  refusal or a reply cut short, which no schema covers.
- `output_config.effort` is pinned per route, so cost and truncation no longer move with
  the model's default, and a changed default cannot invalidate the cache.
- A reply stopped at max_tokens is asked once more at twice the budget
  (max_tokens_attempts): thinking counts toward max_tokens, and an identical retry would
  stop at the same place.

Why v2-text exists: on the owner's Vertex project every v2 call of the 2026-10-11 eval
failed with HTTP 400, "Organization Policy constraint
constraints/vertexai.allowedPartnerModelFeatures violated ... disallowed feature
structured_outputs". A probe on 2026-10-16, one feature per call on claude-sonnet-5-5 at
`eu`, found `output_config.effort` and `cache_control` (five-minute and one-hour, a cache
read on the second call) served, and `output_config.format` the one refused. Effort has
also run in production since image transcription pinned it (src/extract/image_vision.py).
v2 is kept for a project that allows structured outputs.
"""

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any

from src.extract.schema_v2 import ATTACHMENT_SCHEMA_V2, EMAIL_SCHEMA_V2

CALL_SHAPE_VAR = "BRAIN_EXTRACT_CALL_SHAPE"
V1, V2, V2_TEXT = "v1", "v2", "v2-text"
SHAPES = (V1, V2, V2_TEXT)
DEFAULT_SHAPE = V2_TEXT

# One hour, not the five-minute default: the sync runs hourly, so a one-hour entry
# written by one run is still warm for the next (a read refreshes it), where a
# five-minute one would be written again by every run.
CACHE_CONTROL = {"type": "ephemeral", "ttl": "1h"}

# A reply stopped at max_tokens is sent again once, at this multiple of the budget.
RETRY_FACTOR = 2


def call_shape() -> str:
    """`v1`, `v2` or `v2-text`, from BRAIN_EXTRACT_CALL_SHAPE; unset or blank is
    DEFAULT_SHAPE, `v2-text`.

    Any other value stops the call with ClaudeNotConfigured, which leaves the work
    pending as an unset model does: a typo must not run one shape while the owner
    believes another is on.
    """
    raw = os.environ.get(CALL_SHAPE_VAR, "")
    value = raw.strip().lower() or DEFAULT_SHAPE
    if value not in SHAPES:
        from src.extract.claude_extract import ClaudeNotConfigured

        raise ClaudeNotConfigured(
            f"{CALL_SHAPE_VAR}={raw!r} is not a call shape: set it to one of"
            f" {', '.join(SHAPES)}, or unset it for {DEFAULT_SHAPE}"
        )
    return value


def uses_schema_v2(shape: str | None = None) -> bool:
    """Whether calls in `shape` (the configured one when None) ask for schema v2, with the
    v2 prompts: v2 and v2-text do, v1 does not."""
    return (shape or call_shape()) != V1


# The routes whose v2 requests are opt-in (issue 211): the calendar route, and the cached
# system prompts of the Teams and WhatsApp routes. A deploy must not change those requests
# before the owner has run the eval on them, so each stays as it was (v1's calendar call,
# the thread prompts as built) until BRAIN_EXTRACT_V2_ROUTES names it. Ship, check the
# eval, then widen.
V2_ROUTES_VAR = "BRAIN_EXTRACT_V2_ROUTES"
OPT_IN_ROUTES = ("calendar", "teams", "whatsapp")


def v2_route(name: str, shape: str | None = None) -> bool:
    """Whether the opt-in route `name` takes its v2 request: the call shape (the configured
    one when None) asks for schema v2, and BRAIN_EXTRACT_V2_ROUTES, a list separated by
    commas, empty by default, names it.

    A name that is not one of OPT_IN_ROUTES stops the call with ClaudeNotConfigured, as a
    mistyped call shape does: a typo must not leave a route off while the owner believes
    it on."""
    raw = os.environ.get(V2_ROUTES_VAR, "")
    named = {part.strip().lower() for part in raw.split(",") if part.strip()}
    wrong = sorted(named - set(OPT_IN_ROUTES))
    if wrong:
        from src.extract.claude_extract import ClaudeNotConfigured

        raise ClaudeNotConfigured(
            f"{V2_ROUTES_VAR}={raw!r} names {', '.join(wrong)}, not a route: name any of"
            f" {', '.join(OPT_IN_ROUTES)}, or leave it empty"
        )
    return name in named and uses_schema_v2(shape)


def structured_outputs(shape: str | None = None) -> bool:
    """Whether calls in `shape` (the configured one when None) send `output_config.format`:
    v2 alone."""
    return (shape or call_shape()) == V2


@dataclass(frozen=True)
class Route:
    """What a v2 call on one route pins: its effort, its output budget, its schema."""

    name: str
    effort: str
    max_tokens: int
    schema: dict | None = None

    def output_config(self, structured: bool = True) -> dict:
        """The effort (route_effort: the pinned one, or the route's override), and with
        `structured` the schema as structured output where the route has one; without it
        (v2-text) the effort alone."""
        config: dict[str, Any] = {"effort": route_effort(self)}
        if structured and self.schema is not None:
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
    # room, not the effort. No schema: their parsers are unchanged. Their system prompts
    # are cached with worked threads (teams_prompt.cached_prompt, issue 211).
    "teams": Route("teams", effort="high", max_tokens=8192),
    "whatsapp": Route("whatsapp", effort="high", max_tokens=8192),
    # Calendar events (issue 211): two answer shapes, so two schemas, which the calendar
    # extractor sets on a copy of this route by is_invitation
    # (src/extract/calendar_extractor.py). 8,192 as since issue 203.
    "calendar": Route("calendar", effort="high", max_tokens=8192),
}


# Each route's effort override (issue 211), for an effort sweep's result to reach
# production without a code change: set, it replaces the route's pinned effort in every
# call of that route under v2 and v2-text; v1 sends no effort. Spelled out, not built, so
# a search for the variable finds it here.
EFFORT_VARS = {
    "email": "BRAIN_EXTRACT_EFFORT_EMAIL",
    "attachment": "BRAIN_EXTRACT_EFFORT_ATTACHMENT",
    "teams": "BRAIN_EXTRACT_EFFORT_TEAMS",
    "whatsapp": "BRAIN_EXTRACT_EFFORT_WHATSAPP",
    "calendar": "BRAIN_EXTRACT_EFFORT_CALENDAR",
}
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def effort_var(route_name: str) -> str:
    """The variable that overrides `route_name`'s effort."""
    return EFFORT_VARS[route_name]


def route_effort(route: Route) -> str:
    """The effort `route`'s calls are sent at: its override when set, else the pinned one.

    A value that is not an effort stops the call with ClaudeNotConfigured, as a mistyped
    call shape does: a sweep's result must not silently run at another effort."""
    var = EFFORT_VARS.get(route.name)
    raw = os.environ.get(var, "") if var else ""
    value = raw.strip().lower()
    if not value:
        return route.effort
    if value not in EFFORTS:
        from src.extract.claude_extract import ClaudeNotConfigured

        raise ClaudeNotConfigured(
            f"{var}={raw!r} is not an effort: set it to one of {', '.join(EFFORTS)},"
            f" or unset it for {route.effort}"
        )
    return value


def max_tokens_attempts(route: Route) -> tuple[int, int]:
    """The budgets a call on `route` is sent with: once, and once more at RETRY_FACTOR
    times as much if the first reply stopped at max_tokens."""
    return route.max_tokens, route.max_tokens * RETRY_FACTOR


def thread_call(route_name: str, v1_max_tokens: int) -> tuple[tuple[int, ...], dict]:
    """The output budgets a call on a route with an unchanged prompt (Teams, WhatsApp) is
    sent with, and what call shape v2 adds to it: under v1 one call at `v1_max_tokens`,
    once more at the route's budget if that reply stopped at max_tokens, and nothing
    added; under v2 the route's budgets and its output_config.

    v1's first call is the request as it always was. The second exists because a thread
    cut short at 2,048 failed its parse on every run: 18 of the 23 Teams threads left
    failed by the 2026-10-11 re-extraction (issue 163 counted 13% of calls cut short)."""
    route = ROUTES[route_name]
    if call_shape() == V1:
        return (v1_max_tokens, route.max_tokens), {}
    return max_tokens_attempts(route), {"output_config": route.output_config()}


def system_blocks(instructions: str, examples: str) -> list[dict]:
    """The system prompt as two text blocks, cached at the last: the cache is a prefix,
    so the one marker covers both."""
    return [
        {"type": "text", "text": instructions},
        {"type": "text", "text": examples, "cache_control": dict(CACHE_CONTROL)},
    ]


def schema_text(schema: dict) -> str:
    """Schema v2 as call shape v2-text states it in the system prompt, in place of
    structured outputs. Compact JSON: the same bytes on every call, so the cache holds it."""
    return (
        "OUTPUT FORMAT\n"
        "Answer with one JSON object and nothing else: no Markdown code fence, no text"
        " before or after it. It must be valid against the JSON Schema below: every"
        " property it lists present, in the order it lists them, and no property it does"
        ' not list. "format": "date" means a YYYY-MM-DD date.\n'
        + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    )


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
    """One call in shape v2 or v2-text: its route, the system blocks, the user turn, and
    `source`, the third-party text the user turn fences, which evidence quotes are checked
    against. `structured` is whether it sends structured outputs, taken from the call shape
    it is built under (v2 does, v2-text does not), so a request built for a batch line is
    sent as it was built."""

    route: Route
    system: tuple[dict, ...]
    user: str
    source: str = ""
    structured: bool = field(default_factory=structured_outputs)
    # An email's newest message, which its thread updates are checked against, and what
    # was left out of its quoted history (src/extract/quotes.py); empty for other routes.
    newest: str = ""
    quotes: dict = field(default_factory=dict)

    @property
    def blocks(self) -> list[dict]:
        """The system blocks sent: as built, and without structured outputs the schema
        stated in a block of its own before the last, so the one cache marker, on the last
        block, still covers every block."""
        blocks = [dict(block) for block in self.system]
        if not self.structured and self.route.schema is not None:
            stated = {"type": "text", "text": schema_text(self.route.schema)}
            blocks.insert(len(blocks) - 1 if blocks else 0, stated)
        return blocks

    def kwargs(self, max_tokens: int) -> dict:
        """What claude_extract.complete() is given for this call."""
        return {
            "max_tokens": max_tokens,
            "system": self.blocks,
            "messages": [{"role": "user", "content": self.user}],
            "output_config": self.route.output_config(structured=self.structured),
        }

    @property
    def chars(self) -> int:
        """The characters sent, system prompt included: what a token budget holds room for."""
        return sum(len(block["text"]) for block in self.blocks) + len(self.user)
