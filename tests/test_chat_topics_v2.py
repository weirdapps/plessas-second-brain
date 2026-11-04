"""Chat and meeting topics under the v2 routes (issue 281, after 180 and 211).

With BRAIN_EXTRACT_CHAT_TOPICS on and a route named in BRAIN_EXTRACT_V2_ROUTES, the
cached Teams and WhatsApp prompts show the topics key in their worked examples too, and
the calendar v2 request asks for topics in its instructions, schema and example. With
topics off, every v2 request is exactly what #211 sends. Every value is invented.
"""

import json

import pytest

from src.extract import calendar_extractor as ce
from src.extract import teams_prompt, thread_examples
from src.store import topic_registry as tr


def _thread_prompt():
    return teams_prompt.build_prompt(
        {"chat_label": "Okapi", "participants": ["Alice"]},
        [{"composed_at": "2026-04-29T08:00:00", "sender": "Alice", "content": "hi"}],
    )


@pytest.mark.parametrize("route", ["teams", "whatsapp"])
def test_examples_without_topics_are_unchanged(route):
    plain = thread_examples.examples_text(route, commitments=False)
    assert plain == thread_examples.examples_text(route, commitments=False, topics=False)
    assert '"topics"' not in plain


@pytest.mark.parametrize("route", ["teams", "whatsapp"])
@pytest.mark.parametrize("commitments", [False, True])
def test_each_example_answers_with_topics_in_the_order_asked(route, commitments):
    for example in thread_examples.EXAMPLES[route]:
        shown = thread_examples.answer(example, commitments=commitments, topics=True)
        keys = list(shown)
        assert keys.index("topics") == keys.index("key_facts") - 1
        if commitments:
            assert keys.index("commitments") < keys.index("topics")
        assert 1 <= len(shown["topics"]) <= 5


def test_the_cached_prompt_shows_topics_only_when_the_prompt_asks_for_them():
    system, user = _thread_prompt()
    plain_blocks, _ = teams_prompt.cached_prompt("teams", system, user)
    asked_blocks, _ = teams_prompt.cached_prompt("teams", *tr.with_topics(system, user))
    plain = json.dumps(plain_blocks, ensure_ascii=False)
    asked = json.dumps(asked_blocks, ensure_ascii=False)
    assert '\\"topics\\"' not in plain
    assert asked.count('\\"topics\\"') >= 3  # the key asked, and the two examples' answers


# --- calendar ----------------------------------------------------------------------------------

INVITE = "Event subject: Okapi review\nOrganizer: Alice\n\nEvent body/description:\nNotes."


@pytest.mark.parametrize("invitation", [True, False])
def test_a_v2_calendar_request_without_topics_is_unchanged(invitation):
    before = ce._request(invitation, INVITE)
    same = ce._request(invitation, INVITE, topics=False)
    assert before.system == same.system and before.route == same.route
    assert "topics" not in json.dumps(before.route.schema)


@pytest.mark.parametrize("invitation", [True, False])
def test_a_v2_calendar_request_with_topics_asks_everywhere(invitation):
    request = ce._request(invitation, INVITE, topics=True)
    schema = request.route.schema
    assert "topics" in schema["properties"] and "topics" in schema["required"]
    system = json.dumps(request.system, ensure_ascii=False)
    assert "topic tags" in system  # the instructions
    assert system.count('\\"topics\\"') >= 2  # the instructions' key and the example answer


class _Response:
    def __init__(self, text):
        self.content = [type("B", (), {"type": "text", "text": text})()]
        self.stop_reason = "end_turn"


EVENT = {
    "subject": "Okapi budget review",
    "organizer_name": "Alice",
    "attendees": [],
    "start_at": "2026-01-10T09:00:00Z",
    "end_at": "2026-01-10T10:00:00Z",
}
BODY = "Notes: we agreed the okapi budget for 2026 and Bob will circulate the final numbers."


def test_a_v2_calendar_extraction_returns_its_topics(monkeypatch):
    monkeypatch.setenv("BRAIN_EXTRACT_V2_ROUTES", "calendar")
    reply = {
        "body_summary": "Budget.",
        "decisions": [],
        "action_items": [],
        "topics": ["Okapi budget", 3],
    }
    monkeypatch.setattr(ce, "complete", lambda **kw: _Response(json.dumps(reply)))

    assert ce.extract_event(EVENT, BODY, topics=True)["topics"] == ["Okapi budget"]
    assert "topics" not in ce.extract_event(EVENT, BODY)
    assert ce.event_prompt_hash(EVENT, BODY) != ce.event_prompt_hash(EVENT, BODY, topics=True)
    assert ce.event_prompt_hash(EVENT, BODY) == ce.event_prompt_hash(EVENT, BODY, topics=False)


def test_calendar_sync_asks_for_topics_on_the_v2_route(tmp_path, monkeypatch):
    import types

    from src.export import calendar_export
    from src.pipeline import calendar_sync
    from src.store.schema import create_database
    from tests.pipeline.test_calendar_sync import Outlook, _event

    db = tmp_path / "brain.db"
    create_database(str(db)).close()
    calls = []

    def extract(event, body_html, topics=False):
        calls.append(topics)
        return {"body_summary": "Okapi", "decisions": [], "action_items": [], "topics": []}

    monkeypatch.setattr(calendar_export, "run_outlook_cli", Outlook([_event("ev1")]))
    monkeypatch.setattr("src.extract.calendar_extractor.extract_event", extract)
    monkeypatch.setattr(calendar_sync, "DATA_ROOT", tmp_path)
    monkeypatch.setenv(tr.CHAT_TOPICS_ENV, "1")
    monkeypatch.setenv("BRAIN_EXTRACT_V2_ROUTES", "calendar")
    args = {
        "db": str(db),
        "backfill": False,
        "since": None,
        "until": None,
        "skip_extraction": False,
        "dry_run": False,
    }

    assert calendar_sync.run(types.SimpleNamespace(**args)) == 0
    assert calls == [True]
