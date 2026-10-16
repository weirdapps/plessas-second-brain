"""Topic families: folding, containment and embedding, and the reads that expand a topic.

No topic or link is changed: topic_family only maps. Every value is invented.
"""

import numpy as np
import pytest

from src.store import query
from src.store import topic_canon as tc
from src.store.context import get_topic_context
from src.store.schema import create_database
from tests import factories as f


@pytest.fixture
def conn(tmp_path):
    db = create_database(str(tmp_path / "brain.db"))
    yield db
    db.close()


def _tag(conn, name, times):
    """`times` emails tagged with topic `name`; the id of the topic."""
    for _ in range(times):
        f.email(conn, subject=f"About {name}", topics=(name,))
    return conn.execute("SELECT id FROM topics WHERE name = ?", (name,)).fetchone()[0]


def _rows(conn):
    return {
        (t, h, m) for t, h, m in conn.execute("SELECT topic_id, head_id, method FROM topic_family")
    }


def test_plurals_and_word_order_fold_under_the_most_used(conn):
    card = _tag(conn, "credit card", 6)
    cards = _tag(conn, "credit cards", 2)
    reordered = _tag(conn, "card credit", 1)

    rows = tc.plan_fold_and_containment(conn)

    assert {(t, h, m) for t, h, m, _s in rows} >= {
        (cards, card, "fold"),
        (reordered, card, "fold"),
    }


def test_a_rare_topic_goes_under_the_most_specific_frequent_topics_its_words_hold(conn):
    payments = _tag(conn, "payments", 6)
    fraud_prevention = _tag(conn, "fraud prevention", 6)
    fraud = _tag(conn, "fraud", 6)
    rare = _tag(conn, "card payments fraud prevention", 1)
    _tag(conn, "2026", 9)  # a number heads nothing
    dated = _tag(conn, "budget 2026", 1)

    rows = tc.plan_fold_and_containment(conn)

    heads = {h for t, h, m, _s in rows if t == rare and m == "containment"}
    assert heads == {payments, fraud_prevention}  # 'fraud' is inside 'fraud prevention'
    assert fraud not in heads
    assert not [r for r in rows if r[0] == dated]


def test_writing_is_idempotent_and_touches_no_topic(conn):
    _tag(conn, "credit card", 6)
    _tag(conn, "credit cards", 2)
    topics = conn.execute("SELECT * FROM topics ORDER BY id").fetchall()
    links = conn.execute("SELECT * FROM email_topics ORDER BY email_id").fetchall()

    first = tc.write_rows(conn, tc.plan_fold_and_containment(conn))
    second = tc.write_rows(conn, tc.plan_fold_and_containment(conn))

    assert first == 1 and second == 0
    assert conn.execute("SELECT * FROM topics ORDER BY id").fetchall() == topics
    assert conn.execute("SELECT * FROM email_topics ORDER BY email_id").fetchall() == links


def test_a_family_is_one_way_for_containment_and_both_ways_for_synonyms(conn):
    payments = _tag(conn, "payments", 6)
    cardless = _tag(conn, "cardless payments", 1)
    synonym = _tag(conn, "payment", 1)
    tc.write_rows(
        conn, [(cardless, payments, "containment", 0.5), (synonym, payments, "fold", 1.0)]
    )

    assert tc.topic_family_ids(conn, payments) == [payments, cardless, synonym]
    assert tc.topic_family_ids(conn, cardless) == [cardless]
    assert tc.topic_family_ids(conn, synonym) == [synonym, payments, cardless]


def _vectors(table):
    def vectors_of(names):
        out = np.array([table[n] for n in names], dtype=np.float32)
        return out / np.linalg.norm(out, axis=1, keepdims=True)

    return vectors_of


def test_embedding_joins_a_name_to_a_more_used_synonym_within_the_floor(conn):
    watch = _tag(conn, "competitor watch", 6)
    monitoring = _tag(conn, "competitor monitoring", 3)
    q1 = _tag(conn, "q1 2026", 4)
    q2 = _tag(conn, "q2 2026", 2)
    other = _tag(conn, "lunch menu", 2)
    table = {
        "competitor watch": [1.0, 0.0, 0.0],
        "competitor monitoring": [0.99, 0.05, 0.0],
        "q1 2026": [0.0, 1.0, 0.0],
        "q2 2026": [0.0, 0.999, 0.01],
        "lunch menu": [0.0, 0.0, 1.0],
    }

    rows = tc.plan_embedding(conn, _vectors(table), floor=0.92)

    assert [(t, h, m) for t, h, m, _s in rows] == [(monitoring, watch, "embedding")]
    assert q1 and q2 and other  # other quarters and other topics stay apart


def test_topic_context_reads_the_whole_family(conn):
    payments = _tag(conn, "payments", 2)
    cardless = f.email(conn, subject="Cardless", topics=("cardless payments",))
    f.decision(conn, "Launch cardless payments", email_id=cardless)
    rare = conn.execute("SELECT id FROM topics WHERE name = 'cardless payments'").fetchone()[0]
    tc.write_rows(conn, [(rare, payments, "containment", 0.5)])

    out = get_topic_context(conn, "payments")

    assert out["email_count"] == 3
    assert [d["decision"] for d in out["decisions"]] == ["Launch cardless payments"]
    assert out["topic"]["family_total"] == 2
    assert get_topic_context(conn, "payments", family=False)["email_count"] == 2


def test_the_decision_filter_takes_each_family(conn):
    head = _tag(conn, "okapi", 1)
    synonym_email = f.email(conn, subject="Okapis", topics=("okapis",))
    f.decision(conn, "Feed the okapis", email_id=synonym_email)
    synonym = conn.execute("SELECT id FROM topics WHERE name = 'okapis'").fetchone()[0]
    tc.write_rows(conn, [(synonym, head, "fold", 1.0)])
    f.decision(conn, "Count the okapi", email_id=f.email(conn, subject="O", topics=("okapi",)))

    found = {d["decision"] for d in query.query_decisions(conn, topic="okapi")}

    assert found == {"Feed the okapis", "Count the okapi"}


def test_a_store_without_the_table_has_families_of_one(tmp_path):
    import sqlite3

    old = sqlite3.connect(tmp_path / "old.db")
    old.execute("CREATE TABLE topics (id INTEGER PRIMARY KEY, name TEXT, display_name TEXT)")

    assert tc.topic_family_ids(old, 7) == [7]
    assert tc.family_of_matching_sql(old) == "SELECT id FROM topics WHERE name LIKE ?"


def test_the_command_is_a_dry_run_by_default(tmp_path, monkeypatch, capsys):
    import sys

    from src import cli, config

    db = tmp_path / "brain.db"
    conn = create_database(str(db))
    _tag(conn, "credit card", 6)
    _tag(conn, "credit cards", 2)
    conn.close()
    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    monkeypatch.setattr(config, "is_replica", lambda: False)
    monkeypatch.setattr(sys, "argv", ["brain", "--db", str(db), "maint", "topic-canon"])
    cli.main()
    assert "dry run" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["brain", "--db", str(db), "maint", "topic-canon", "--apply"])
    cli.main()
    assert "'added': 1" in capsys.readouterr().out
