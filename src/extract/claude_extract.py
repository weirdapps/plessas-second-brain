"""
Claude-based email extraction via Vertex AI or direct Anthropic API.

Drop-in replacement for the Gemini extractor in local.py.
"""

import os
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
# Default model — Vertex AI requires '@' separator, direct API uses '-'
CLAUDE_MODEL_BASE = os.environ.get("CLAUDE_EXTRACT_MODEL") or os.environ.get(
    "VERTEX_MODEL_EXTRACT", "claude-sonnet-4-6"
)


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


def extract_one(email: dict) -> dict | None:
    """Extract structured data from a single email using Claude."""
    sys.path.insert(0, str(REPO_ROOT))
    from src.extract.parser import parse_extraction
    from src.extract.prompt import build_extraction_prompt
    from src.extract.vertex_fallback import create_with_refusal_fallback

    # Shared client — do not close it here (see _get_client_and_model).
    client, model = _get_client_and_model()
    prompt = build_extraction_prompt(email)

    response = create_with_refusal_fallback(
        client,
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text
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

    # Shared client — do not close it here (see _get_client_and_model).
    client, model = _get_client_and_model()
    prompt = build_conversation_extraction_prompt(conversation)

    response = create_with_refusal_fallback(
        client,
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text
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
