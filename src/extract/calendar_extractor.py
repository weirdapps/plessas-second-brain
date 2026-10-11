"""Calendar event extraction via LLM."""

import hashlib
import json

from src.extract.claude_extract import _response_text, complete
from src.extract.untrusted import fence
from src.redact import PROMPT_RULE, redact_secrets


def parse_extraction_response(raw: str) -> dict:
    """
    Parse the LLM's JSON response.

    Defensive parsing:
    - Strip markdown code fences if present (```json ... ```)
    - Parse as JSON; failing that, the object between the first "{" and the
      last "}" (a "Here is the JSON:" preamble is the commonest near-miss)
    - Return dict with keys: body_summary (str), decisions (list), action_items (list)

    Raises ValueError when no JSON object can be recovered. It used to return
    empty defaults, which calendar-sync stored as a finished 'extracted' row
    with an empty summary, and now that an extraction REPLACES an event's
    decisions and actions it would also have deleted them. Raising lets the
    caller record 'failed' and keep what it had.

    Args:
        raw: Raw LLM response text

    Returns:
        dict with body_summary, decisions, action_items
    """
    # Strip markdown code fences if present
    cleaned = raw.strip()

    # Remove ```json ... ``` or ``` ... ```
    if cleaned.startswith("```"):
        # Find first newline after opening fence
        first_newline = cleaned.find("\n")
        if first_newline != -1:
            # Find closing fence
            closing_fence = cleaned.rfind("```")
            if closing_fence > first_newline:
                cleaned = cleaned[first_newline + 1 : closing_fence].strip()

    start, end = cleaned.find("{"), cleaned.rfind("}")
    candidates = [cleaned]
    if start != -1 and end > start:
        candidates.append(cleaned[start : end + 1])
    for candidate in candidates:
        try:
            result = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(result, dict):
            # Ensure required keys exist with defaults
            return {
                "body_summary": result.get("body_summary", ""),
                "decisions": result.get("decisions", []),
                "action_items": result.get("action_items", []),
            }
    raise ValueError(f"calendar extraction response is not JSON: {raw[:80]!r}")


# The prompt around the invite. Its text is part of event_prompt_hash, so an edit
# here re-extracts every event once, as a changed prompt should.
_PROMPT_TEMPLATE = (
    """You are analyzing a calendar event from a corporate email system.

{invite}

Extract the following as JSON:
{{
  "body_summary": "1-2 sentence summary of what this meeting was about",
  "decisions": [
    {{"decision": "what was decided", "decided_by": "who decided it", "decision_date": null}}
  ],
  "action_items": [
    {{"task": "what needs to be done", "owner": "who owns it", "deadline": "when, if mentioned"}}
  ]
}}

Set decision_date to the date the text states for the decision, as YYYY-MM-DD, or to JSON null when it states none.
If the body is empty or contains only a Teams link with no agenda, return empty summary and empty arrays.
"""
    + PROMPT_RULE
    + """
Respond with ONLY the JSON object, no other text."""
)


def event_prompt_hash(event: dict, body: str | None) -> str | None:
    """sha256 of what the model would read for this event: the invite and the prompt
    around it. The fence tag is random per call, so it is left out. None when the
    body is too short to extract.

    calendar-sync stores it with an extraction. Outlook moves an event's etag on
    edits the model never sees (an attendee answering, a room change), and the same
    hash then means the extraction on record still stands."""
    invite = _invite_text(event, body)
    if invite is None:
        return None
    return hashlib.sha256(f"{_PROMPT_TEMPLATE}\x00{invite}".encode()).hexdigest()


def extract_event(event: dict, body: str | None = None) -> dict:
    """
    Run LLM extraction on a calendar event.

    Args:
        event: Event dict with subject, organizer, attendees, start_at
        body: Optional event body/description

    Returns:
        dict with body_summary, decisions, action_items
    """
    invite = _invite_text(event, body)
    # If body is None or too short, return empty result (no LLM call)
    if invite is None:
        return {"body_summary": "", "decisions": [], "action_items": []}

    # Whoever sent the invite wrote all of it, the subject included.
    prompt = _PROMPT_TEMPLATE.format(invite=fence(invite))

    response = complete(max_tokens=1024, messages=[{"role": "user", "content": prompt}])

    # First text block, never content[0]: extended thinking puts a ThinkingBlock there.
    # 86 events failed this way between 2026-08-12 and 2026-08-24 (calendar-sync.log),
    # on the same days as successful extractions, so the shape is intermittent rather
    # than constant. Unlike attachments there is no llm_error column here, so the
    # failure was visible only in the log.
    raw_text = _response_text(response)

    # Parse response
    return parse_extraction_response(raw_text)


def _invite_text(event: dict, body: str | None) -> str | None:
    """The invite as the prompt shows it, or None when the body is too short."""
    if body is None or len(body.strip()) < 50:
        return None

    # Redact credentials, then truncate to 4000 chars. Redacting first means a
    # key straddling the cut cannot survive as an unrecognisable fragment.
    truncated_body = redact_secrets(body)[:4000]

    # Extract event metadata
    subject = event.get("subject", "")
    organizer = event.get("organizer", "")
    # `or`, not a get() default: Graph sends "name": null, and get() returns the
    # stored None rather than the default, which str.join rejects.
    attendees = ", ".join(
        str(a.get("name") or a.get("email") or "") if isinstance(a, dict) else str(a)
        for a in event.get("attendees", [])
    )
    start_at = event.get("start_at", "")

    return f"""Event subject: {subject}
Organizer: {organizer}
Attendees: {attendees}
Date: {start_at}

Event body/description:
{truncated_body}"""
