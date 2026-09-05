"""Auto-downgrade a Vertex AI Claude call when the model raises a policy refusal.

Some Opus versions occasionally raise a spurious `stop_reason == "refusal"` on benign
work. This helper retries such a call ONCE on the VERTEX_MODEL_FALLBACK_SDK /
VERTEX_REGION_FALLBACK tier — model AND region together, since region is a function of
model version (>=4.7 -> eu, <=4.6 -> europe-west1; a mismatched pair 429s). Shared by
every second-brain SDK extraction call site.
"""

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def create_with_refusal_fallback(client: Any, *, model: str, **create_kwargs: Any) -> Any:
    """Call ``client.messages.create(model=model, **create_kwargs)``.

    If the response stop_reason is ``"refusal"``, retry once on a fresh AnthropicVertex
    client pinned to the configured fallback tier and return that response.
    On any non-refusal stop_reason (including ``max_tokens``), return the first response
    unchanged so existing callers keep their own handling.
    """
    response = client.messages.create(model=model, **create_kwargs)
    if getattr(response, "stop_reason", None) != "refusal":
        return response

    fb_model = os.environ.get("VERTEX_MODEL_FALLBACK_SDK", "claude-opus-5")
    fb_region = os.environ.get("VERTEX_REGION_FALLBACK", "eu")
    project = os.environ.get("VERTEX_SDK_PROJECT") or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")

    # The fallback tier is allowed to equal the primary, and on the VPS it does:
    # VERTEX_MODEL_EXTRACT and VERTEX_MODEL_FALLBACK_SDK are both claude-opus-5
    # in region eu, because the fallback stopped being a model-class escape
    # hatch on 2026-08-03. A refusal is a property of the model and the prompt,
    # so replaying the identical pair cannot change stop_reason: it only spends a
    # second call, and up to another max_call_seconds of the caller's deadline,
    # to be told the same thing. sb-outlook-sync logged 7,124 of these.
    #
    # Only when the region is KNOWN to match. A client that does not expose
    # `.region` leaves the pair unproven, and dropping a retry on an unproven
    # match would trade a cheap wasted call for a silently lost recovery.
    primary_region = getattr(client, "region", None)
    if fb_model == model and primary_region is not None and fb_region == primary_region:
        logger.warning(
            "Vertex policy refusal on %s @ %s; fallback tier is the same pair, not retrying",
            model,
            primary_region,
        )
        return response

    logger.warning(
        "Vertex policy refusal on %s — downgrading to fallback %s @ %s",
        model,
        fb_model,
        fb_region,
    )
    if not project:
        # Direct-API path (ANTHROPIC_API_KEY): no Vertex project to build a fallback on.
        logger.error("No Vertex project available for fallback; returning the refusal.")
        return response

    from anthropic import AnthropicVertex

    fb_client = AnthropicVertex(project_id=project, region=fb_region, timeout=120.0)
    try:
        return fb_client.messages.create(model=fb_model, **create_kwargs)
    finally:
        fb_client.close()
