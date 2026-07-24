"""Prompt template for Teams thread extraction.

Asked the LLM to produce: summary, decisions, action_items, key_facts,
sentiment, language. Mirrors the schema of the email-extraction JSON so
the loader logic stays uniform across kinds.
"""

import json

SYSTEM_PROMPT = (
    "You are extracting structured knowledge from a Microsoft Teams thread "
    "(channel post + replies, or a chat session). Produce the JSON object "
    "below — no commentary, no preamble. Be terse but accurate; this becomes "
    "searchable institutional memory. Write summary, decisions, action_items, "
    "and key_facts in the same language as the thread (e.g. Greek for Greek "
    "threads, English for English threads); the 'language' field is the "
    "ISO 639-1 code for that language."
)

USER_TEMPLATE = """\
THREAD METADATA
- chat: {chat_label}
- thread_kind: {thread_kind}
- started_at: {started_at}
- ended_at: {ended_at}
- participants: {participants}
- message_count: {message_count}

MESSAGES (chronological; system messages already excluded):
{transcript}

Return JSON with these keys (use empty string / empty list when nothing applies):

{{
  "summary": "<= 3 sentences summarising what was discussed and the outcome.",
  "decisions": [
    {{"decision": "<what was decided>", "decided_by": "<participant name or null>", "decision_date": "<ISO date or null>"}}
  ],
  "action_items": [
    {{"task": "<who-does-what>", "owner": "<participant name or null>", "deadline": "<ISO date or null>"}}
  ],
  "key_facts": [
    {{"fact": "<concise fact worth remembering>"}}
  ],
  "sentiment": "<positive|neutral|negative|mixed>",
  "language": "<ISO 639-1 like 'en' or 'el'>"
}}
"""


def build_prompt(thread: dict, messages: list[dict]) -> tuple[str, str]:
    """Build (system, user) prompts for Vertex AI Claude.

    Args:
        thread: row from teams_threads + chat label, e.g.
            {"chat_label": "Cards & Digital / Novak — Cards Strategy",
             "thread_kind": "channel_post",
             "started_at": "...", "ended_at": "...", "message_count": 3,
             "participants": ["Novak, Maria", "Papadopoulos, Nikos"]}
        messages: list of {"composed_at", "sender", "content"} dicts in order.

    Returns:
        (system_prompt, user_prompt)
    """
    transcript = "\n".join(f"[{m['composed_at']}] {m['sender']}: {m['content']}" for m in messages)
    user = USER_TEMPLATE.format(
        chat_label=thread.get("chat_label", "(unknown)"),
        thread_kind=thread.get("thread_kind", ""),
        started_at=thread.get("started_at", ""),
        ended_at=thread.get("ended_at", ""),
        participants=", ".join(thread.get("participants", [])) or "(unknown)",
        message_count=thread.get("message_count", len(messages)),
        transcript=transcript,
    )
    return SYSTEM_PROMPT, user


def parse_response(text: str) -> dict:
    """Parse the LLM JSON response, with tolerance for code-fence wrappers."""
    s = text.strip()
    if s.startswith("```"):
        # Strip the first line and any trailing fence.
        s = "\n".join(line for line in s.splitlines() if not line.startswith("```"))
    return json.loads(s)
