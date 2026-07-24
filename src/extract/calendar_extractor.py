"""Calendar event extraction via LLM."""

import json


def parse_extraction_response(raw: str) -> dict:
    """
    Parse the LLM's JSON response.

    Defensive parsing:
    - Strip markdown code fences if present (```json ... ```)
    - Parse as JSON
    - Return dict with keys: body_summary (str), decisions (list), action_items (list)
    - On any parse failure: return {"body_summary": "", "decisions": [], "action_items": []}

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

    # Try to parse as JSON
    try:
        result = json.loads(cleaned)

        # Ensure required keys exist with defaults
        return {
            "body_summary": result.get("body_summary", ""),
            "decisions": result.get("decisions", []),
            "action_items": result.get("action_items", []),
        }
    except (json.JSONDecodeError, ValueError):
        # Return empty defaults on any parse failure
        return {"body_summary": "", "decisions": [], "action_items": []}


def extract_event(event: dict, body: str | None = None) -> dict:
    """
    Run LLM extraction on a calendar event.

    Args:
        event: Event dict with subject, organizer, attendees, start_at
        body: Optional event body/description

    Returns:
        dict with body_summary, decisions, action_items
    """
    # If body is None or too short, return empty result (no LLM call)
    if body is None or len(body.strip()) < 50:
        return {"body_summary": "", "decisions": [], "action_items": []}

    # Truncate body to 4000 chars
    truncated_body = body[:4000]

    # Extract event metadata
    subject = event.get("subject", "")
    organizer = event.get("organizer", "")
    attendees = ", ".join(
        a.get("name", a.get("email", "")) if isinstance(a, dict) else str(a)
        for a in event.get("attendees", [])
    )
    start_at = event.get("start_at", "")

    # Build prompt
    prompt = f"""You are analyzing a calendar event from a corporate email system.

Event subject: {subject}
Organizer: {organizer}
Attendees: {attendees}
Date: {start_at}

Event body/description:
{truncated_body}

Extract the following as JSON:
{{
  "body_summary": "1-2 sentence summary of what this meeting was about",
  "decisions": [
    {{"decision": "what was decided", "decided_by": "who decided it", "decision_date": "{start_at}"}}
  ],
  "action_items": [
    {{"task": "what needs to be done", "owner": "who owns it", "deadline": "when, if mentioned"}}
  ]
}}

If the body is empty or contains only a Teams link with no agenda, return empty summary and empty arrays.
Respond with ONLY the JSON object, no other text."""

    # Call LLM — reuse the existing Vertex/API client factory
    from src.extract.claude_extract import _get_client_and_model

    client, model = _get_client_and_model()

    response = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    # Extract text from response
    raw_text = response.content[0].text

    # Parse response
    return parse_extraction_response(raw_text)
