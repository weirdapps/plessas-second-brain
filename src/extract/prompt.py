"""
Extraction prompt templates for Claude Sonnet.

Builds prompts to extract structured information from emails and conversations.
"""

from typing import Any

from src.config import USER_NAME, USER_ROLE

# Cap on email body chars sent to the LLM. Long bodies (newsletters, deep reply
# chains) can push the JSON response past max_tokens and fail extraction entirely;
# the attachment pipeline already caps at the same size.
MAX_CONTENT_CHARS = 50_000


def build_extraction_prompt(email: dict[str, Any]) -> str:
    """
    Build extraction prompt for Claude Sonnet.

    Args:
        email: Email dictionary with fields from export pipeline:
            - message_id: str
            - date_received: str (ISO format)
            - sender: str | dict with {name, address}
            - to_recipients: list[str] | list[dict with {name, address}]
            - cc_recipients: list[str] | list[dict with {name, address}]
            - subject: str
            - content: str
            - mailbox_name: str

    Returns:
        Extraction prompt string
    """
    # Format sender — handle both string and dict formats
    sender = email.get("sender", "N/A")
    if isinstance(sender, dict):
        sender = f"{sender.get('name', '')} <{sender.get('address', '')}>".strip()

    # Format recipients — handle both string and dict formats
    def format_recipients(recipients: list) -> str:
        parts = []
        for r in recipients:
            if isinstance(r, dict):
                parts.append(f"{r.get('name', '')} <{r.get('address', '')}>".strip())
            else:
                parts.append(str(r))
        return ", ".join(parts)

    to_list = format_recipients(email.get("to_recipients", []))
    cc_list = format_recipients(email.get("cc_recipients", []))

    # Cap the body so a long email can't push the response past max_tokens.
    content = email.get("content", "") or ""
    if len(content) > MAX_CONTENT_CHARS:
        content = (
            content[:MAX_CONTENT_CHARS]
            + f"\n[...email body truncated to {MAX_CONTENT_CHARS} chars]"
        )

    # Build user identity context if configured
    identity_context = ""
    if USER_NAME or USER_ROLE:
        parts = []
        if USER_NAME:
            parts.append(f"The email archive owner is {USER_NAME}")
        if USER_ROLE:
            parts.append(f"whose role is {USER_ROLE}")
        identity_context = f"\n\nContext: {', '.join(parts)}. Extract information from their perspective — actions assigned to them, decisions they made, etc.\n"

    prompt = f"""You are extracting structured information from an email. Read the email carefully and extract the following information as JSON.
{identity_context}
Email Metadata:
From: {sender}
To: {to_list or "N/A"}
CC: {cc_list or "N/A"}
Subject: {email.get("subject", "N/A")}
Date: {email.get("date_received", "N/A")}

Email Content:
{content}

---

Extract the following information and return ONLY a valid JSON object (no markdown, no code blocks, just the JSON):

{{
  "summary": "1-2 sentence summary of the email's substance (not just topic, but what is being communicated/decided/asked)",
  "topics": ["project/initiative tags as brief keywords"],
  "decisions": [
    {{"decision": "description of decision made or communicated", "decided_by": "name or role of decision maker"}}
  ],
  "action_items": [
    {{"task": "description of task", "owner": "person responsible", "deadline": "YYYY-MM-DD or null"}}
  ],
  "commitments": [
    {{"commitment": "description of promise/commitment", "by": "person giving commitment", "to": "person/group receiving commitment"}}
  ],
  "people_roles": {{
    "person_name": "role in this context (decision-maker, reviewer, blocker, FYI, requester, etc.)"
  }},
  "sentiment": "directive | collaborative | informational | escalation | celebratory",
  "urgency": "low | medium | high | critical",
  "language": "greek | english | mixed",
  "key_facts": ["important facts, numbers, dates, external references"],
  "references": ["external documents, links, prior decisions mentioned"]
}}

Rules:
1. Handle both Greek and English text
2. Return empty lists/dicts for fields with no data (never use null for lists/dicts)
3. Be precise: only extract information actually present in the email
4. For action_items, deadline can be null if not specified
5. For people_roles, extract anyone mentioned with a clear role in the context
6. For sentiment, choose the best match from the enum values
7. For urgency, infer from language, deadlines, and context
8. For language, choose based on primary language of content
9. Extract key_facts only if they are substantive (not just pleasantries)
10. References should be explicit mentions of docs/links/prior decisions

Return ONLY the JSON object, nothing else."""

    return prompt


def build_conversation_extraction_prompt(conversation: dict[str, Any]) -> str:
    """Build extraction prompt for a conversation (multiple turns).

    Args:
        conversation: Dict with:
            - session_id: str
            - workspace: str
            - project_name: str
            - started_at: str
            - turns: list of {speaker, content, timestamp, has_code, has_tool_use}

    Returns:
        Extraction prompt string
    """
    identity_context = ""
    if USER_NAME or USER_ROLE:
        parts = []
        if USER_NAME:
            parts.append(f"The conversation owner is {USER_NAME}")
        if USER_ROLE:
            parts.append(f"whose role is {USER_ROLE}")
        identity_context = (
            f"\n\nContext: {', '.join(parts)}. "
            f"The 'user' speaker is this person. "
            f"Extract information from their perspective.\n"
        )

    # Format turns compactly
    turn_lines = []
    for i, turn in enumerate(conversation.get("turns", [])):
        speaker = turn["speaker"].upper()
        content = turn["content"]
        # Truncate very long turns for the prompt
        if len(content) > 5000:
            content = content[:5000] + "\n[...truncated]"
        turn_lines.append(f"[Turn {i + 1}] {speaker}:\n{content}\n")

    turns_text = "\n".join(turn_lines)

    prompt = f"""You are extracting structured information from a conversation between a user and an AI coding assistant (Claude Code). Read the conversation carefully and extract the following as JSON.
{identity_context}
Conversation Metadata:
Project: {conversation.get("project_name", "unknown")}
Workspace: {conversation.get("workspace", "unknown")}
Date: {conversation.get("started_at", "unknown")}

Conversation:
{turns_text}

---

Extract the following and return ONLY a valid JSON object:

{{
  "summary": "2-3 sentence summary of what was discussed, decided, or built in this conversation",
  "topics": ["project/technology/domain tags as brief keywords"],
  "decisions": [
    {{"decision": "technical or business decision made", "decided_by": "user or assistant"}}
  ],
  "action_items": [
    {{"task": "follow-up task identified", "owner": "user or N/A", "deadline": "YYYY-MM-DD or null"}}
  ],
  "preferences_expressed": [
    "user preferences, corrections, or style guidelines expressed (e.g., 'prefers direct SDK over frameworks', 'wants terse responses')"
  ],
  "technical_decisions": [
    "architecture choices, tool selections, implementation approaches chosen (e.g., 'chose SQLite over Postgres for simplicity')"
  ],
  "key_facts": ["important facts, conclusions, or data points from the conversation"],
  "sentiment": "productive | exploratory | debugging | planning | reviewing",
  "urgency": "low | medium | high | critical",
  "language": "greek | english | mixed"
}}

Rules:
1. Handle both Greek and English text
2. Return empty lists for fields with no data
3. Be precise: only extract information actually present
4. For preferences_expressed, capture any user corrections or stated preferences about how they want things done — these are valuable for future conversations
5. For technical_decisions, capture architectural choices, technology selections, and implementation strategies
6. Ignore tool use details (Read, Write, Bash calls) — focus on the substance of what was discussed and decided
7. The summary should capture the arc of the conversation, not just list topics
8. For sentiment, choose the best match from the enum values

Return ONLY the JSON object, nothing else."""

    return prompt
