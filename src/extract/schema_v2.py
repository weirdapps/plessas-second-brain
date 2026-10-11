"""Schema v2 of the extraction JSON: what call shape v2 asks the model for (issue 163).

Structured outputs (`output_config.format`) hold the model to a JSON schema, under limits
that shaped this one:

- every object must set `additionalProperties: false`, so v1's `people_roles`, an object
  keyed by person name, cannot be expressed; it is the `people` array here;
- no string or array length limits are accepted, so the caps on list sizes and quote
  lengths are in the prompt;
- a request may carry at most 24 optional and 16 union-typed parameters, so every property
  is required, an empty string or list standing for "none", and the one union is an
  action's deadline, a date or null;
- an enum value can come back in any capitalisation, so the parser compares them folded.

Properties are written in the order the schema lists them, so an item's `evidence`, the
verbatim passage that states it, comes before the item: the quote is written first and
the paraphrase follows from it. The vocabularies are the extraction eval's labels
(src/evals/extraction.py imports them from here), so a v2 reply is graded against the
owner's gold field for field.
"""

import re
from datetime import datetime
from typing import Any

# What kind of item it is. Bulk mail and notifications have nothing to decide or do.
ITEM_KINDS = (
    "work_thread",
    "fyi_forward",
    "notification",
    "bulk",
    "invite_logistics",
    "regulatory_text",
    "personal",
    "other",
)
# ISO 639-1, as the Teams and WhatsApp extractions already write it, plus two words.
LANGUAGES = ("el", "en", "mixed", "other")
OWNER_ROLES = (
    "owner_decided",
    "assigned_to_owner",
    "owner_assigned_other",
    "owner_fyi",
    "third_party",
)
DECISION_STATUSES = ("made", "proposed", "requested")
DIRECTIONS = ("owner_owes", "owed_to_owner", "third_party")
# The email vocabularies of schema v1 (src/extract/parser.py): emails.sentiment and
# emails.urgency hold them, and the person and sender dossiers count them.
SENTIMENTS = ("directive", "collaborative", "informational", "escalation", "celebratory")
URGENCIES = ("low", "medium", "high", "critical")

# What an enum outside its vocabulary becomes. Structured outputs keep a reply inside
# it; these apply only to a reply that was not held to the schema.
ENUM_DEFAULTS = {
    "item_kind": "other",
    "language": "other",
    "sentiment": "informational",
    "urgency": "low",
    "owner_role": "third_party",
    "status": "made",
    "direction": "third_party",
}

# The text of each list's items: an item without it is dropped.
ITEM_TEXT = {
    "decisions": "decision",
    "action_items": "task",
    "commitments": "commitment",
    "people": "name_as_written",
}
# The lists whose items carry an evidence quote.
EVIDENCED = ("decisions", "action_items", "commitments")


def _object(properties: dict[str, dict]) -> dict:
    """An object whose every property is required and that allows no other."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def _enum(values: tuple[str, ...]) -> dict:
    return {"type": "string", "enum": list(values)}


def _array(items: dict) -> dict:
    return {"type": "array", "items": items}


_TEXT: dict = {"type": "string"}
_TEXTS = _array(_TEXT)
_DATE_OR_NULL = {"anyOf": [{"type": "string", "format": "date"}, {"type": "null"}]}

DECISION = _object(
    {
        "evidence": _TEXT,
        "decision": _TEXT,
        "decided_by": _TEXT,
        "owner_role": _enum(OWNER_ROLES),
        "status": _enum(DECISION_STATUSES),
    }
)
ACTION = _object(
    {
        "evidence": _TEXT,
        "task": _TEXT,
        "owner": _TEXT,
        "direction": _enum(DIRECTIONS),
        "deadline": _DATE_OR_NULL,
    }
)
COMMITMENT = _object({"evidence": _TEXT, "commitment": _TEXT, "by": _TEXT, "to": _TEXT})
PERSON = _object({"name_as_written": _TEXT, "name_latin": _TEXT, "email": _TEXT, "role": _TEXT})

EMAIL_SCHEMA_V2 = _object(
    {
        "item_kind": _enum(ITEM_KINDS),
        "language": _enum(LANGUAGES),
        "summary": _TEXT,
        "topics": _TEXTS,
        "sentiment": _enum(SENTIMENTS),
        "urgency": _enum(URGENCIES),
        "decisions": _array(DECISION),
        "action_items": _array(ACTION),
        "commitments": _array(COMMITMENT),
        "key_facts": _TEXTS,
        "people": _array(PERSON),
    }
)
# An attachment's extraction: the fields Phase 2 stores (attachment_content.summary and
# language, and the email's topics, decisions, action items and key facts).
ATTACHMENT_SCHEMA_V2 = _object(
    {
        "language": _enum(LANGUAGES),
        "summary": _TEXT,
        "topics": _TEXTS,
        "decisions": _array(DECISION),
        "action_items": _array(ACTION),
        "key_facts": _TEXTS,
    }
)
SCHEMAS = {"email": EMAIL_SCHEMA_V2, "attachment": ATTACHMENT_SCHEMA_V2}

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def is_date(value: Any) -> bool:
    """A YYYY-MM-DD string naming a real day."""
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def problems(value: Any, schema: dict, where: str = "$") -> list[str]:
    """What keeps `value` from matching `schema`, by path; empty when it matches.

    Covers the part of JSON Schema the schemas above use. A message names the path and
    what the field may hold, never the value found there: extractions hold mail.
    """
    if "anyOf" in schema:
        if any(not problems(value, option, where) for option in schema["anyOf"]):
            return []
        return [f"{where} matches none of its forms"]
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            return [f"{where} is not an object"]
        out = [f"{where}.{key} is missing" for key in schema["required"] if key not in value]
        if schema.get("additionalProperties") is False:
            out += [
                f"{where} has a property the schema does not name"
                for key in value
                if key not in schema["properties"]
            ]
        for key, sub in schema["properties"].items():
            if key in value:
                out += problems(value[key], sub, f"{where}.{key}")
        return out
    if kind == "array":
        if not isinstance(value, list):
            return [f"{where} is not an array"]
        return [
            p
            for n, item in enumerate(value)
            for p in problems(item, schema["items"], f"{where}[{n}]")
        ]
    if kind == "string":
        if not isinstance(value, str):
            return [f"{where} is not a string"]
        if "enum" in schema and value not in schema["enum"]:
            return [f"{where} is not one of {', '.join(schema['enum'])}"]
        if schema.get("format") == "date" and not is_date(value):
            return [f"{where} is not a YYYY-MM-DD date"]
        return []
    if kind == "null":
        return [] if value is None else [f"{where} is not null"]
    return [f"{where} has a type this validator does not know"]
