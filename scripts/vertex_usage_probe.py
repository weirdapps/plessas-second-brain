#!/usr/bin/env python3
"""Which usage fields the platform returns for an extraction call (issue 211).

The usage log (src/extract/claude_extract.py _log_usage) records the thinking tokens, the
cache writes by time to live, the request id and the response id, as null when the
response does not carry them. This sends a few tiny calls in call shape v2-text (the
cached email system prompt, schema in the prompt, a pinned effort, no structured outputs)
about one synthetic email, through claude_extract.complete like every call site, and says
for each field whether it was reported. The second call should read the cache the first
wrote. About 3,000 cached input tokens and at most 1,024 output tokens a call.

    python scripts/vertex_usage_probe.py            # what it would send; no call
    python scripts/vertex_usage_probe.py --yes      # two calls
"""

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MAX_CALLS = 4
MAX_TOKENS = 1024

EMAIL = {
    "message_id": "probe-1",
    "subject": "Probe: room booking",
    "sender": {"name": "Ann Example", "address": "ann@example.com"},
    "to_recipients": [{"name": "Owner Person", "address": "owner@example.com"}],
    "cc_recipients": [],
    "date_received": "2026-11-04T09:00:00Z",
    "content": "Hello, I booked room 4 for Friday at 10:00. Please confirm by Thursday.",
}

FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
    "thinking_tokens",
    "request_id",
    "response_id",
)


def fields(response: Any) -> dict[str, Any]:
    """The usage fields as _log_usage reads them, None where the response has none."""
    usage = getattr(response, "usage", None)
    ttl = getattr(usage, "cache_creation", None)
    details = getattr(usage, "output_tokens_details", None)
    values: dict[str, Any] = {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", None),
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
        "cache_write_5m_tokens": getattr(ttl, "ephemeral_5m_input_tokens", None),
        "cache_write_1h_tokens": getattr(ttl, "ephemeral_1h_input_tokens", None),
        "thinking_tokens": getattr(details, "thinking_tokens", None),
        "request_id": getattr(response, "_request_id", None),
        "response_id": getattr(response, "id", None),
    }
    return values


def _request():
    from src.extract.call_shape import Request
    from src.extract.prompt import build_email_request_v2

    built = build_email_request_v2(EMAIL)
    # As v2-text sends it whatever the environment says: no structured outputs.
    return Request(route=built.route, system=built.system, user=built.user, structured=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--yes", action="store_true", help="send the calls")
    parser.add_argument(
        "--calls",
        type=int,
        default=2,
        choices=range(1, MAX_CALLS + 1),
        metavar=f"1-{MAX_CALLS}",
        help="how many calls (default 2: a cache write, then a read)",
    )
    args = parser.parse_args(argv)

    request = _request()
    kwargs = request.kwargs(MAX_TOKENS)
    print(
        f"{args.calls} call(s): system prompt {request.chars:,} characters cached one hour,"
        f" output_config {kwargs['output_config']}, max_tokens {MAX_TOKENS}"
    )
    if not args.yes:
        print("Nothing was called. Add --yes to send them.")
        return 2

    from src.extract import claude_extract

    seen: dict[str, list[Any]] = {key: [] for key in FIELDS}
    for n in range(1, args.calls + 1):
        response = claude_extract.complete(**kwargs)
        got = fields(response)
        print(f"call {n}: stop_reason {getattr(response, 'stop_reason', None)}, {got}")
        for key, value in got.items():
            seen[key].append(value)
    print("fields:")
    for key in FIELDS:
        values = [v for v in seen[key] if v is not None]
        verdict = f"reported ({', '.join(str(v) for v in values)})" if values else "not reported"
        print(f"  {key}: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
