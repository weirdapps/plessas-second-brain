"""scripts/repair_case_twins.py gives each email back its own extraction.

The loader matched extraction files by the lowercased message id, so an email whose
id differs from another's only in letter case could be stored with the other's
extraction. The repair finds those emails by comparing the stored summary with the
email's own file, rewrites them from it, and asks the model again for the ones
whose file macOS wrote the twin's over.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from src.extract.extraction_files import extraction_path
from src.store.loader import load_single_email
from src.store.schema import create_database, get_connection

SCRIPT = Path(__file__).parent.parent / "scripts" / "repair_case_twins.py"

KEPT = "AAMkPairOneOAAA="  # stored with its own extraction
CROSSED = "AAMkPairOneoAAA="  # stored with KEPT's; its own file is on disk
SOURCE = "AAMkPairTwoKAAA="  # stored with its own extraction
LOST = "AAMkPairTwokAAA="  # stored with SOURCE's; macOS wrote SOURCE's over its file
ALONE = "AAMkNoTwinXAAA="


@pytest.fixture
def repair(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("repair_case_twins", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered first: the dataclass in it resolves its annotations through it.
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "EXTRACTED", tmp_path / "extracted")
    monkeypatch.setattr(module, "is_replica", lambda: False)
    module.removed = []
    monkeypatch.setattr(
        module, "remove_vectors", lambda ids: module.removed.extend(ids) or len(ids)
    )
    return module


def _extraction(message_id, summary, **extra):
    return {
        "message_id": message_id,
        "summary": summary,
        "topics": [],
        "decisions": [],
        "action_items": [],
        "people_roles": {},
        "key_facts": [],
        **extra,
    }


def _metadata(message_id, cc=()):
    return {
        "message_id": message_id,
        "internet_message_id": f"<{message_id}@example.com>",
        "date_received": "2026-10-02T07:00:00Z",
        "sender": {"name": "Sender", "address": "sender@example.com"},
        "subject": f"subject {message_id}",
        "content": f"body of {message_id}",
        "mailbox_name": "Inbox",
        "to_recipients": [{"name": "Reader", "address": "reader@example.com"}],
        "cc_recipients": list(cc),
    }


@pytest.fixture
def store(tmp_path):
    """The store and disk the old loader left: two crossed pairs and a loner."""
    db = tmp_path / "brain.db"
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    own = {mid: _extraction(mid, f"own summary of {mid}") for mid in (KEPT, CROSSED, SOURCE, ALONE)}
    own[CROSSED]["decisions"] = [{"decision": "crossed decision"}]
    own[KEPT]["decisions"] = [{"decision": "kept decision"}]
    # The model named SOURCE's people recipients: one known by address, one not.
    own[SOURCE]["people_roles"] = {"Known Colleague": "recipient", "Source Boss": "recipient"}
    # Old names where a disk that folds case allows them, current names otherwise.
    (extracted / f"{KEPT}.json").write_text(json.dumps(own[KEPT]))
    extraction_path(extracted, CROSSED).write_text(json.dumps(own[CROSSED]))
    extraction_path(extracted, SOURCE).write_text(json.dumps(own[SOURCE]))
    (extracted / f"{LOST}.json").write_text(json.dumps(own[SOURCE]))  # SOURCE's, LOST's name
    (extracted / f"{ALONE}.json").write_text(json.dumps(own[ALONE]))

    conn = create_database(str(db))
    colleague = {"name": "Known Colleague", "address": "known@example.com"}
    for metadata, extraction in (
        (_metadata(KEPT, cc=[colleague]), own[KEPT]),
        (_metadata(CROSSED), own[KEPT]),
        (_metadata(SOURCE), own[SOURCE]),
        (_metadata(LOST), own[SOURCE]),
        (_metadata(ALONE), own[ALONE]),
    ):
        assert load_single_email(conn, metadata, extraction)
    conn.commit()
    conn.close()
    return db


def _stored(db):
    conn = get_connection(str(db))
    try:
        summaries = dict(conn.execute("SELECT message_id, summary FROM emails"))
        decisions = sorted(
            (m, d)
            for m, d in conn.execute(
                "SELECT e.message_id, d.decision FROM decisions d JOIN emails e ON e.id = d.email_id"
            )
        )
        ids = dict(conn.execute("SELECT message_id, id FROM emails"))
        return summaries, decisions, ids
    finally:
        conn.close()


def test_the_report_names_each_outcome_and_writes_nothing(repair, store, capsys):
    before = _stored(store)

    assert repair.main(["--db", str(store)]) == 0

    out = capsys.readouterr().out
    assert "twin_emails=4 correct=2 rewrite=1 lost=1 unverified=0 unexplained=0" in out
    assert _stored(store) == before
    assert repair.removed == []


def test_apply_rewrites_the_crossed_email_from_its_own_file(repair, store):
    assert repair.main(["--db", str(store), "--apply"]) == 0

    summaries, decisions, ids = _stored(store)
    assert summaries[CROSSED] == f"own summary of {CROSSED}"
    assert summaries[KEPT] == f"own summary of {KEPT}"
    assert summaries[LOST] == f"own summary of {SOURCE}"  # waits for --reextract
    assert (CROSSED, "crossed decision") in decisions
    assert (CROSSED, "kept decision") not in decisions
    assert (KEPT, "kept decision") in decisions
    assert repair.removed == [ids[CROSSED]]


def test_reextract_asks_the_model_for_the_lost_email_and_keeps_its_answer(
    repair, store, monkeypatch, tmp_path
):
    asked = []

    def model(email):
        asked.append(email)
        return _extraction(email["message_id"], "the model's summary"), False

    monkeypatch.setattr(repair, "_ask_model", model)

    assert repair.main(["--db", str(store), "--apply", "--reextract"]) == 0

    summaries, _, ids = _stored(store)
    assert summaries[LOST] == "the model's summary"
    assert [e["message_id"] for e in asked] == [LOST]
    assert asked[0]["content"] == f"body of {LOST}"
    assert asked[0]["to_recipients"] == [{"name": "Reader", "address": "reader@example.com"}]
    saved = json.loads(extraction_path(tmp_path / "extracted", LOST).read_text())
    assert saved["summary"] == "the model's summary"
    assert sorted(repair.removed) == sorted([ids[CROSSED], ids[LOST]])


def test_a_second_run_finds_nothing_to_repair(repair, store, monkeypatch, capsys):
    monkeypatch.setattr(
        repair, "_ask_model", lambda e: (_extraction(e["message_id"], "the model's summary"), False)
    )
    repair.main(["--db", str(store), "--apply", "--reextract"])
    capsys.readouterr()

    assert repair.main(["--db", str(store)]) == 0

    assert "correct=4 rewrite=0 lost=0" in capsys.readouterr().out


def test_the_model_running_out_stops_the_asking(repair, store, monkeypatch):
    monkeypatch.setattr(repair, "_ask_model", lambda e: (None, True))

    assert repair.main(["--db", str(store), "--apply", "--reextract"]) == 1

    summaries, _, _ = _stored(store)
    assert summaries[LOST] == f"own summary of {SOURCE}"
    assert summaries[CROSSED] == f"own summary of {CROSSED}"


def test_apply_refuses_on_a_replica(repair, store, monkeypatch):
    monkeypatch.setattr(repair, "is_replica", lambda: True)
    before = _stored(store)

    assert repair.main(["--db", str(store), "--apply"]) == 2

    assert _stored(store) == before


def test_a_crash_while_asking_still_drops_the_rewritten_vectors(repair, store, monkeypatch):
    def crash(email):
        raise RuntimeError("the model client failed")

    monkeypatch.setattr(repair, "_ask_model", crash)

    with pytest.raises(RuntimeError):
        repair.main(["--db", str(store), "--apply", "--reextract"])

    summaries, _, ids = _stored(store)
    assert summaries[CROSSED] == f"own summary of {CROSSED}"
    assert repair.removed == [ids[CROSSED]]


def test_vectors_a_stopped_run_owed_are_dropped_by_the_next(repair, store, monkeypatch, capsys):
    """Stopped before it dropped them, a run leaves the vectors owed, not forgotten:
    the rewritten emails read as correct from then on, and build_index embeds only
    ids it lacks, so nothing else would ever replace their vectors."""
    recorder = repair.remove_vectors

    def stopped(ids):
        raise OSError("stopped while rewriting the index")

    monkeypatch.setattr(repair, "remove_vectors", stopped)
    with pytest.raises(OSError):
        repair.main(["--db", str(store), "--apply"])
    monkeypatch.setattr(repair, "remove_vectors", recorder)
    capsys.readouterr()

    assert repair.main(["--db", str(store)]) == 0
    assert "1 rewritten emails still hold their old vectors" in capsys.readouterr().out

    assert repair.main(["--db", str(store), "--apply"]) == 0

    _, _, ids = _stored(store)
    assert repair.removed == [ids[CROSSED]]
    capsys.readouterr()
    repair.main(["--db", str(store)])
    assert "still hold their old vectors" not in capsys.readouterr().out
