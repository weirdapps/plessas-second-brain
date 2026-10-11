"""Prompt for WhatsApp session extraction.

The Teams thread prompt with its own opening: the JSON the model returns is the
same shape (summary, decisions, action_items, key_facts, sentiment, language),
so the pipeline writes it to the same tables. What differs is what the model is
told it is reading: chats with family, friends and colleagues, not a work
channel, and short messages with media shown only by their kind.
"""

from src.extract.teams_prompt import THREAD_TEMPLATE, USER_TEMPLATE, parse_response
from src.extract.untrusted import fence
from src.redact import PROMPT_RULE

__all__ = ["SYSTEM_PROMPT", "build_prompt", "parse_response"]

SYSTEM_PROMPT = (
    "You are extracting structured knowledge from one session of a WhatsApp chat "
    "(a one-to-one or group chat on the user's phone, with family, friends or "
    "colleagues). Messages are short and informal; a photo, voice note or document "
    "appears only as its kind in brackets. Produce the JSON object below, no "
    "commentary, no preamble. Be terse but accurate; this becomes searchable "
    "personal memory. Write summary, decisions, action_items and key_facts in the "
    "same language as the chat (e.g. Greek for Greek chats, English for English "
    "chats); the 'language' field is the ISO 639-1 code for that language."
    f" {PROMPT_RULE}"
)


def build_prompt(thread: dict, messages: list[dict]) -> tuple[str, str]:
    """(system, user) prompts for one session.

    thread: {"chat_label", "started_at", "ended_at", "message_count", "participants"}
    messages: [{"composed_at", "sender", "content"}] in order.
    """
    transcript = "\n".join(f"[{m['composed_at']}] {m['sender']}: {m['content']}" for m in messages)
    thread_text = THREAD_TEMPLATE.format(
        chat_label=thread.get("chat_label", "(unknown)"),
        thread_kind="whatsapp_session",
        started_at=thread.get("started_at", ""),
        ended_at=thread.get("ended_at", ""),
        participants=", ".join(thread.get("participants", [])) or "(unknown)",
        message_count=thread.get("message_count", len(messages)),
        transcript=transcript,
    )
    return SYSTEM_PROMPT, USER_TEMPLATE.format(thread=fence(thread_text))
