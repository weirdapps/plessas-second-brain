"""
Claude-based email extraction via Vertex AI or direct Anthropic API.

Drop-in replacement for the Gemini extractor in local.py.
"""

import os
import sys
import threading
import time
from pathlib import Path

from src.llm_policy import (
    Action,
    Attempt,
    ReauthResult,
    decide,
    reauth,
    resolve_deadline,
    running_on_linux,
)

REPO_ROOT = Path(__file__).parent.parent.parent
# Default model — Vertex AI requires '@' separator, direct API uses '-'
CLAUDE_MODEL_BASE = os.environ.get("CLAUDE_EXTRACT_MODEL") or os.environ.get(
    "VERTEX_MODEL_EXTRACT", "claude-sonnet-4-6"
)

# 4096 truncated dense inputs: one news digest synthesis covers ~50 articles, so
# its extraction JSON legitimately runs long and stopped mid-object.
# attachment_pipeline already uses 8192 for the same reason.
MAX_OUTPUT_TOKENS = 8192


def _response_text(response) -> str:
    """First text block of a response, skipping any leading thinking block.

    Indexing content[0] assumed the first block carries .text. With extended
    thinking the first block is a ThinkingBlock (.thinking, no .text), which
    raised AttributeError and failed the extraction outright.
    """
    for block in response.content:
        text = getattr(block, "text", None)
        if text is not None:
            return text
    raise ValueError("LLM response contained no text block")


# The extraction client is built once and shared across the whole run. A fresh
# client per item makes google.auth fork `gcloud config get project` on every
# Vertex call (authorized_user ADC has no embedded project; ~2s per fork), which
# on an email backlog blew past the sb-noon-catchup systemd start timeout. The
# anthropic client is httpx-based and thread-safe, so one instance is safe to
# share across the extraction ThreadPoolExecutor workers. It is never closed per
# item — it lives until the process exits.
_client_lock = threading.Lock()
_cached_client_and_model = None


def _build_client_and_model():
    """Build a new Anthropic client + resolved model name (no caching)."""
    model = CLAUDE_MODEL_BASE

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        from anthropic import Anthropic

        return Anthropic(api_key=api_key, timeout=60.0), model

    project_id = os.environ.get("VERTEX_SDK_PROJECT") or os.environ.get(
        "ANTHROPIC_VERTEX_PROJECT_ID"
    )
    region = os.environ.get("VERTEX_SDK_REGION") or os.environ.get(
        "CLOUD_ML_REGION", "europe-west1"
    )
    if project_id:
        from anthropic import AnthropicVertex

        # Vertex AI wants '@' before a date snapshot, e.g.
        # claude-sonnet-4-5-20250929 -> claude-sonnet-4-5@20250929.
        # The len>=8 guard makes this fire ONLY for a trailing YYYYMMDD date;
        # version-only ids like claude-opus-4-8 pass through unchanged and are
        # valid date-less on Vertex (verified: opus-4-8 @ eu returns 200).
        if "@" not in model:
            parts = model.rsplit("-", 1)
            if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) >= 8:
                model = f"{parts[0]}@{parts[1]}"
        return AnthropicVertex(project_id=project_id, region=region, timeout=120.0), model

    raise RuntimeError(
        "No Claude credentials found. Set ANTHROPIC_API_KEY for direct API, "
        "or ANTHROPIC_VERTEX_PROJECT_ID + CLOUD_ML_REGION for Vertex AI."
    )


def _get_client_and_model():
    """Return the process-wide shared Anthropic client + model, built once.

    Reused across every extraction (emails, conversations, attachments, images)
    so the Vertex auth handshake (which forks gcloud) runs once per process
    instead of once per item. Do NOT close the returned client — it is shared.
    """
    global _cached_client_and_model
    if _cached_client_and_model is None:
        with _client_lock:
            if _cached_client_and_model is None:
                _cached_client_and_model = _build_client_and_model()
    return _cached_client_and_model


def reset_client_cache():
    """Drop the cached client so the next call rebuilds it (tests, env changes)."""
    global _cached_client_and_model
    _cached_client_and_model = None


# Process-wide latch: once a re-auth has FAILED, no other worker repeats the wait.
#
# On Linux reauth() has no local remedy and polls for PUSH_WAIT_SECONDS (1020s) waiting
# for the Mac's token push. call_with_policy runs per item, and three of its call sites
# drive a ThreadPoolExecutor (local.py:397, attachment_pipeline.py:361,
# image_pipeline.py:284). Without a latch each worker in turn pays its own full wait, so
# four workers spend 68 minutes discovering, four times over, the one fact the first
# worker already established in 17: the push is not coming.
#
# The lock is held ACROSS the reauth() call, not merely around the flag reads. A
# check-then-call that releases the lock in between latches nothing: llm_policy's
# _REAUTH_LOCK already serialises reauth() internally, so a second worker has passed the
# flag check and is parked inside _REAUTH_LOCK long before the first worker fails 1020s
# later. It would then acquire that lock and repeat the whole wait, which is precisely
# the behaviour this exists to prevent. Holding across the call therefore adds no
# serialisation that _REAUTH_LOCK was not already imposing.
_reauth_latch_lock = threading.Lock()
_reauth_failed = False


def reset_reauth_latch() -> None:
    """Clear the failed-reauth latch. For tests, which share one process."""
    global _reauth_failed
    with _reauth_latch_lock:
        _reauth_failed = False


def _reauth_unless_latched(*, is_linux: bool) -> ReauthResult | None:
    """Run reauth() unless an earlier one already failed. None means "skipped, latched".

    Only FAILED latches. SUCCEEDED and SKIPPED both leave the flag clear on purpose:
    the credential is good as of now, so a worker that hits an unrelated auth error
    later deserves its own full attempt rather than inheriting a verdict that has
    stopped being true. Preserving that is what keeps the one affordable wait available
    to the 30-minute units.
    """
    global _reauth_failed
    with _reauth_latch_lock:
        if _reauth_failed:
            return None
        result = reauth(is_linux=is_linux)
        if result is ReauthResult.FAILED:
            _reauth_failed = True
        return result


def call_with_policy(fn, *, max_call_seconds: float) -> object:
    """Run fn() under the shared retry/reauth policy.

    fn is a zero-argument callable that performs a single SDK request and returns
    the response or raises.  Returns the response when the policy says RETURN, or
    raises the last exception when the policy gives up.  Both extract_one and
    extract_conversation route through this so that auth failures trigger a re-auth
    and a retry rather than propagating immediately.

    now is always time.time() (wall clock), never time.monotonic().
    PTS_LLM_DEADLINE is wall-clock arithmetic; a monotonic now (~1e5) against an
    epoch deadline (~1.7e9) would make the budget check permanently false, silently
    disabling the whole deadline mechanism.
    """
    # Local import: policy_bridge imports reset_client_cache from this module, so a
    # top-level import would create a circular dependency.  By the time this function
    # runs, claude_extract is fully initialised and the deferred import resolves cleanly.
    from src.extract.policy_bridge import classify_exception

    deadline = resolve_deadline(time.time(), os.environ)
    attempt = Attempt()
    last_exc: BaseException | None = None
    last_response: object = None
    is_linux = running_on_linux()

    while True:
        last_exc = None
        last_response = None
        try:
            last_response = fn()
        except Exception as exc:
            last_exc = exc

        outcome = classify_exception(last_exc, last_response)
        attempt = attempt.bump(outcome)
        decision = decide(
            outcome,
            attempt,
            now=time.time(),  # wall clock: PTS_LLM_DEADLINE is epoch-based
            deadline=deadline,
            max_call_seconds=max_call_seconds,
            is_linux=is_linux,
        )

        if decision.action is Action.RETURN:
            return last_response

        if decision.action in (Action.REAUTH_RETRY, Action.WAIT_FOR_PUSH):
            result = _reauth_unless_latched(is_linux=is_linux)
            if result is None:
                # Latched: another worker already waited the full push window and it
                # did not arrive. Fail this item now instead of spending the same wait
                # again — the run's remaining budget belongs to items that can still
                # succeed. No with_reauth_used() here: nothing was spent.
                break
            if result is not ReauthResult.SKIPPED:
                attempt = attempt.with_reauth_used()
            continue

        if decision.action is Action.PLAIN_RETRY:
            if decision.sleep_s > 0:
                time.sleep(decision.sleep_s)
            continue

        # GIVE_UP or UNRECOVERABLE_AUTH: stop looping
        break

    if last_exc is not None:
        raise last_exc
    return last_response


def extract_one(email: dict) -> dict | None:
    """Extract structured data from a single email using Claude."""
    sys.path.insert(0, str(REPO_ROOT))
    from src.extract.parser import parse_extraction
    from src.extract.prompt import build_extraction_prompt
    from src.extract.vertex_fallback import create_with_refusal_fallback

    prompt = build_extraction_prompt(email)

    # _do_call calls _get_client_and_model() on each attempt so that a successful
    # reauth (which calls reset_client_cache) is picked up on the retry rather than
    # silently reusing the stale in-memory credential.  Do not close the returned
    # client — it is the shared, long-lived instance (see _get_client_and_model).
    def _do_call():
        current_client, current_model = _get_client_and_model()
        return create_with_refusal_fallback(
            current_client,
            model=current_model,
            max_tokens=MAX_OUTPUT_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )

    response = call_with_policy(_do_call, max_call_seconds=120.0)

    text = _response_text(response)
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1])

    # Surface truncation as a structured error so retry/diagnostics see why JSON broke.
    if response.stop_reason == "max_tokens":
        raise ValueError(f"LLM response truncated at max_tokens={response.usage.output_tokens}")

    extraction = parse_extraction(text)
    extraction["message_id"] = email["message_id"]
    return extraction


def extract_conversation(conversation: dict) -> dict | None:
    """Extract structured data from a conversation using Claude."""
    sys.path.insert(0, str(REPO_ROOT))
    from src.extract.parser import CONVERSATION_SENTIMENT_VALUES, parse_extraction
    from src.extract.prompt import build_conversation_extraction_prompt
    from src.extract.vertex_fallback import create_with_refusal_fallback

    prompt = build_conversation_extraction_prompt(conversation)

    # _do_call calls _get_client_and_model() on each attempt so that a successful
    # reauth (which calls reset_client_cache) is picked up on the retry rather than
    # silently reusing the stale in-memory credential.  Do not close the returned
    # client — it is the shared, long-lived instance (see _get_client_and_model).
    def _do_call():
        current_client, current_model = _get_client_and_model()
        return create_with_refusal_fallback(
            current_client,
            model=current_model,
            max_tokens=MAX_OUTPUT_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )

    response = call_with_policy(_do_call, max_call_seconds=120.0)

    text = _response_text(response)
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1])

    extraction = parse_extraction(
        text,
        sentiment_values=CONVERSATION_SENTIMENT_VALUES,
        sentiment_default="exploratory",
    )
    extraction["session_id"] = conversation["session_id"]
    return extraction
