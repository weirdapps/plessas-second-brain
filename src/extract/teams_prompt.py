"""Prompt template for Teams thread extraction.

Asked the LLM to produce: summary, decisions, action_items, key_facts,
sentiment, language. Mirrors the schema of the email-extraction JSON so
the loader logic stays uniform across kinds.
"""

import json

from src.extract.untrusted import fence
from src.redact import PROMPT_RULE

# What makes a decision, and where items come from: the rules of the email prompt's v2
# (src/extract/prompt.py), for a thread. About half of a sample of mail "decisions" were
# quoted policy, intentions, table rows or news, and a reply that quotes an earlier
# message re-extracted it. Shared with the WhatsApp prompt.
ITEM_RULES = (
    "A decision is a choice made by an identifiable person or body, which a message"
    " states as made, proposed or requested. Not decisions: a requirement of a law,"
    " regulation, contract or policy that a message quotes; an agenda item or a topic to"
    " discuss; a row or a figure of a table; an intention without a choice ('we will look"
    " into it'). A message may quote or paste an earlier message, a forwarded email or a"
    " document: read that quoted text as context, and take decisions and action items"
    " from what the participants themselves wrote in the thread, never from the quoted"
    " text alone."
)

SYSTEM_PROMPT = (
    "You are extracting structured knowledge from a Microsoft Teams thread "
    "(channel post + replies, or a chat session). Produce the JSON object "
    "below — no commentary, no preamble. Be terse but accurate; this becomes "
    "searchable institutional memory. Write summary, decisions, action_items, "
    "and key_facts in the same language as the thread (e.g. Greek for Greek "
    "threads, English for English threads); the 'language' field is the "
    "ISO 639-1 code for that language. "
    f"{ITEM_RULES} {PROMPT_RULE}"
)

# The chat name and participants' names are third-party text, as the messages
# are, so the whole thread goes inside the fence.
THREAD_TEMPLATE = """\
THREAD METADATA
- chat: {chat_label}
- thread_kind: {thread_kind}
- started_at: {started_at}
- ended_at: {ended_at}
- participants: {participants}
- message_count: {message_count}

MESSAGES (chronological; system messages already excluded):
{transcript}"""

USER_TEMPLATE = """\
{thread}

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
    thread_text = THREAD_TEMPLATE.format(
        chat_label=thread.get("chat_label", "(unknown)"),
        thread_kind=thread.get("thread_kind", ""),
        started_at=thread.get("started_at", ""),
        ended_at=thread.get("ended_at", ""),
        participants=", ".join(thread.get("participants", [])) or "(unknown)",
        message_count=thread.get("message_count", len(messages)),
        transcript=transcript,
    )
    user = USER_TEMPLATE.format(thread=fence(thread_text))
    return SYSTEM_PROMPT, user


# Commitments (issue 168), asked of a thread only on a store that can hold them: schema
# v43 (src/store/ledger.py), gated, so the prompts above go out unchanged until the owner
# takes that step. Shared with the WhatsApp prompt.
COMMITMENT_RULE = (
    "Also write commitments, in the same language: a commitment is a promise a participant"
    " makes to another in the thread ('I will send it by Friday'), with 'by' who made it"
    " and 'to' who received it; a plan, a wish or a request is not one."
)
_COMMITMENTS_KEY = """  "commitments": [
    {{"commitment": "<the promise>", "by": "<participant who made it or null>", "to": "<participant it was made to or null>", "due": "<ISO date or null>"}}
  ],
""".replace("{{", "{").replace("}}", "}")
_KEY_FACTS_KEY = '  "key_facts": [\n'


def with_commitments(system: str, user: str) -> tuple[str, str]:
    """A thread prompt that also asks for commitments: the rule after the system prompt,
    the key before key_facts in the JSON the user turn asks for."""
    if _KEY_FACTS_KEY not in user:
        raise ValueError("not a thread prompt: no key_facts key")
    head, _, tail = user.rpartition(_KEY_FACTS_KEY)
    return f"{system} {COMMITMENT_RULE}", head + _COMMITMENTS_KEY + _KEY_FACTS_KEY + tail


def parse_response(text: str) -> dict:
    """Parse the LLM JSON response, with tolerance for code-fence wrappers."""
    s = text.strip()
    if s.startswith("```"):
        # Strip the first line and any trailing fence.
        s = "\n".join(line for line in s.splitlines() if not line.startswith("```"))
    return json.loads(s)
