"""
Parse and validate LLM JSON responses.

Handles malformed JSON, validates schema, and provides defaults for missing fields.
"""

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# Schema definition with defaults
EXTRACTION_SCHEMA = {
    "summary": str,
    "topics": list,
    "decisions": list,
    "action_items": list,
    "commitments": list,
    "people_roles": dict,
    "sentiment": str,
    "urgency": str,
    "language": str,
    "key_facts": list,
    "references": list,
}

SENTIMENT_VALUES = {
    "directive",
    "collaborative",
    "informational",
    "escalation",
    "celebratory",
}
# Conversation (Claude Code session) sentiment vocabulary — a coding-session tone
# is different from email tone. Must match the values offered in the conversation
# prompt (build_conversation_extraction_prompt); otherwise every value gets coerced.
CONVERSATION_SENTIMENT_VALUES = {
    "productive",
    "exploratory",
    "debugging",
    "planning",
    "reviewing",
}
URGENCY_VALUES = {"low", "medium", "high", "critical"}
LANGUAGE_VALUES = {"greek", "english", "mixed"}


# Fields the loader stores as text, one row or tag per item. The conversation
# prompt's two extra fields are among them: parse_extraction copies them through
# with every other key the schema does not name.
STRING_LIST_FIELDS = (
    "topics",
    "key_facts",
    "references",
    "preferences_expressed",
    "technical_decisions",
)


def _as_string_list(value: Any) -> list[str]:
    """`value` as the list of strings the loader expects.

    The model does not always answer in that shape. 35 conversation extractions
    gave technical_decisions as [{"decision": ...}], and the loader, which writes
    each item as f"[TECHNICAL] {item}", stored the dict's Python repr as a key
    fact. A null reached the loader as None to iterate, which raises and stops
    every conversation load, and a bare string would be iterated one character
    at a time. So: null is an empty list, a bare string or dict is one item, a
    dict item is its 'decision', its 'text' or its first string value, and any
    other item is dropped.
    """
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out: list[str] = []
    for item in items:
        if isinstance(item, dict):
            text = item.get("decision") or item.get("text")
            if not isinstance(text, str):
                text = next((v for v in item.values() if isinstance(v, str)), None)
            item = text
        if isinstance(item, str):
            out.append(item)
    return out


def _strip_to_object(raw: str) -> str:
    """The reply without markdown fences or any text around its JSON object."""
    # Remove markdown code blocks
    raw = re.sub(r"```json\s*", "", raw)
    raw = re.sub(r"```\s*$", "", raw)
    raw = raw.strip()

    # Try to find JSON object bounds if there's extra text
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        raw = raw[start : end + 1]
    return raw


def _clean_json_string(raw: str) -> str:
    """
    Clean common JSON formatting issues.

    Only for a reply that strict parsing rejected. The repairs edit the text
    without regard to string literals, and in valid JSON a comma before a
    closing bracket can only be inside a string, so run over a valid reply they
    deleted that comma from its content.

    Args:
        raw: Raw string that may contain JSON

    Returns:
        Cleaned JSON string
    """
    raw = _strip_to_object(raw)

    # Fix trailing commas before closing braces/brackets
    raw = re.sub(r",\s*}", "}", raw)
    raw = re.sub(r",\s*]", "]", raw)

    # Fix missing commas between JSON elements (common LLM output error)
    raw = re.sub(r"([\"\}\]])\s*\n\s*([\"\{\[])", r"\1,\n\2", raw)

    return raw


def _salvage_truncated_json(raw: str) -> str | None:
    """Best-effort repair of JSON truncated mid-structure.

    When the LLM hits max_tokens the response stops mid-object/array/string, so
    strict parsing fails ("Expecting ',' delimiter"). Close a dangling string
    and any open containers, dropping a trailing incomplete key/element, so we
    salvage the summary + whatever fields completed instead of losing them all.

    Returns a candidate string (the caller must still json.loads it — the
    candidate is only used if it parses, so a bad guess never yields garbage),
    or None if the input is bracket-balanced (i.e. truncation isn't the cause).
    """
    stack: list[str] = []
    in_str = False
    escaped = False
    for ch in raw:
        if escaped:
            escaped = False
            continue
        if in_str:
            if ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()

    if not stack and not in_str:
        return None  # brackets balanced — the failure isn't a truncation

    s = raw
    if in_str:
        s += '"'  # close the dangling string (value or key)
    s = s.rstrip()
    # A dangling key with no value ('..., "owner":') — drop the key and its colon.
    if s.endswith(":"):
        s = s[:-1].rstrip()
        s = re.sub(r',?\s*"(?:[^"\\]|\\.)*"\s*$', "", s).rstrip()
    # Drop a trailing comma before closing the open containers.
    s = s.rstrip(",").rstrip()
    return s + "".join(reversed(stack))


def parse_extraction(
    raw_response: str,
    sentiment_values: set[str] | None = None,
    sentiment_default: str = "informational",
) -> dict[str, Any]:
    """
    Parse LLM response into extraction dict.

    Handles malformed JSON and applies defaults for missing fields.

    Args:
        raw_response: Raw LLM response (may contain markdown, extra text, etc.)
        sentiment_values: Allowed sentiment vocabulary. Defaults to the email set
            (SENTIMENT_VALUES); pass CONVERSATION_SENTIMENT_VALUES for conversations.
        sentiment_default: Value to coerce an out-of-vocabulary sentiment to.

    Returns:
        Validated extraction dict with all required fields

    Raises:
        ValueError: If JSON is completely unparseable
    """
    # Strictly first: the repairs in _clean_json_string can only damage a reply
    # that is already valid.
    try:
        data = json.loads(_strip_to_object(raw_response))
    except json.JSONDecodeError:
        # Clean the response
        cleaned = _clean_json_string(raw_response)

        # Try to parse
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            # Truncated response (LLM hit max_tokens): salvage a valid prefix by closing
            # the open string/containers. Only accepted if it parses, so a bad guess never
            # yields garbage.
            #
            # We deliberately do NOT globally replace ' -> " as a fallback: that corrupts
            # apostrophes inside string values (Greek possessives, English contractions)
            # and can silently store mangled data. A rare single-quoted-JSON response now
            # fails visibly (logged + retried) instead of being corrupted in place.
            salvaged = _salvage_truncated_json(cleaned)
            if salvaged is not None:
                try:
                    data = json.loads(salvaged)
                    logger.warning("Recovered truncated JSON via salvage (%d chars)", len(cleaned))
                except json.JSONDecodeError:
                    raise ValueError(f"Failed to parse JSON: {e}") from e
            else:
                raise ValueError(f"Failed to parse JSON: {e}") from e

    # Ensure it's a dict
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object, got {type(data).__name__}")

    # Apply defaults for missing fields.
    #
    # `or default`, not `get(key, default)`. The two differ exactly when the key
    # is PRESENT and null, which the prompt actively teaches the model is legal
    # ("deadline": "YYYY-MM-DD or null"), and dict.get then returns None: the
    # enum normalisation below did None.lower() and raised AttributeError, while
    # a null list reached the loader as None to be iterated. On the email path
    # that is a permanent poison item, since the same input fails identically on
    # every retry.
    result = {
        "summary": data.get("summary") or "",
        "topics": data.get("topics") or [],
        "decisions": data.get("decisions") or [],
        "action_items": data.get("action_items") or [],
        "commitments": data.get("commitments") or [],
        "people_roles": data.get("people_roles") or {},
        "sentiment": data.get("sentiment") or "informational",
        "urgency": data.get("urgency") or "low",
        "language": data.get("language") or "english",
        "key_facts": data.get("key_facts") or [],
        "references": data.get("references") or [],
    }

    # Preserve extra fields (e.g. conversation-specific: preferences_expressed, technical_decisions)
    for key, value in data.items():
        if key not in result:
            result[key] = value

    # Only the fields present: an email extraction gains no conversation fields.
    for key in STRING_LIST_FIELDS:
        if key in result:
            result[key] = _as_string_list(result[key])

    # Normalize enums to lowercase. str() so a model that answers with a number
    # or a bool gets validated below rather than raising here.
    result["sentiment"] = str(result["sentiment"]).lower()
    result["urgency"] = str(result["urgency"]).lower()
    result["language"] = str(result["language"]).lower()

    # Validate against schema — log issues but don't reject (non-breaking)
    sentiment_values = sentiment_values or SENTIMENT_VALUES
    is_valid, issues = validate_extraction(result, sentiment_values=sentiment_values)
    if not is_valid:
        logger.warning(
            f"LLM extraction has {len(issues)} validation issue(s): " + "; ".join(issues[:5])
        )
        # Coerce invalid enum values to defaults
        if result["sentiment"] not in sentiment_values:
            logger.warning(
                f"Coercing invalid sentiment '{result['sentiment']}' → '{sentiment_default}'"
            )
            result["sentiment"] = sentiment_default
        if result["urgency"] not in URGENCY_VALUES:
            logger.warning(f"Coercing invalid urgency '{result['urgency']}' → 'low'")
            result["urgency"] = "low"
        if result["language"] not in LANGUAGE_VALUES:
            logger.warning(f"Coercing invalid language '{result['language']}' → 'english'")
            result["language"] = "english"

    return result


def validate_extraction(
    data: dict[str, Any], sentiment_values: set[str] | None = None
) -> tuple[bool, list[str]]:
    """
    Validate extraction against schema.

    Args:
        data: Extraction dict to validate
        sentiment_values: Allowed sentiment vocabulary (defaults to email SENTIMENT_VALUES)

    Returns:
        Tuple of (is_valid, list_of_issues)
    """
    sentiment_values = sentiment_values or SENTIMENT_VALUES
    issues = []

    # Check required fields are present
    for field, expected_type in EXTRACTION_SCHEMA.items():
        if field not in data:
            issues.append(f"Missing required field: {field}")
            continue

        # Check type
        value = data[field]
        if not isinstance(value, expected_type):
            issues.append(
                f"Field '{field}' has wrong type: expected {expected_type.__name__}, got {type(value).__name__}"
            )

    # Validate enum values
    if "sentiment" in data and data["sentiment"] not in sentiment_values:
        issues.append(
            f"Invalid sentiment value: {data['sentiment']}. Must be one of {sentiment_values}"
        )

    if "urgency" in data and data["urgency"] not in URGENCY_VALUES:
        issues.append(f"Invalid urgency value: {data['urgency']}. Must be one of {URGENCY_VALUES}")

    if "language" in data and data["language"] not in LANGUAGE_VALUES:
        issues.append(
            f"Invalid language value: {data['language']}. Must be one of {LANGUAGE_VALUES}"
        )

    # Validate nested structures
    if "decisions" in data and isinstance(data["decisions"], list):
        for i, decision in enumerate(data["decisions"]):
            if not isinstance(decision, dict):
                issues.append(f"decisions[{i}] is not a dict")
                continue
            if "decision" not in decision:
                issues.append(f"decisions[{i}] missing 'decision' field")
            if "decided_by" not in decision:
                issues.append(f"decisions[{i}] missing 'decided_by' field")

    if "action_items" in data and isinstance(data["action_items"], list):
        for i, item in enumerate(data["action_items"]):
            if not isinstance(item, dict):
                issues.append(f"action_items[{i}] is not a dict")
                continue
            if "task" not in item:
                issues.append(f"action_items[{i}] missing 'task' field")
            if "owner" not in item:
                issues.append(f"action_items[{i}] missing 'owner' field")
            if "deadline" not in item:
                issues.append(f"action_items[{i}] missing 'deadline' field")

    if "commitments" in data and isinstance(data["commitments"], list):
        for i, comm in enumerate(data["commitments"]):
            if not isinstance(comm, dict):
                issues.append(f"commitments[{i}] is not a dict")
                continue
            if "commitment" not in comm:
                issues.append(f"commitments[{i}] missing 'commitment' field")
            if "by" not in comm:
                issues.append(f"commitments[{i}] missing 'by' field")
            if "to" not in comm:
                issues.append(f"commitments[{i}] missing 'to' field")

    return len(issues) == 0, issues
