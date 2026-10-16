"""Calendar event extraction via LLM.

An event's text is its invitation: Outlook keeps the organiser's description, and
notes reach it only when someone writes them in after the meeting. Asked every
invitation for decisions, the model turned agendas into them: one daily call's
template, paraphrased 296 times, was 460 of the 519 calendar decisions. A meeting
still to come, and any occurrence of a series, whose text is the series'
invitation and the same every time, is now read as an invitation: what it is for,
its agenda and who is expected, and nothing decided. Only a single meeting already
over is asked for decisions and action items, and only from notes or minutes of it.
"""

import hashlib
import json
import re
from datetime import UTC, datetime

from src.extract.claude_extract import _response_text, complete
from src.extract.html_text import html_to_text, looks_like_html
from src.extract.untrusted import fence
from src.redact import PROMPT_RULE, redact_secrets

# Attendee names the prompt shows; the rest are counted. Events average 48
# attendees and reach 500, and the list was most of the input: p95 11,038 tokens.
PROMPT_ATTENDEES = 30

# Under this many characters of text, once the joining details are out, there is
# nothing to read, and no call is made. It was measured on the raw HTML, which
# every body passed, so half the calls returned an empty JSON.
MIN_TEXT = 50

# How much of the text the prompt shows.
MAX_TEXT = 4000

# The joining details an online meeting adds to an invitation. Teams sets them
# between two rules of underscores, of one width, opened by a line naming the
# service (an inner rule, narrower, splits off the dial-in part); Zoom and Webex
# set labelled lines. A line is dropped only when it is joining details whole.
_RULE = re.compile(r"^\s*_{20,}\s*$")
_SERVICE = re.compile(r"microsoft teams|teams\.microsoft\.com|zoom\.us|webex", re.IGNORECASE)
_JOIN_LINE = re.compile(
    r"^\s*(?:"
    r"<?https?://\S*(?:teams\.microsoft\.com|teams\.live\.com|zoom\.us|webex\.com"
    r"|meet\.google\.com|aka\.ms/JoinTeamsMeeting)\S*>?"
    r"|(?:meeting id|passcode|password|phone conference id|video conference id|conference id"
    r"|tenant key|video id|αναγνωριστικό σύσκεψης|κωδικός πρόσβασης)\s*:.*"
    r"|(?:microsoft teams meeting|join zoom meeting|join the meeting now"
    r"|click here to join the meeting|join on your computer, mobile app or room device"
    r"|join with a video conferencing device|join on a video conferencing device"
    r"|or call in \(audio only\)|dial in by phone|dial by your location|one tap mobile"
    r"|find a local number|σύσκεψη του microsoft teams)\b.*"
    r")\s*$",
    re.IGNORECASE,
)


def _json_object(raw: str) -> dict:
    """The JSON object in a model's answer: a code fence and a preamble are
    stripped. Raises ValueError when none can be recovered."""
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
            return result
    raise ValueError(f"calendar extraction response is not JSON: {raw[:80]!r}")


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
    result = _json_object(raw)
    # Ensure required keys exist with defaults
    return {
        "body_summary": result.get("body_summary", ""),
        "decisions": result.get("decisions", []),
        "action_items": result.get("action_items", []),
    }


def _plain(value) -> str:
    return " ".join(str(value).split()).rstrip(".") if value else ""


def invitation_summary(fields: dict) -> str:
    """The summary stored for an invitation: its purpose, agenda and who is expected."""
    purpose, expected = _plain(fields.get("purpose")), _plain(fields.get("expected"))
    agenda = fields.get("agenda")
    items = [_plain(item) for item in (agenda if isinstance(agenda, list) else [agenda])]
    parts = []
    if purpose:
        parts.append(f"Purpose: {purpose}.")
    if any(items):
        parts.append("Agenda: " + "; ".join(item for item in items if item) + ".")
    if expected:
        parts.append(f"Expected: {expected}.")
    return " ".join(parts) or str(fields.get("body_summary") or "")


# The prompts around the invite. Their text is part of event_prompt_hash, so an
# edit here re-extracts an event once, the next time it changes, as a changed
# prompt should.
_INVITATION_TEMPLATE = (
    """You are reading a calendar invitation from a corporate email system: a meeting still to come, or one meeting of a recurring series.

{invite}

An invitation says what a meeting is for, not what it decided. Extract the following as JSON:
{{
  "purpose": "one sentence: what the meeting is for",
  "agenda": ["each agenda item or topic, as the text gives it"],
  "expected": "who is expected to attend, present or prepare, by role or name, as the text says"
}}

Report no decisions and no action items: an agenda item, a topic to discuss or a request to prepare is not a decision. Use an empty string or an empty list for anything the text does not state, and return empty values when it holds no more than joining details.
"""
    + PROMPT_RULE
    + """
Respond with ONLY the JSON object, no other text."""
)

_PROMPT_TEMPLATE = (
    """You are analyzing a calendar event from a corporate email system: a single meeting that has already taken place.

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

Take decisions and action items only from notes or minutes of the meeting itself: what was agreed, and who will do what. An agenda, an invitation or a list of topics to discuss records no decision; for those, return empty arrays and summarise the purpose and agenda.
Set decision_date to the date the text states for the decision, as YYYY-MM-DD, or to JSON null when it states none.
If the body is empty or contains only a Teams link with no agenda, return empty summary and empty arrays.
"""
    + PROMPT_RULE
    + """
Respond with ONLY the JSON object, no other text."""
)


def _moment(value) -> datetime | None:
    """A stored time as an aware UTC moment; stored times are UTC, marked or not."""
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def is_invitation(event: dict, now: datetime | None = None) -> bool:
    """Whether an event's text can only be its invitation.

    True for any occurrence of a series and for a meeting not yet over, or whose
    end cannot be read. Only a single meeting already over can hold notes of it.
    """
    if event.get("is_recurring") or event.get("recurrence_master_id"):
        return True
    ended = _moment(event.get("end_at") or event.get("start_at"))
    return ended is None or ended > (now or datetime.now(UTC))


def _without_joining_details(text: str) -> str:
    """The text without the joining details an online meeting adds to it."""
    lines = text.split("\n")
    for opening, line in enumerate(lines):
        if not _RULE.match(line):
            continue
        following = [nearby for nearby in lines[opening + 1 : opening + 6] if nearby.strip()]
        if not any(_SERVICE.search(nearby) for nearby in following[:3]):
            continue
        width = len(line.strip())
        closing = next(
            (
                n
                for n in range(opening + 1, len(lines))
                if _RULE.match(lines[n]) and len(lines[n].strip()) == width
            ),
            len(lines) - 1,
        )
        lines = lines[:opening] + lines[closing + 1 :]
        break
    return "\n".join(line for line in lines if not _JOIN_LINE.match(line))


# How much of an event's text calendar_events.body keeps. An invitation is a few hundred
# characters; a meeting's notes written into it rarely pass a few thousand.
BODY_MAX_CHARS = 20_000


def event_body_text(body: str | None) -> str | None:
    """An event's text as calendar_events.body stores it: as a reader sees it, the HTML
    converted and the joining details left out, credentials masked, with no floor on its length
    (a short note is still the meeting's), cut at BODY_MAX_CHARS. "" for an event with no text,
    None when none was given."""
    if body is None:
        return None
    text = html_to_text(body) if looks_like_html(body) else body
    return redact_secrets(_without_joining_details(text).strip())[:BODY_MAX_CHARS]


def _readable(body: str | None) -> str | None:
    """The body as a reader sees it, joining details out, or None when under MIN_TEXT
    characters are left. Credentials are redacted before the cut."""
    if not body:
        return None
    text = html_to_text(body) if looks_like_html(body) else body
    text = _without_joining_details(text).strip()
    if len(text) < MIN_TEXT:
        return None
    return redact_secrets(text)[:MAX_TEXT]


def _invite_text(event: dict, body: str | None, invitation: bool | None = None) -> str | None:
    """The invite as the prompt shows it, or None when there is no text to read.

    An invitation leaves out its date: an occurrence of a series is the series'
    invitation, and so its siblings share one prompt and one reading.
    """
    text = _readable(body)
    if text is None:
        return None
    if invitation is None:
        invitation = is_invitation(event)

    subject = event.get("subject", "")
    # parse_event names the organiser organizer_name and organizer_email; the
    # prompt read `organizer`, which it never sets, so this line was always blank.
    organizer = (
        event.get("organizer") or event.get("organizer_name") or event.get("organizer_email") or ""
    )
    # `or`, not a get() default: Graph sends "name": null, and get() returns the
    # stored None rather than the default, which str.join rejects.
    names = [
        str(a.get("name") or a.get("email") or "") if isinstance(a, dict) else str(a)
        for a in event.get("attendees", [])
    ]
    attendees = ", ".join(names[:PROMPT_ATTENDEES])
    if len(names) > PROMPT_ATTENDEES:
        attendees += f" and {len(names) - PROMPT_ATTENDEES} more"

    lines = [f"Event subject: {subject}", f"Organizer: {organizer}", f"Attendees: {attendees}"]
    if not invitation:
        lines.append(f"Date: {event.get('start_at', '')}")
    return "\n".join(lines) + f"\n\nEvent body/description:\n{text}"


def _prompt_parts(event: dict, body: str | None) -> tuple[bool, str, str | None]:
    """Whether the event reads as an invitation, its template, and its invite text."""
    invitation = is_invitation(event)
    template = _INVITATION_TEMPLATE if invitation else _PROMPT_TEMPLATE
    return invitation, template, _invite_text(event, body, invitation)


def event_prompt_hash(event: dict, body: str | None) -> str | None:
    """sha256 of what the model would read for this event: the invite and the prompt
    around it. The fence tag is random per call, so it is left out. None when the
    body leaves nothing to extract.

    calendar-sync stores it with an extraction. Outlook moves an event's etag on
    edits the model never sees (an attendee answering, a room change), and the same
    hash then means the extraction on record still stands. The occurrences of a
    series share it while their invitation does, and a meeting changes it once it
    is over, when it is read as a meeting."""
    _, template, invite = _prompt_parts(event, body)
    if invite is None:
        return None
    return hashlib.sha256(f"{template}\x00{invite}".encode()).hexdigest()


def extract_event(event: dict, body: str | None = None) -> dict:
    """
    Run LLM extraction on a calendar event.

    Args:
        event: Event dict with subject, organizer, attendees, start_at
        body: Optional event body/description

    Returns:
        dict with body_summary, decisions, action_items. An invitation's summary
        is its purpose, agenda and who is expected, and it has no decisions or
        action items whatever the model returns.
    """
    invitation, template, invite = _prompt_parts(event, body)
    # No text to read: an empty result, no LLM call
    if invite is None:
        return {"body_summary": "", "decisions": [], "action_items": []}

    # Whoever sent the invite wrote all of it, the subject included.
    prompt = template.format(invite=fence(invite))

    response = complete(max_tokens=1024, messages=[{"role": "user", "content": prompt}])

    # First text block, never content[0]: extended thinking puts a ThinkingBlock there.
    # 86 events failed this way between 2026-08-12 and 2026-08-24 (calendar-sync.log),
    # on the same days as successful extractions, so the shape is intermittent rather
    # than constant. Unlike attachments there is no llm_error column here, so the
    # failure was visible only in the log.
    raw_text = _response_text(response)

    if invitation:
        return {
            "body_summary": invitation_summary(_json_object(raw_text)),
            "decisions": [],
            "action_items": [],
        }
    return parse_extraction_response(raw_text)
