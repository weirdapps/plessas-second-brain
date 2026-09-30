"""An attachment's summary owns its key facts, decisions and action items.

A new summary of the same attachment replaces them and leaves the email's own rows alone. Rows
written before v28 carry no attachment_id, so a row already on the email is not added again.
"""

from src.extract import attachment_pipeline
from src.extract.attachment_pipeline import run_phase2
from src.store.schema import create_database

EXTRACTION = {
    "summary": "s",
    "language": "en",
    "topics": [],
    "decisions": [{"decision": "go ahead", "decided_by": "board"}],
    "action_items": [{"task": "send the deck", "owner": "me"}],
    "key_facts": ["revenue rose", "costs fell"],
}


def _db(tmp_path):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject) VALUES (7, '2026-09-01', 'deck')"
    )
    conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size,"
        " file_path, exported_at)"
        " VALUES (1, 7, 'deck.pdf', 'application/pdf', 1, '/x/deck.pdf', '2026-09-01')"
    )
    conn.execute(
        "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method,"
        " extraction_status, extracted_at, llm_status)"
        " VALUES (1, 'text', 'pdfplumber', 'extracted', '2026-09-01', 'pending')"
    )
    conn.commit()
    return path, conn


def _summarise(path, monkeypatch, extraction=EXTRACTION):
    monkeypatch.setattr(
        attachment_pipeline,
        "_extract_one_attachment",
        lambda row, *a, **k: (row[0], row[5], extraction, None, False),
    )
    run_phase2(str(path))


def test_the_rows_carry_their_attachment(tmp_path, monkeypatch):
    path, conn = _db(tmp_path)

    _summarise(path, monkeypatch)

    for table in ("key_facts", "decisions", "action_items"):
        assert {r[0] for r in conn.execute(f"SELECT attachment_id FROM {table}")} == {1}


def test_a_new_summary_replaces_the_attachments_rows_only(tmp_path, monkeypatch):
    path, conn = _db(tmp_path)
    conn.execute("INSERT INTO key_facts (email_id, fact) VALUES (1, 'from the email body')")
    conn.commit()
    _summarise(path, monkeypatch)
    conn.execute("UPDATE attachment_content SET llm_status = 'pending'")
    conn.commit()

    _summarise(
        path,
        monkeypatch,
        {**EXTRACTION, "key_facts": ["revenue rose sharply"], "decisions": [], "action_items": []},
    )

    facts = sorted(r[0] for r in conn.execute("SELECT fact FROM key_facts"))
    assert facts == ["from the email body", "revenue rose sharply"]
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0


def test_a_row_already_on_the_email_is_not_added_twice(tmp_path, monkeypatch):
    path, conn = _db(tmp_path)
    conn.execute("INSERT INTO key_facts (email_id, fact) VALUES (1, 'revenue rose')")
    conn.commit()

    _summarise(path, monkeypatch)

    count = conn.execute("SELECT COUNT(*) FROM key_facts WHERE fact = 'revenue rose'").fetchone()[0]
    assert count == 1
