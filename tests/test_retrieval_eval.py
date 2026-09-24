"""scripts/retrieval_eval.py: a known-item baseline for keyword search."""

import importlib.util
import json
from pathlib import Path

import pytest

from src.store.schema import create_database

SCRIPT = Path(__file__).parent.parent / "scripts" / "retrieval_eval.py"


@pytest.fixture
def ev():
    spec = importlib.util.spec_from_file_location("retrieval_eval", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    rows = [
        (1, "Quarterly zebrafinch budget review", "Inbox"),
        (2, "Hello", "Inbox"),  # one search word: not usable
        (3, "Zebrafinch market wrap", "News"),  # news: left out
        (4, "", "Inbox"),
    ]
    conn.executemany(
        "INSERT INTO emails (id, message_id, date_received, subject, mailbox_name, summary) "
        "VALUES (?, ?, '2026-09-01T00:00:00Z', ?, ?, 'nothing about it')",
        [(i, i, subject, mailbox) for i, subject, mailbox in rows],
    )
    conn.commit()
    conn.close()
    return path


def test_the_set_takes_the_longest_subject_words_of_usable_mail_only(ev, db):
    conn = ev._open(db)

    items = ev.build_set(conn, size=5, seed=1)

    assert items == [{"email_id": 1, "query": "zebrafinch quarterly"}]


def test_the_score_counts_ranks(ev):
    result = ev.score([1, 3, None, 11])

    assert result["queries"] == 4
    assert (result["hit@1"], result["hit@5"], result["hit@10"]) == (0.25, 0.5, 0.5)
    assert result["mrr"] == pytest.approx((1 + 1 / 3 + 1 / 11) / 4)


def test_build_then_run_finds_the_email_by_its_subject(ev, db, tmp_path, capsys):
    """The subject is indexed from v22, so this is also the index's own check."""
    eval_set = tmp_path / "set.json"

    assert ev.main(["build", "--db", str(db), "--file", str(eval_set)]) == 0
    assert ev.main(["run", "--db", str(db), "--file", str(eval_set)]) == 0

    out = capsys.readouterr().out
    result = json.loads(out[out.index("{") : out.index("}") + 1])
    assert result["hit@1"] == 1.0
    assert json.loads(eval_set.read_text())[0]["email_id"] == 1


def test_the_database_is_opened_read_only(ev, db):
    import sqlite3

    conn = ev._open(db)

    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM emails")
