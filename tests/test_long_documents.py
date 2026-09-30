"""Long files are read in full, and summarised in parts.

MAX_TEXT_CHARS is a ceiling against runaway input, not a cap real documents reach. Phase 2
sends a text longer than LONG_TEXT_CHARS in parts of at most PART_CHARS, cut at paragraph
boundaries, then asks once more for one extraction over the parts' extractions.
"""

import inspect
import json
import sqlite3

from src.extract import attachment_pipeline, claude_extract
from src.extract.attachment_extractors import MAX_TEXT_CHARS, extract_text_from_file
from src.extract.attachment_pipeline import DEFERRED, LONG_TEXT_CHARS, run_phase2
from src.extract.attachment_prompt import PART_CHARS, build_attachment_prompt, split_text
from src.store.schema import create_database


class _TextBlock:
    def __init__(self, text):
        self.text = text


class _Response:
    def __init__(self, text):
        self.content = [_TextBlock(text)]
        self.stop_reason = "end_turn"


def _fake_model(monkeypatch, calls):
    class FakeMessages:
        def create(self, **kw):
            prompt = kw["messages"][0]["content"]
            calls.append(prompt)
            if "extractions of the parts" in prompt:
                body = {"summary": "the whole document", "key_facts": ["a fact from the merge"]}
            else:
                body = {"summary": f"part {len(calls)}", "key_facts": [f"fact {len(calls)}"]}
            base = {"topics": [], "decisions": [], "action_items": [], "language": "english"}
            return _Response(json.dumps({**base, **body}))

    fake = type("Client", (), {"messages": FakeMessages()})()
    monkeypatch.setattr(claude_extract, "_get_client_and_model", lambda: (fake, "m"))


def _long_row(tmp_path, chars):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject) VALUES (7, '2026-09-01', 'deck')"
    )
    conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size,"
        " file_path, exported_at)"
        " VALUES (1, 7, 'deck.txt', 'text/plain', 1, '/x/deck.txt', '2026-09-01')"
    )
    paragraph = "Paragraph about the plan. " + "word " * 400
    text = "\n\n".join([paragraph] * (chars // len(paragraph) + 1))[:chars]
    conn.execute(
        "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method,"
        " extraction_status, extracted_at, llm_status)"
        " VALUES (1, ?, 'direct_read', 'extracted', '2026-09-01', 'pending')",
        (text,),
    )
    conn.commit()
    conn.close()
    return path, text


def test_a_300000_character_text_is_stored_in_full(tmp_path):
    f = tmp_path / "long.txt"
    f.write_text(("A sentence about the plan. " * 10 + "\n\n") * 1200)

    out = extract_text_from_file(str(f), "text/plain")

    assert MAX_TEXT_CHARS >= 2_000_000
    assert len(out["text"]) == len(f.read_text()) > 300_000


def test_parts_follow_paragraphs_and_lose_nothing():
    text = "\n\n".join(f"Paragraph {i}. " + "word " * 500 for i in range(100))

    parts = split_text(text)

    assert len(parts) > 1
    assert all(len(p) <= PART_CHARS for p in parts)
    assert "\n\n".join(parts) == text


def test_a_paragraph_longer_than_a_part_is_cut_hard():
    text = "x" * (PART_CHARS * 2 + 5)

    parts = split_text(text)

    assert [len(p) for p in parts] == [PART_CHARS, PART_CHARS, 5]
    assert "".join(parts) == text


def test_a_part_prompt_names_its_part():
    prompt = build_attachment_prompt(
        extracted_text="text", filename="f.txt", mime_type="text/plain", part=(2, 5)
    )
    assert "part 2 of 5" in prompt


def test_a_long_text_is_summarised_in_parts_then_merged(tmp_path, monkeypatch):
    path, text = _long_row(tmp_path, 130_000)
    calls = []
    _fake_model(monkeypatch, calls)

    stats = run_phase2(str(path))

    assert len(calls) == len(split_text(text)) + 1
    assert stats["extracted"] == 1
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT summary, llm_status FROM attachment_content").fetchone() == (
        "the whole document",
        "extracted",
    )
    assert [r[0] for r in conn.execute("SELECT fact FROM key_facts")] == ["a fact from the merge"]
    conn.close()


def test_running_out_of_time_between_parts_defers_the_item(monkeypatch):
    calls = []
    _fake_model(monkeypatch, calls)
    answers = iter([False, True])
    text = "\n\n".join(["word " * 8000] * 3)
    row = (1, 2, text, "deck.txt", "text/plain", 3, "deck", "2026-09-01")

    _ac, _email, extraction, error, verdict = attachment_pipeline._extract_one_attachment(
        row, lambda: next(answers)
    )

    assert (extraction, verdict) == (None, DEFERRED)
    assert "out of time" in error
    assert len(calls) == 1


def test_a_deferred_item_stays_pending_and_is_not_a_failure(tmp_path, monkeypatch):
    path, _ = _long_row(tmp_path, 130_000)
    monkeypatch.setattr(
        attachment_pipeline,
        "_extract_one_attachment",
        lambda row, *a, **k: (row[0], row[5], None, "deferred: out of time", DEFERRED),
    )

    stats = run_phase2(str(path))

    assert (stats["deferred"], stats["failed"], stats["processed"]) == (1, 0, 0)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT llm_status FROM attachment_content").fetchone()[0] == "pending"
    conn.close()


def test_phase2_can_leave_long_texts_for_later(tmp_path, monkeypatch):
    path, _ = _long_row(tmp_path, 130_000)
    seen = []
    monkeypatch.setattr(
        attachment_pipeline,
        "_extract_one_attachment",
        lambda row, *a, **k: seen.append(row) or (row[0], row[5], {"summary": "s"}, None, False),
    )

    stats = run_phase2(str(path), max_text_chars=LONG_TEXT_CHARS)

    assert seen == []
    assert stats["processed"] == 0


def test_the_hourly_sync_leaves_long_texts_for_the_nightly_pass():
    from src import cli

    source = inspect.getsource(cli)
    at = source.index("deadline_s=PHASE2_SYNC_DEADLINE_S")
    assert "max_text_chars=LONG_TEXT_CHARS" in source[at - 200 : at + 200]
