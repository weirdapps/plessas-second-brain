"""Two emails whose message ids differ only in letter case each keep their own extraction.

Outlook message ids are case-sensitive base64 built on a counter, so two messages
26 steps apart share every character but one: 'O' in one id, 'o' in the other.
The loader indexed extraction files by the lowercased name, for macOS, whose disk
folds case. On the producer's Linux disk both files exist, and each email got
whichever of the two the directory listed last: 2,465 of 4,050 such pairs ended up
sharing one extraction, the second email showing the first one's summary,
decisions, action items and people. On macOS the second write replaced the first
file outright.
"""

import json

from src.extract import local
from src.extract.extraction_files import extraction_path, read_extraction
from src.store.loader import load_extractions, load_single_email, replace_extraction
from src.store.schema import create_database, get_connection

TWIN_UPPER = "AAMkTwinCounterOAAA="
TWIN_LOWER = "AAMkTwinCounteroAAA="


def _extraction(message_id, summary, **extra):
    return {
        "message_id": message_id,
        "summary": summary,
        "sentiment": "informational",
        "urgency": "low",
        "language": "english",
        "topics": [],
        "decisions": [],
        "action_items": [],
        "commitments": [],
        "people_roles": {},
        "key_facts": [],
        **extra,
    }


def _staged(message_id, subject):
    return {
        "message_id": message_id,
        "internet_message_id": f"<{subject.replace(' ', '-')}@example.com>",
        "date_received": "2026-10-02T07:00:00Z",
        "sender": {"name": "Sender", "address": "sender@example.com"},
        "subject": subject,
        "content": "body",
        "mailbox_name": "Inbox",
        "to_recipients": [{"name": "Reader", "address": "reader@example.com"}],
        "cc_recipients": [],
    }


def _stage(tmp_path, *emails):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "batch-00001.json").write_text(json.dumps({"emails": list(emails)}))
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    db = tmp_path / "brain.db"
    create_database(str(db)).close()
    return db, staging, extracted


def _summaries(db):
    conn = get_connection(str(db))
    try:
        return dict(conn.execute("SELECT message_id, summary FROM emails"))
    finally:
        conn.close()


def test_case_twins_get_two_file_names_on_a_disk_that_folds_case(tmp_path):
    upper = extraction_path(tmp_path, TWIN_UPPER)
    lower = extraction_path(tmp_path, TWIN_LOWER)

    assert upper.name.lower() != lower.name.lower()
    assert upper.name.startswith(TWIN_UPPER)


def test_an_email_never_loads_its_case_twins_extraction(tmp_path):
    """The twin is extracted, the email is staged and not yet: it waits for its own."""
    db, staging, extracted = _stage(
        tmp_path, _staged(TWIN_UPPER, "first"), _staged(TWIN_LOWER, "second")
    )
    (extracted / f"{TWIN_UPPER}.json").write_text(
        json.dumps(_extraction(TWIN_UPPER, "first summary"))
    )

    assert load_extractions(str(db), str(extracted), str(staging)) == 1

    assert _summaries(db) == {TWIN_UPPER: "first summary"}


def test_case_twins_each_load_their_own_extraction(tmp_path):
    db, staging, extracted = _stage(
        tmp_path, _staged(TWIN_UPPER, "first"), _staged(TWIN_LOWER, "second")
    )
    for message_id, summary in ((TWIN_UPPER, "first summary"), (TWIN_LOWER, "second summary")):
        extraction_path(extracted, message_id).write_text(
            json.dumps(_extraction(message_id, summary))
        )

    assert load_extractions(str(db), str(extracted), str(staging)) == 2

    assert _summaries(db) == {TWIN_UPPER: "first summary", TWIN_LOWER: "second summary"}


def test_a_file_holding_another_ids_extraction_is_not_loaded(tmp_path):
    """macOS wrote the second twin over the first twin's file, name and all."""
    db, staging, extracted = _stage(tmp_path, _staged(TWIN_LOWER, "second"))
    (extracted / f"{TWIN_LOWER}.json").write_text(
        json.dumps(_extraction(TWIN_UPPER, "first summary"))
    )

    assert load_extractions(str(db), str(extracted), str(staging)) == 0
    assert read_extraction(extracted, TWIN_LOWER) is None


def test_a_file_from_before_ids_were_stored_loads_by_its_exact_name(tmp_path):
    (tmp_path / "1001.json").write_text(json.dumps({"summary": "old"}))

    assert read_extraction(tmp_path, "1001") == {"summary": "old"}


def test_the_current_name_wins_over_an_old_file(tmp_path):
    (tmp_path / f"{TWIN_UPPER}.json").write_text(json.dumps(_extraction(TWIN_UPPER, "old")))
    extraction_path(tmp_path, TWIN_UPPER).write_text(json.dumps(_extraction(TWIN_UPPER, "new")))

    assert read_extraction(tmp_path, TWIN_UPPER)["summary"] == "new"


def test_extraction_writes_under_the_case_safe_name(monkeypatch, tmp_path):
    monkeypatch.setattr(local, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(local, "EXTRACTED_DIR", tmp_path / "extracted")
    monkeypatch.setattr(local, "LOG_FILE", tmp_path / "extract.log")
    monkeypatch.setattr(
        "src.extract.claude_extract._get_client_and_model", lambda: (object(), "model")
    )
    monkeypatch.setattr(
        local, "collect_emails", lambda: [{"message_id": TWIN_UPPER}, {"message_id": TWIN_LOWER}]
    )
    monkeypatch.setattr(
        local,
        "extract_inline",
        lambda e, k, engine="claude": (
            e["message_id"],
            _extraction(e["message_id"], f"summary of {e['message_id']}"),
            False,
            None,
        ),
    )

    local.run_extraction(workers=1, deadline_s=600.0)

    for message_id in (TWIN_UPPER, TWIN_LOWER):
        assert read_extraction(tmp_path / "extracted", message_id)["summary"] == (
            f"summary of {message_id}"
        )


def _loaded_with_the_twins_extraction(tmp_path):
    """An email stored with its case twin's extraction, as the old loader left it, beside
    rows that are not the extraction's: header people, an attachment's decision, topic and
    key fact, and a decision written before attachment rows carried attachment_id."""
    db = tmp_path / "brain.db"
    conn = create_database(str(db))
    metadata = _staged(TWIN_LOWER, "second")
    wrong = _extraction(
        TWIN_UPPER,
        "first summary",
        sentiment="directive",
        topics=["Twin Topic", "Shared Topic"],
        decisions=[{"decision": "twin decision", "decided_by": "Someone"}],
        action_items=[{"task": "twin task", "owner": "Someone"}],
        commitments=[{"commitment": "twin promise", "by": "A", "to": "B"}],
        people_roles={"Twin Person": "approver"},
        key_facts=["twin fact"],
    )
    assert load_single_email(conn, metadata, wrong)
    email_id = conn.execute("SELECT id FROM emails WHERE message_id = ?", (TWIN_LOWER,)).fetchone()[
        0
    ]
    attachment_topic = conn.execute(
        "INSERT INTO topics (name, display_name) VALUES ('attachment topic', 'Attachment Topic')"
    ).lastrowid
    conn.execute(
        "INSERT INTO email_topics (email_id, topic_id) VALUES (?, ?)", (email_id, attachment_topic)
    )
    conn.execute(
        "INSERT INTO decisions (email_id, decision, attachment_id) VALUES (?, 'from the pdf', 7)",
        (email_id,),
    )
    conn.execute(
        "INSERT INTO key_facts (email_id, fact, attachment_id) VALUES (?, 'pdf fact', 7)",
        (email_id,),
    )
    conn.execute(
        "INSERT INTO decisions (email_id, decision) VALUES (?, 'older attachment decision')",
        (email_id,),
    )
    conn.commit()
    right = _extraction(
        TWIN_LOWER,
        "replacement summary",
        topics=["Own Topic", "Shared Topic"],
        decisions=[{"decision": "own decision"}],
        action_items=["own task"],
        commitments=[],
        people_roles={"Own Person": ["requester"]},
        key_facts=["own fact"],
    )
    return conn, email_id, metadata, wrong, right


def _rows(conn, email_id):
    def column(sql):
        return sorted(r[0] for r in conn.execute(sql, (email_id,)))

    return {
        "decisions": column("SELECT decision FROM decisions WHERE email_id = ?"),
        "actions": column("SELECT task FROM action_items WHERE email_id = ?"),
        "commitments": column("SELECT commitment FROM commitments WHERE email_id = ?"),
        "facts": column("SELECT fact FROM key_facts WHERE email_id = ?"),
        "topics": column(
            "SELECT t.name FROM email_topics et JOIN topics t ON t.id = et.topic_id"
            " WHERE et.email_id = ?"
        ),
        "people": column(
            "SELECT p.name || ':' || ep.role_in_email FROM email_people ep"
            " JOIN people p ON p.id = ep.person_id WHERE ep.email_id = ?"
        ),
    }


def test_replace_extraction_swaps_the_twins_rows_for_the_emails_own(tmp_path):
    conn, email_id, metadata, wrong, right = _loaded_with_the_twins_extraction(tmp_path)

    replace_extraction(conn, email_id, metadata, wrong=wrong, right=right)
    conn.commit()

    assert tuple(
        conn.execute(
            "SELECT summary, sentiment, urgency, language FROM emails WHERE id = ?", (email_id,)
        ).fetchone()
    ) == ("replacement summary", "informational", "low", "english")
    assert _rows(conn, email_id) == {
        "decisions": ["from the pdf", "older attachment decision", "own decision"],
        "actions": ["own task"],
        "commitments": [],
        "facts": ["own fact", "pdf fact"],
        "topics": ["attachment topic", "own topic", "shared topic"],
        "people": ["Own Person:requester", "Reader:recipient", "Sender:sender"],
    }

    # The summary is searchable as the email's own, not the twin's.
    def matches(word):
        return [
            r[0]
            for r in conn.execute("SELECT rowid FROM emails_fts WHERE emails_fts MATCH ?", (word,))
        ]

    assert matches("replacement") == [email_id]
    assert matches("first") == []
    conn.close()


def test_replace_extraction_twice_changes_nothing_more(tmp_path):
    conn, email_id, metadata, wrong, right = _loaded_with_the_twins_extraction(tmp_path)
    replace_extraction(conn, email_id, metadata, wrong=wrong, right=right)
    once = _rows(conn, email_id)

    replace_extraction(conn, email_id, metadata, wrong=right, right=right)

    assert _rows(conn, email_id) == once
    conn.close()
