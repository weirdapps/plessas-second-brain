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


def test_news_and_automation_mail_take_no_decision_or_action_from_an_attachment(
    tmp_path, monkeypatch
):
    """As the email loader takes none from their bodies: a monitor digest or a machine report
    decides nothing and assigns nothing to anyone. Its facts and topics are still kept."""
    for source_class in ("news", "automation"):
        (tmp_path / source_class).mkdir()
        path, conn = _db(tmp_path / source_class)
        conn.execute("UPDATE emails SET source_class = ?", (source_class,))
        conn.commit()

        _summarise(path, monkeypatch)

        counts = [
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("decisions", "action_items", "key_facts")
        ]
        assert counts == [0, 0, 2], source_class
        conn.close()


V2_EXTRACTION = {
    "extraction_schema": 2,
    "summary": "s",
    "language": "en",
    "topics": [],
    "decisions": [
        {
            "decision": "go ahead",
            "decided_by": "board",
            "status": "made",
            "owner_role": "third_party",
            "evidence": "The board approved the plan.",
            "evidence_found": True,
        },
        {
            "decision": "hire two analysts",
            "status": "proposed",
            "owner_role": "owner_decided",
            "evidence": "a paraphrase the text does not hold",
            "evidence_found": False,
        },
    ],
    "action_items": [
        {
            "task": "send the deck",
            "owner": "me",
            "direction": "owner_owes",
            "evidence": "Please send the deck by Friday.",
            "evidence_found": True,
        }
    ],
    "key_facts": [],
}


def _rows(conn, sql):
    return sorted(tuple(r) for r in conn.execute(sql))


def test_a_v2_summary_keeps_the_v2_fields_the_email_loader_keeps(tmp_path, monkeypatch):
    """Status, owner role and quote of a decision, direction and quote of an action, as
    src/store/loader.py stores them for an email's body: a quote only when the parser found
    it in the document's text."""
    path, conn = _db(tmp_path)

    _summarise(path, monkeypatch, V2_EXTRACTION)

    assert _rows(conn, "SELECT decision, status, owner_role, evidence FROM decisions") == [
        ("go ahead", "made", "third_party", "The board approved the plan."),
        ("hire two analysts", "proposed", "owner_decided", None),
    ]
    assert _rows(conn, "SELECT task, direction, evidence FROM action_items") == [
        ("send the deck", "owner_owes", "Please send the deck by Friday.")
    ]


def test_a_v1_summary_stores_no_v2_field(tmp_path, monkeypatch):
    """A v1 reply has no extraction_schema: keys it happens to carry are not v2 fields."""
    path, conn = _db(tmp_path)
    v1 = {key: value for key, value in V2_EXTRACTION.items() if key != "extraction_schema"}

    _summarise(path, monkeypatch, v1)

    assert _rows(conn, "SELECT status, owner_role, evidence FROM decisions") == [
        (None, None, None),
        (None, None, None),
    ]
    assert _rows(conn, "SELECT direction, evidence FROM action_items") == [(None, None)]
