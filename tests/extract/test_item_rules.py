"""The newest-message and decision rules, in every prompt that asks for decisions.

On the replica of 2026-10-16, 16.5% of email decisions, 22.3% of actions and 22.9% of key
facts repeated an item of an earlier email in the same thread, and about half of a
sample of 50 mail "decisions" were no decision: quoted policy text, intentions, table
rows, news. v2's email prompt carries the rules; these tests hold the other prompts that
ask for decisions to them, and schema v2's commitments to a direction and a due date.
"""

import pytest

from src.extract import calendar_extractor, prompt, schema_v2, teams_prompt, whatsapp_prompt
from src.extract.parser import parse_extraction_v2


@pytest.mark.parametrize("system", [teams_prompt.SYSTEM_PROMPT, whatsapp_prompt.SYSTEM_PROMPT])
def test_thread_prompts_carry_the_decision_exclusions_and_the_quote_rule(system):
    folded = system.lower()

    assert "not decisions" in folded
    assert "regulation" in folded and "intention" in folded and "table" in folded
    assert "quoted" in folded
    assert teams_prompt.ITEM_RULES in system


def test_the_past_meeting_prompt_carries_the_decision_exclusions():
    template = calendar_extractor._PROMPT_TEMPLATE.lower()

    assert "not decisions" in template
    assert "regulation" in template and "intention" in template


def test_the_invitation_prompt_still_asks_for_no_decisions_at_all():
    assert "Report no decisions and no action items" in calendar_extractor._INVITATION_TEMPLATE


def test_the_email_prompt_takes_items_from_the_newest_message_alone():
    instructions = prompt.email_system_v2()[0]["text"]

    assert "from the newest message alone" in instructions
    assert "anything decided only in an earlier, quoted message" in instructions


def test_a_commitment_carries_its_direction_and_due_date():
    commitment = schema_v2.COMMITMENT["properties"]

    assert list(commitment) == ["evidence", "commitment", "by", "to", "direction", "due"]
    assert commitment["direction"]["enum"] == list(schema_v2.DIRECTIONS)


def test_the_email_prompt_explains_a_commitments_direction_and_due_date():
    instructions = prompt.email_system_v2()[0]["text"]
    section = instructions.split("COMMITMENTS", 1)[1].split("EVIDENCE", 1)[0]

    assert '"direction"' in section and '"due"' in section


def test_the_parser_reads_a_commitments_direction_and_due_date():
    reply = (
        '{"commitments": [{"evidence": "I will send it by Friday", "commitment": "Send it",'
        ' "by": "Ann", "to": "Bob", "direction": "OWED_TO_OWNER", "due": "2026-03-06T00:00"},'
        ' {"evidence": "", "commitment": "Call back", "by": "Ann", "to": "Bob",'
        ' "direction": "sideways", "due": "next week"}]}'
    )

    first, second = parse_extraction_v2(reply)["commitments"]

    assert (first["direction"], first["due"]) == ("owed_to_owner", "2026-03-06")
    assert (second["direction"], second["due"]) == ("third_party", None)
