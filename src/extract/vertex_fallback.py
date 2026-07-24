"""Auto-downgrade a Vertex AI Claude call when the model raises a policy refusal.

Opus 4.7/4.8 occasionally raise a spurious `stop_reason == "refusal"` on benign work.
Opus 4.6 does not. This helper retries such a call ONCE on the Opus 4.6 / europe-west1
fallback tier — model AND region together, since a <=4.6 model lives in europe-west1
(opus-4-6 @ eu would 429). Shared by every second-brain SDK extraction call site.
"""

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def create_with_refusal_fallback(client: Any, *, model: str, **create_kwargs: Any) -> Any:
    """Call ``client.messages.create(model=model, **create_kwargs)``.

    If the response stop_reason is ``"refusal"``, retry once on a fresh AnthropicVertex
    client pinned to the Opus 4.6 / europe-west1 fallback tier and return that response.
    On any non-refusal stop_reason (including ``max_tokens``), return the first response
    unchanged so existing callers keep their own handling.
    """
    response = client.messages.create(model=model, **create_kwargs)
    if getattr(response, "stop_reason", None) != "refusal":
        return response

    fb_model = os.environ.get("VERTEX_MODEL_FALLBACK_SDK", "claude-opus-4-6")
    fb_region = os.environ.get("VERTEX_REGION_FALLBACK", "europe-west1")
    project = os.environ.get("VERTEX_SDK_PROJECT") or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
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
