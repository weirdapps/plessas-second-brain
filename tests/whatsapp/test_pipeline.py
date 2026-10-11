"""brain whatsapp-sync, step 3: sessions through the same Vertex extraction as Teams."""

import json
from unittest.mock import patch

import google.auth.exceptions as gauth
import pytest

from src.export.whatsapp_export import ingest_snapshot
from src.extract.whatsapp_pipeline import extract_threads
from src.extract.whatsapp_prompt import build_prompt
from src.extract.whatsapp_threads import bound_threads
from tests.whatsapp.conftest import ALICE, DIRECT_JID, OWNER, build_snapshot

REPLY = json.dumps(
    {
        "summary": "Agreed to sail on Saturday if the wind allows.",
        "decisions": [{"decision": "Sail on Saturday", "decided_by": "Alice Example"}],
        "action_items": [{"task": "Check the forecast", "owner": "Alice Example"}],
        "key_facts": [{"fact": "The boat is moored at the marina"}],
        "sentiment": "positive",
        "language": "en",
    }
)


def _chat(tmp_path, db, words=("Shall we take the boat out on Saturday morning?",)):
    rows = [
        (f"m{i}", DIRECT_JID, ALICE, text, f"2026-09-01 10:0{i}:00+03:00", 0, "", "")
        for i, text in enumerate(words)
    ]
    rows.append(
        ("r", DIRECT_JID, OWNER, "Yes, if the forecast is good. I will bring the sails and the food.",
         "2026-09-01 10:09:00+03:00", 1, "", "")
    )  # fmt: skip
    ingest_snapshot(db, build_snapshot(tmp_path, rows))
    bound_threads(db)


@pytest.fixture(autouse=True)
def vertex(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "test-project")
    # Never the real ~/.second-brain/needs_gcloud_reauth, whatever a test raises.
    monkeypatch.setattr("src.extract.whatsapp_pipeline.touch_sentinel", lambda: None)


def test_a_session_is_extracted_into_the_shared_tables(db, tmp_path):
    _chat(tmp_path, db)
    with patch("src.extract.whatsapp_pipeline._call_llm", return_value=REPLY):
        out = extract_threads(db)
    assert out["extracted"] == 1
    summary, status = db.execute(
        "SELECT summary, extraction_status FROM whatsapp_threads"
    ).fetchone()
    assert status == "extracted" and "Saturday" in summary
    for table in ("decisions", "action_items", "key_facts"):
        n = db.execute(f"SELECT COUNT(*) FROM {table} WHERE whatsapp_thread_id = 1").fetchone()[0]
        assert n == 1, table


def test_the_model_is_never_called_inside_a_write_transaction(db, tmp_path):
    _chat(tmp_path, db)
    seen = []

    def llm(system, user):
        seen.append(db.in_transaction)
        return REPLY

    with patch("src.extract.whatsapp_pipeline._call_llm", side_effect=llm):
        extract_threads(db)
    assert seen == [False]


def test_a_thread_with_too_little_text_is_skipped_without_a_call(db, tmp_path):
    ingest_snapshot(
        db,
        build_snapshot(
            tmp_path, [("m", DIRECT_JID, ALICE, "ok", "2026-09-01 10:00:00+03:00", 0, "", "")]
        ),
    )
    bound_threads(db)
    with patch("src.extract.whatsapp_pipeline._call_llm") as llm:
        out = extract_threads(db)
    assert out["skipped"] == 1
    llm.assert_not_called()


def test_re_extraction_replaces_the_earlier_rows(db, tmp_path):
    _chat(tmp_path, db)
    with patch("src.extract.whatsapp_pipeline._call_llm", return_value=REPLY):
        extract_threads(db)
        db.execute("UPDATE whatsapp_threads SET extraction_status = 'pending'")
        db.commit()
        extract_threads(db)
    assert (
        db.execute("SELECT COUNT(*) FROM decisions WHERE whatsapp_thread_id = 1").fetchone()[0] == 1
    )


def test_an_expired_credential_leaves_the_thread_pending(db, tmp_path):
    _chat(tmp_path, db)
    with patch(
        "src.extract.whatsapp_pipeline._call_llm",
        side_effect=gauth.RefreshError("invalid_grant: Bad Request"),
    ):
        out = extract_threads(db)
    assert out["deferred"] == 1
    assert db.execute("SELECT extraction_status FROM whatsapp_threads").fetchone()[0] == "pending"


def test_any_other_failure_is_recorded_as_failed(db, tmp_path):
    _chat(tmp_path, db)
    with patch("src.extract.whatsapp_pipeline._call_llm", side_effect=ValueError("bad json")):
        out = extract_threads(db)
    assert out["failed"] == 1


def test_a_spent_deadline_starts_no_thread(db, tmp_path):
    _chat(tmp_path, db)
    with patch("src.extract.whatsapp_pipeline._call_llm") as llm:
        out = extract_threads(db, deadline_s=0)
    assert out["deferred"] == 1
    llm.assert_not_called()


def test_the_prompt_says_whatsapp_and_fences_the_messages():
    system, user = build_prompt(
        {"chat_label": "Chat A", "started_at": "2026-09-01T07:00:00Z",
         "ended_at": "2026-09-01T07:09:00Z", "message_count": 2, "participants": ["Alice Example"]},
        [{"composed_at": "2026-09-01T07:00:00Z", "sender": "Alice Example", "content": "hi there"}],
    )  # fmt: skip
    assert "WhatsApp" in system
    assert "hi there" in user
    assert "Microsoft Teams" not in system + user


def test_extraction_refuses_to_start_without_a_vertex_project(db, tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_VERTEX_PROJECT_ID", raising=False)
    monkeypatch.delenv("VERTEX_SDK_PROJECT", raising=False)
    _chat(tmp_path, db)
    with pytest.raises(RuntimeError):
        extract_threads(db)


def test_the_rows_the_model_writes_are_masked(db, tmp_path):
    """A card number, an IBAN or a password the model copies into a row is masked
    as the row is stored (security-privacy-01 and -07)."""
    from tests.payment_data import card, digits, iban, masked

    _chat(tmp_path, db)
    visa, account = card("4", 16), iban("GR", digits(23, 1))
    reply = {
        **json.loads(REPLY),
        "decisions": [{"decision": f"pay the rent to {account}"}],
        "action_items": [{"task": "κωδικός πρόσβασης: abc123"}],
        "key_facts": [{"fact": f"her card is {visa}"}],
    }

    with patch("src.extract.whatsapp_pipeline._call_llm", return_value=json.dumps(reply)):
        extract_threads(db)

    rows = [
        db.execute(f"SELECT {column} FROM {table}").fetchone()[0]
        for table, column in (
            ("decisions", "decision"),
            ("action_items", "task"),
            ("key_facts", "fact"),
        )
    ]
    assert rows == [
        f"pay the rent to GR[REDACTED:iban]{account[-4:]}",
        "κωδικός πρόσβασης: [REDACTED:password]",
        f"her card is {masked(visa)}",
    ]
