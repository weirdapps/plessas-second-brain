"""Every extraction prompt tells the model not to copy card, account, IBAN, tax or
password values, and says so outside the fenced third-party text.

The prompts asked for "important facts, numbers", and the extractor copied card
numbers and IBANs into key facts (code-extract-02, security-privacy-01 and -07).
src/redact.py masks what it can recognise; the rule also covers what it cannot,
such as an account or tax number with no check digit. It sits with the
instructions: inside the fence the model is told to follow nothing, and the
fenced text itself must reach the model as it was.
"""

import re

import pytest

from src.redact import PROMPT_RULE

TAG = re.compile(r"<(untrusted_[0-9a-f]{12})>\n")
DATA = "Agenda: the quarterly review of 1234 cards, then the budget for 2027."
THREAD = {"chat_label": "Chat", "started_at": "s", "ended_at": "e", "participants": ["A"]}
MESSAGES = [{"composed_at": "t", "sender": "A", "content": DATA}]


def _email():
    from src.extract.prompt import build_extraction_prompt

    return build_extraction_prompt(
        {"sender": {"name": "A", "address": "a@example.com"}, "subject": "s", "content": DATA}
    )


def _conversation():
    from src.extract.prompt import build_conversation_extraction_prompt

    return build_conversation_extraction_prompt({"turns": [{"speaker": "user", "content": DATA}]})


def _attachment():
    from src.extract.attachment_prompt import build_attachment_prompt

    return build_attachment_prompt(DATA, "doc.pdf", "application/pdf", "subject", "2026-10-01")


def _teams():
    from src.extract.teams_prompt import build_prompt

    return "\n".join(build_prompt(THREAD, MESSAGES))


def _whatsapp():
    from src.extract.whatsapp_prompt import build_prompt

    return "\n".join(build_prompt(THREAD, MESSAGES))


def _calendar():
    from src.extract.calendar_extractor import _PROMPT_TEMPLATE, _invite_text
    from src.extract.untrusted import fence

    return _PROMPT_TEMPLATE.format(invite=fence(_invite_text({"subject": "Sync"}, DATA)))


@pytest.mark.parametrize(
    "build",
    [_email, _conversation, _attachment, _teams, _whatsapp, _calendar],
    ids=lambda b: b.__name__,
)
def test_the_rule_is_given_once_and_outside_the_fence(build):
    prompt = build()

    tag = TAG.search(prompt)
    assert tag, "no fence"
    body = prompt[tag.end() : prompt.index(f"\n</{tag.group(1)}>")]
    assert prompt.count(PROMPT_RULE) == 1
    assert PROMPT_RULE not in body
    assert DATA in body  # the sender's text reaches the model as it was


def test_the_rule_reads_as_one_plain_sentence():
    """It is formatted into str.format templates and f-strings alike."""
    assert "{" not in PROMPT_RULE and "}" not in PROMPT_RULE
    assert "\n" not in PROMPT_RULE
