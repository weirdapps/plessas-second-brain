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


def _clean_json_string(raw: str) -> str:
    """
    Clean common JSON formatting issues.

    Args:
        raw: Raw string that may contain JSON

    Returns:
        Cleaned JSON string
    """
    # Remove markdown code blocks
    raw = re.sub(r"```json\s*", "", raw)
    raw = re.sub(r"```\s*$", "", raw)
    raw = raw.strip()

    # Try to find JSON object bounds if there's extra text
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        raw = raw[start : end + 1]

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

    # Apply defaults for missing fields
    result = {
        "summary": data.get("summary", ""),
        "topics": data.get("topics", []),
        "decisions": data.get("decisions", []),
        "action_items": data.get("action_items", []),
        "commitments": data.get("commitments", []),
        "people_roles": data.get("people_roles", {}),
        "sentiment": data.get("sentiment", "informational"),
        "urgency": data.get("urgency", "low"),
        "language": data.get("language", "english"),
        "key_facts": data.get("key_facts", []),
        "references": data.get("references", []),
    }

    # Preserve extra fields (e.g. conversation-specific: preferences_expressed, technical_decisions)
    for key, value in data.items():
        if key not in result:
            result[key] = value

    # Normalize enums to lowercase
    result["sentiment"] = result["sentiment"].lower()
    result["urgency"] = result["urgency"].lower()
    result["language"] = result["language"].lower()

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
