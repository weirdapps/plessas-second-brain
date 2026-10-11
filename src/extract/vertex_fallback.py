"""Auto-downgrade a Vertex AI Claude call when the model raises a policy refusal.

Some Opus versions occasionally raise a spurious `stop_reason == "refusal"` on benign
work. This helper retries such a call ONCE on the VERTEX_MODEL_FALLBACK_SDK /
VERTEX_REGION_FALLBACK tier — model AND region together, since region is a function of
model version (>=4.7 -> eu, <=4.6 -> europe-west1; a mismatched pair 429s). The tier is
optional and has no default: unset, a refusal comes back as it is. Shared by every
second-brain SDK extraction call site.
"""

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def create_with_refusal_fallback(
    client: Any, *, model: str, after_refusal: dict | None = None, **create_kwargs: Any
) -> Any:
    """Call ``client.messages.create(model=model, **create_kwargs)``.

    If the response stop_reason is ``"refusal"``, retry once on a fresh AnthropicVertex
    client pinned to the configured fallback tier and return that response.
    On any non-refusal stop_reason (including ``max_tokens``), return the first response
    unchanged so existing callers keep their own handling.

    ``after_refusal`` is a dict the caller keeps across its own retries. It records
    the fallback tier once the primary has refused, so a retry after an error on
    that tier (a 429, a dropped connection) goes to the tier alone: the primary's
    answer cannot change, and replaying it would cost a billed call and an attempt.
    """
    if after_refusal and "tier" in after_refusal:
        project, fb_model, fb_region = after_refusal["tier"]
        return _on_fallback_tier(project, fb_model, fb_region, create_kwargs)
    response = client.messages.create(model=model, **create_kwargs)
    if getattr(response, "stop_reason", None) != "refusal":
        return response

    # No default for either. The tier is a model AND its region, and the defaults this
    # carried (an Opus 4.6 model in europe-west1) came to be a pair the allowlist
    # rejects, so a refusal would have been answered with an error. Unset, or half set,
    # there is no second call (below) and the refusal comes back as it is.
    fb_model = os.environ.get("VERTEX_MODEL_FALLBACK_SDK", "").strip()
    fb_region = os.environ.get("VERTEX_REGION_FALLBACK", "").strip()
    project = os.environ.get("VERTEX_SDK_PROJECT") or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")

    # The fallback tier is ALLOWED to equal the primary, and it does wherever the
    # allowlist holds one model: from 2026-08-03 to 2026-09-23, and again since
    # 2026-10-06, VERTEX_MODEL_EXTRACT and VERTEX_MODEL_FALLBACK_SDK name the same model
    # in the same region, so the guard below returns every refusal. In the first of
    # those periods sb-outlook-sync logged 7,124 skipped retries. A refusal is a property
    # of the model and the prompt, so replaying the identical pair cannot change
    # stop_reason: it only spends a second call, and up to another max_call_seconds of
    # the caller's deadline, to be told the same thing. Keep the guard: it is what makes
    # it safe to point the fallback at the primary.
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

    if not project:
        # The direct API (BRAIN_ALLOW_DIRECT_API=1): no Vertex project to build a fallback on.
        logger.error("No Vertex project available for fallback; returning the refusal.")
        return response
    if not (fb_model and fb_region):
        logger.warning(
            "Vertex policy refusal on %s; no fallback tier is set (VERTEX_MODEL_FALLBACK_SDK"
            " and VERTEX_REGION_FALLBACK, together), returning the refusal",
            model,
        )
        return response
    logger.warning(
        "Vertex policy refusal on %s — downgrading to fallback %s @ %s",
        model,
        fb_model,
        fb_region,
    )
    if after_refusal is not None:
        after_refusal["tier"] = (project, fb_model, fb_region)
    return _on_fallback_tier(project, fb_model, fb_region, create_kwargs)


def _on_fallback_tier(project: str, fb_model: str, fb_region: str, create_kwargs: dict) -> Any:
    from anthropic import AnthropicVertex

    # One request, as the primary client (claude_extract): the policy retries.
    fb_client = AnthropicVertex(project_id=project, region=fb_region, timeout=120.0, max_retries=0)
    try:
        return fb_client.messages.create(model=fb_model, **create_kwargs)
    finally:
        fb_client.close()
