"""parse_extraction_v2: a schema v2 reply, validated and normalised for the loader.

Structured outputs hold a reply to the schema, except a refusal or a reply cut short,
and enum values may come back in any capitalisation; the parser takes whatever came
back to the shape the loader writes, and checks each evidence quote against the text
the model read.
"""

import json

import pytest

from src.extract.parser import parse_extraction_v2


def _email_reply(**over):
    reply = {
        "item_kind": "work_thread",
        "language": "el",
        "summary": "Εγκρίθηκε ο προϋπολογισμός.",
        "topics": ["προϋπολογισμός"],
        "sentiment": "directive",
        "urgency": "medium",
        "decisions": [
            {
                "evidence": "εγκρίνω τον προϋπολογισμό",
                "decision": "Εγκρίθηκε ο προϋπολογισμός",
                "decided_by": "Ελένη",
                "owner_role": "owner_decided",
                "status": "made",
            }
        ],
        "action_items": [
            {
                "evidence": "Θα στείλω την απόφαση έως την Παρασκευή.",
                "task": "Αποστολή της απόφασης",
                "owner": "Ελένη",
                "direction": "owner_owes",
                "deadline": "2026-03-06",
            }
        ],
        "commitments": [],
        "key_facts": ["Προϋπολογισμός: 40.000 ευρώ"],
        "people": [
            {
                "name_as_written": "Νίκος Αντωνίου",
                "name_latin": "Nikos Antoniou",
                "email": "",
                "role": "requester",
            }
        ],
    }
    reply.update(over)
    return reply


SOURCE = (
    "Email Content:\nΝίκο, εγκρίνω τον προϋπολογισμό των 40.000 ευρώ.\n"
    "Θα στείλω την απόφαση   έως την Παρασκευή."
)


def test_a_reply_in_the_schema_comes_back_as_it_was_marked_v2():
    reply = _email_reply()

    out = parse_extraction_v2(json.dumps(reply, ensure_ascii=False))

    assert out["extraction_schema"] == 2
    for key in ("item_kind", "language", "summary", "topics", "key_facts", "people"):
        assert out[key] == reply[key]
    assert out["decisions"][0]["decision"] == "Εγκρίθηκε ο προϋπολογισμός"
    assert out["action_items"][0]["deadline"] == "2026-03-06"


def test_enums_are_compared_folded():
    """Structured outputs do not guarantee the capitalisation of an enum value."""
    reply = _email_reply(item_kind="Work_Thread", language="EL", urgency="High")
    reply["decisions"][0]["owner_role"] = "Owner_Decided"

    out = parse_extraction_v2(json.dumps(reply))

    assert (out["item_kind"], out["language"], out["urgency"]) == ("work_thread", "el", "high")
    assert out["decisions"][0]["owner_role"] == "owner_decided"


def test_a_v1_language_word_becomes_its_code_and_anything_else_other():
    assert parse_extraction_v2(json.dumps(_email_reply(language="greek")))["language"] == "el"
    assert parse_extraction_v2(json.dumps(_email_reply(language="english")))["language"] == "en"
    assert parse_extraction_v2(json.dumps(_email_reply(language="fr")))["language"] == "other"


def test_an_enum_outside_its_vocabulary_takes_its_default():
    reply = _email_reply(sentiment="angry", item_kind="memo")
    reply["action_items"][0]["direction"] = "inbound"
    reply["decisions"][0]["status"] = "maybe"

    out = parse_extraction_v2(json.dumps(reply))

    assert (out["sentiment"], out["item_kind"]) == ("informational", "other")
    assert out["action_items"][0]["direction"] == "third_party"
    assert out["decisions"][0]["status"] == "made"


def test_missing_fields_are_filled_empty():
    out = parse_extraction_v2('{"summary": "only this"}')

    assert out["summary"] == "only this"
    assert out["decisions"] == out["action_items"] == out["commitments"] == []
    assert out["people"] == out["topics"] == out["key_facts"] == []
    assert (out["item_kind"], out["language"], out["urgency"]) == ("other", "other", "low")


def test_an_item_without_its_text_is_dropped_and_unknown_keys_are_not_kept():
    reply = _email_reply()
    reply["decisions"].append({"evidence": "x", "decided_by": "y"})
    reply["decisions"][0]["reasoning"] = "dropped"
    reply["references"] = ["not in v2"]

    out = parse_extraction_v2(json.dumps(reply))

    assert len(out["decisions"]) == 1
    assert "reasoning" not in out["decisions"][0]
    assert "references" not in out


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("2026-03-06", "2026-03-06"),
        ("2026-03-06T17:00:00Z", "2026-03-06"),
        ("next Friday", None),
        ("2026-02-30", None),
        (None, None),
    ],
)
def test_a_deadline_is_a_date_or_null(given, kept):
    reply = _email_reply()
    reply["action_items"][0]["deadline"] = given

    assert parse_extraction_v2(json.dumps(reply))["action_items"][0]["deadline"] == kept


def test_a_person_without_a_written_name_takes_the_latin_one_and_none_is_dropped():
    reply = _email_reply(
        people=[
            {"name_as_written": "", "name_latin": "Nikos", "email": "", "role": "r"},
            {"name_as_written": "", "name_latin": "", "email": "a@example.com", "role": "r"},
        ]
    )

    out = parse_extraction_v2(json.dumps(reply))

    assert [p["name_as_written"] for p in out["people"]] == ["Nikos"]


def test_evidence_is_checked_against_the_source_spacing_aside():
    reply = _email_reply()
    reply["action_items"].append(
        {
            "evidence": "a sentence the email never wrote",
            "task": "t",
            "owner": "o",
            "direction": "third_party",
            "deadline": None,
        }
    )

    out = parse_extraction_v2(json.dumps(reply, ensure_ascii=False), source=SOURCE)

    assert out["decisions"][0]["evidence_found"] is True
    assert out["action_items"][0]["evidence_found"] is True  # its spaces differ
    assert out["action_items"][1]["evidence_found"] is False


def test_an_empty_evidence_is_not_found():
    reply = _email_reply()
    reply["decisions"][0]["evidence"] = ""

    out = parse_extraction_v2(json.dumps(reply, ensure_ascii=False), source=SOURCE)

    assert out["decisions"][0]["evidence_found"] is False


def test_without_a_source_no_evidence_is_judged():
    out = parse_extraction_v2(json.dumps(_email_reply(), ensure_ascii=False))

    assert "evidence_found" not in out["decisions"][0]


def test_a_reply_not_held_to_the_schema_is_repaired_as_v1_repairs_one():
    raw = "```json\n" + json.dumps(_email_reply(), ensure_ascii=False)[:-1] + ",}\n```"

    out = parse_extraction_v2(raw)

    assert out["summary"] == "Εγκρίθηκε ο προϋπολογισμός."


def test_a_reply_that_is_not_an_object_raises_value_error():
    with pytest.raises(ValueError):
        parse_extraction_v2("[1, 2]")
    with pytest.raises(ValueError):
        parse_extraction_v2("no json here")


def test_an_attachment_reply_keeps_only_the_attachment_fields():
    reply = {
        "language": "en",
        "summary": "A policy.",
        "topics": ["policy"],
        "decisions": [],
        "action_items": [],
        "key_facts": ["Limit: 15,000 euros"],
        "people": [{"name_as_written": "x"}],
    }

    out = parse_extraction_v2(json.dumps(reply), kind="attachment")

    assert out["key_facts"] == ["Limit: 15,000 euros"]
    assert "people" not in out and "item_kind" not in out
    assert out["extraction_schema"] == 2
