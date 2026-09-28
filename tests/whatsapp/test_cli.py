"""`brain whatsapp-sync` end to end: ingest, bound, extract, embed, and its guards."""

import json
import sys
from unittest.mock import patch

import numpy as np
import pytest

from src import cli
from src.store import embeddings
from src.store.schema import create_database, get_connection
from tests.whatsapp.conftest import ALICE, DIRECT_JID, OWNER, build_snapshot

REPLY = json.dumps({"summary": "Agreed on the Saturday sail.", "decisions": [], "action_items": [],
                    "key_facts": [], "sentiment": "positive", "language": "en"})  # fmt: skip


@pytest.fixture
def store(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    create_database(str(db)).close()
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "test-project")
    monkeypatch.setattr(embeddings, "EMBEDDINGS_FILE", tmp_path / "embeddings.npz")
    monkeypatch.setattr(embeddings, "_get_client", lambda: None)
    monkeypatch.setattr(
        embeddings,
        "generate_embeddings",
        lambda texts, client=None: np.ones((len(texts), 4), dtype=np.float32),
    )
    snapshot = build_snapshot(
        tmp_path,
        [
            ("m1", DIRECT_JID, ALICE, "Shall we take the boat out on Saturday morning?",
             "2026-09-01 10:00:00+03:00", 0, "", ""),
            ("m2", DIRECT_JID, OWNER, "Yes, if the forecast is good. I will bring the sails.",
             "2026-09-01 10:05:00+03:00", 1, "", ""),
        ],
    )  # fmt: skip
    return db, snapshot


def _main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["brain", *argv])
    try:
        cli.main()
    except SystemExit as e:
        return e.code
    return 0


def test_one_run_takes_a_snapshot_all_the_way_to_a_vector(store, monkeypatch):
    db, snapshot = store
    with patch("src.extract.whatsapp_pipeline._call_llm", return_value=REPLY):
        rc = _main(monkeypatch, "--db", str(db), "whatsapp-sync", "--snapshot", str(snapshot))
    assert rc == 0
    conn = get_connection(str(db))
    assert conn.execute("SELECT COUNT(*) FROM whatsapp_messages").fetchone()[0] == 2
    assert (
        conn.execute("SELECT extraction_status FROM whatsapp_threads").fetchone()[0] == "extracted"
    )
    conn.close()
    ids = np.load(embeddings.EMBEDDINGS_FILE)["ids"]
    assert embeddings.WHATSAPP_THREAD_ID_OFFSET - 1 in ids


def test_a_missing_snapshot_exits_66(store, monkeypatch, tmp_path):
    db, _ = store
    rc = _main(monkeypatch, "--db", str(db), "whatsapp-sync", "--snapshot", str(tmp_path / "no.db"))
    assert rc == 66


def test_a_replica_refuses_to_run_it(store, monkeypatch):
    db, snapshot = store
    monkeypatch.setenv("BRAIN_ROLE", "replica")
    rc = _main(monkeypatch, "--db", str(db), "whatsapp-sync", "--snapshot", str(snapshot))
    assert rc == 2
    assert "whatsapp-sync" not in cli.READ_ONLY_COMMANDS


def test_the_stage_budgets_fit_the_unit_timeout():
    stages = (
        cli.WHATSAPP_INGEST_OBSERVED_S,
        cli.WHATSAPP_BOUND_OBSERVED_S,
        cli.WHATSAPP_EXTRACT_DEADLINE_S,
        cli.WHATSAPP_EMBED_OBSERVED_S,
    )
    assert sum(stages) < cli.WHATSAPP_UNIT_TIMEOUT_S
