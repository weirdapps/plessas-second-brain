"""The consolidation pass: duplicates by rule, labels by a stubbed model, never a delete.

Every value is invented: example.com addresses and made-up names.
"""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from src.extract import claude_extract
from src.llm_cost import TokenBudget
from src.store import consolidate as c
from src.store.schema import create_database
from tests import factories as f

ALICE = ("Alice Smith", "alice@example.com")
BOB = ("Bob Jones", "bob@example.com")


@pytest.fixture
def conn(tmp_path):
    db = create_database(str(tmp_path / "brain.db"))
    yield db
    db.close()


def _thread(conn, n=2, subject="Okapi plan", **columns):
    """n emails of one conversation, a day apart, oldest first."""
    ids = []
    for i in range(n):
        ids.append(
            f.email(
                conn,
                subject=("Re: " if i else "") + subject,
                sender=ALICE if i % 2 == 0 else BOB,
                to=(BOB,) if i % 2 == 0 else (ALICE,),
                conversation_id="conv-okapi",
                date_received=f.ago(days=10 - i),
                summary=f"Message {i} about the okapi plan",
                **columns,
            )
        )
    return ids


def _fact(conn, email_id, text):
    cur = conn.execute("INSERT INTO key_facts (email_id, fact) VALUES (?, ?)", (email_id, text))
    conn.commit()
    return cur.lastrowid


def _commitment(conn, email_id, text, **columns):
    values = {"email_id": email_id, "commitment": text, **columns}
    cols = ", ".join(values)
    cur = conn.execute(
        f"INSERT INTO commitments ({cols}) VALUES ({', '.join('?' * len(values))})",
        tuple(values.values()),
    )
    conn.commit()
    return cur.lastrowid


def _row(conn, table, item_id):
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return dict(cur.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone())


def _start(conn):
    """The first hourly run records the high-water mark and takes nothing."""
    stats = c.consolidate(conn, model=False)
    assert stats.started_from_scratch


# --- current state -----------------------------------------------------------------------


def test_every_item_is_current_until_a_pass_marks_it(conn):
    first, second = _thread(conn)
    a = f.decision(conn, "Adopt the okapi plan", email_id=first)
    b = f.decision(conn, "Adopt the okapi plan", email_id=second)

    current = c.current_sql(conn, "d", "decisions")
    rows = conn.execute(f"SELECT d.id FROM decisions d WHERE {current} ORDER BY d.id").fetchall()

    assert [r[0] for r in rows] == [a, b]


def test_a_duplicate_a_superseded_and_an_invalid_item_are_not_current(conn):
    (email_id,) = _thread(conn, n=1)
    keep, dup, old, gone = (
        f.decision(conn, text, email_id=email_id) for text in ("keep", "dup", "old", "gone")
    )
    conn.execute("UPDATE decisions SET canonical_id = ? WHERE id = ?", (keep, dup))
    conn.execute(
        "UPDATE decisions SET superseded_by = ?, invalid_at = 'x' WHERE id = ?", (keep, old)
    )
    conn.execute("UPDATE decisions SET invalid_at = '2026-01-01' WHERE id = ?", (gone,))

    current = c.current_sql(conn, "d", "decisions")
    ids = [r[0] for r in conn.execute(f"SELECT d.id FROM decisions d WHERE {current}")]

    assert ids == [keep]
    assert c.hidden_ids(conn, "decisions", [keep, dup, old, gone]) == {dup, old, gone}
    everything = c.current_sql(conn, "d", "decisions", include_history=True)
    assert everything == "1"


def test_a_pointer_to_a_deleted_row_counts_as_none(conn):
    """An email extracted again loses its rows; what pointed at them shows again."""
    (email_id,) = _thread(conn, n=1)
    dup = f.decision(conn, "dup", email_id=email_id)
    conn.execute("UPDATE decisions SET canonical_id = 99999 WHERE id = ?", (dup,))
    old = f.decision(conn, "old", email_id=email_id)
    conn.execute(
        "UPDATE decisions SET superseded_by = 99998, invalid_at = 'x' WHERE id = ?", (old,)
    )

    assert c.hidden_ids(conn, "decisions", [dup, old]) == set()
    assert c.repair_pointers(conn) == 2
    assert _row(conn, "decisions", old)["invalid_at"] is None


def test_a_store_without_the_state_columns_shows_everything(tmp_path):
    old = sqlite3.connect(tmp_path / "old.db")
    old.execute("CREATE TABLE decisions (id INTEGER PRIMARY KEY, decision TEXT)")

    assert c.current_sql(old, "d", "decisions") == "1"
    assert c.hidden_ids(old, "decisions", [1]) == set()


def test_recall_filters_name_the_aliases_recall_uses(conn):
    filters = c.recall_filters(conn)

    assert "d.canonical_id" in filters["decisions"]
    assert "a.canonical_id" in filters["actions"]
    assert "c.canonical_id" in filters["commitments"]
    assert set(c.recall_filters(conn, include_history=True).values()) == {"1"}


# --- step 1: rules -------------------------------------------------------------------------


def test_a_reply_that_repeats_an_earlier_item_points_at_it(conn):
    first, second = _thread(conn)
    original = f.decision(conn, "Adopt the okapi plan for the pilot branches", email_id=first)
    copy = f.decision(conn, "Adopt the okapi plan for pilot branches", email_id=second)
    fact = _fact(conn, first, "The okapi pilot costs 5000 euros")
    fact_copy = _fact(conn, second, "The okapi pilot costs 5000 euros.")
    _start(conn)
    conn.execute("DELETE FROM sync_metadata WHERE key = ?", (c.HIGH_WATER_KEY,))
    c._set_high_water(conn, dict.fromkeys(c.STATE_TABLES, 0))
    conn.commit()

    stats = c.consolidate(conn, model=False)

    assert _row(conn, "decisions", copy)["canonical_id"] == original
    assert _row(conn, "decisions", original)["canonical_id"] is None
    assert _row(conn, "key_facts", fact_copy)["canonical_id"] == fact
    assert stats.rule_labels == 2
    logged = conn.execute(
        "SELECT item_table, item_id, label, target_id, method FROM item_labels ORDER BY id"
    ).fetchall()
    assert [tuple(r) for r in logged] == [
        ("decisions", copy, "duplicate_of", original, "rule"),
        ("key_facts", fact_copy, "duplicate_of", fact, "rule"),
    ]


def _backfill(conn, **kwargs):
    return c.consolidate(conn, since=f.ago(days=365)[:10], **kwargs)


def test_a_new_value_is_not_a_duplicate_but_a_hint(conn):
    first, second = _thread(conn)
    old = _fact(conn, first, "Okapi pilot budget is 5000 euros")
    new = _fact(conn, second, "Okapi pilot budget is 7000 euros")

    _backfill(conn, model=False)

    assert _row(conn, "key_facts", new)["canonical_id"] is None
    thread = c.load_thread(conn, "email:conv-okapi")
    _, hints = c.plan_rules(thread)
    assert [(a.id, b.id) for a, b in hints] == [(old, new)]


def test_a_proposal_is_never_folded_into_a_decision_made(conn):
    first, second = _thread(conn)
    proposed = f.decision(conn, "Adopt the okapi plan", email_id=first, status="proposed")
    made = f.decision(conn, "Adopt the okapi plan", email_id=second, status="made")

    _backfill(conn, model=False)

    assert _row(conn, "decisions", proposed)["canonical_id"] is None
    assert _row(conn, "decisions", made)["canonical_id"] is None


def test_a_cluster_keeps_the_most_settled_item(conn):
    """An open copy outranks an expired original: the action is still owed."""
    first, second = _thread(conn)
    expired = f.action_item(conn, "Send the okapi deck", email_id=first, status="expired")
    still_open = f.action_item(conn, "Send the okapi deck", email_id=second, status="open")

    _backfill(conn, model=False)

    assert _row(conn, "action_items", expired)["canonical_id"] == still_open
    assert _row(conn, "action_items", still_open)["canonical_id"] is None


def test_two_owners_or_two_deadlines_are_two_actions(conn):
    first, second = _thread(conn)
    a = f.action_item(conn, "Send the okapi deck", email_id=first, owner="Alice Smith")
    b = f.action_item(conn, "Send the okapi deck", email_id=second, owner="Bob Jones")
    d1 = f.action_item(conn, "Book the room", email_id=first, owner=None, deadline="2026-11-01")
    d2 = f.action_item(conn, "Book the room", email_id=second, owner=None, deadline="2026-11-08")

    _backfill(conn, model=False)

    assert all(_row(conn, "action_items", i)["canonical_id"] is None for i in (a, b, d1, d2))


def test_a_later_copy_of_a_copy_points_at_the_original(conn):
    first, second, third = _thread(conn, n=3)
    original = f.decision(conn, "Adopt the okapi plan", email_id=first)
    f.decision(conn, "Adopt the okapi plan", email_id=second)
    _backfill(conn, model=False)
    late = f.decision(conn, "Adopt the okapi plan", email_id=third)

    _backfill(conn, model=False)

    assert _row(conn, "decisions", late)["canonical_id"] == original


def test_running_again_on_the_same_items_writes_nothing(conn):
    first, second = _thread(conn)
    f.decision(conn, "Adopt the okapi plan", email_id=first)
    f.decision(conn, "Adopt the okapi plan", email_id=second)
    _backfill(conn, model=False)
    labels = conn.execute("SELECT COUNT(*) FROM item_labels").fetchone()[0]

    again = _backfill(conn, model=False)

    assert again.threads == 0
    assert conn.execute("SELECT COUNT(*) FROM item_labels").fetchone()[0] == labels


def test_news_and_automation_threads_are_left_alone(conn):
    first, second = _thread(conn, source_class="automation")
    f.decision(conn, "Adopt the okapi plan", email_id=first)
    f.decision(conn, "Adopt the okapi plan", email_id=second)

    stats = _backfill(conn, model=False)

    assert stats.threads == 0


def test_a_teams_thread_is_a_thread(conn):
    thread_id = f.teams_thread(conn)
    a = f.decision(conn, "Adopt the okapi plan", teams_thread_id=thread_id)
    b = f.decision(conn, "Adopt the okapi plan", teams_thread_id=thread_id)
    conn.execute("UPDATE teams_threads SET ended_at = ? WHERE id = ?", (f.ago(days=1), thread_id))
    conn.commit()

    _backfill(conn, model=False)

    assert _row(conn, "decisions", b)["canonical_id"] == a


def test_the_pass_never_deletes(conn):
    first, second = _thread(conn)
    f.decision(conn, "Adopt the okapi plan", email_id=first)
    f.decision(conn, "Adopt the okapi plan", email_id=second)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    _backfill(conn, model=False)

    conn.set_trace_callback(None)
    assert not any(s.strip().upper().startswith("DELETE") for s in statements)
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 2


def test_undo_puts_back_what_a_run_replaced(conn):
    first, second = _thread(conn)
    f.decision(conn, "Adopt the okapi plan", email_id=first)
    copy = f.decision(conn, "Adopt the okapi plan", email_id=second)
    stats = _backfill(conn, model=False)

    assert c.revert(conn, stats.run_id) == 1
    assert _row(conn, "decisions", copy)["canonical_id"] is None
    assert conn.execute("SELECT undone_at IS NOT NULL FROM item_labels").fetchone()[0] == 1


# --- the hourly mark ----------------------------------------------------------------------


def test_the_hourly_pass_takes_only_threads_with_new_items(conn):
    first, second = _thread(conn)
    f.decision(conn, "Adopt the okapi plan", email_id=first)
    old_copy = f.decision(conn, "Adopt the okapi plan", email_id=second)
    _start(conn)  # the mark: nothing before it is taken
    other = f.email(conn, subject="Zebra plan", conversation_id="conv-zebra")
    z1 = f.decision(conn, "Paint the zebra crossing", email_id=other)
    z2 = f.decision(conn, "Paint the zebra crossing", email_id=other)

    stats = c.consolidate(conn, model=False)

    assert stats.threads == 1
    assert _row(conn, "decisions", z2)["canonical_id"] == z1
    assert _row(conn, "decisions", old_copy)["canonical_id"] is None
    assert c.consolidate(conn, model=False).threads == 0


def test_an_hourly_run_out_of_time_leaves_the_mark_for_the_next(conn):
    _start(conn)
    first, second = _thread(conn)
    f.decision(conn, "Adopt the okapi plan", email_id=first)
    copy = f.decision(conn, "Adopt the okapi plan", email_id=second)

    stopped = c.consolidate(conn, model=False, deadline_s=-1)

    assert stopped.stop_kind == "deadline"
    assert c.consolidate(conn, model=False).threads == 1
    assert _row(conn, "decisions", copy)["canonical_id"] is not None


# --- step 2: the model --------------------------------------------------------------------


def _response(text, tokens=(800, 120)):
    return SimpleNamespace(
        id="msg_test_reply",
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        stop_details=None,
        model="claude-sonnet-5-5",
        usage=SimpleNamespace(
            input_tokens=tokens[0],
            output_tokens=tokens[1],
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


@pytest.fixture
def model(monkeypatch, tmp_path):
    stub = SimpleNamespace(calls=[], labels=[])

    def create(client, *, after_refusal=None, **kwargs):
        stub.calls.append(kwargs)
        user = kwargs["messages"][0]["content"]
        labels = stub.labels(user) if callable(stub.labels) else stub.labels
        return _response(json.dumps({"labels": labels}))

    monkeypatch.setattr(claude_extract, "_get_client_and_model", lambda: (object(), "m"))
    monkeypatch.setattr("src.extract.vertex_fallback.create_with_refusal_fallback", create)
    monkeypatch.setattr(claude_extract, "USAGE_LOG_DIR", tmp_path)
    return stub


def _handle(user, text):
    """The handle the prompt gave the line holding `text`."""
    for line in user.splitlines():
        if text in line:
            return line.split()[0]
    raise AssertionError(f"{text!r} is not in the prompt")


def test_a_later_message_completes_an_action_with_its_words(conn, model, monkeypatch):
    from src import config

    monkeypatch.setattr(config, "USER_EMAIL_PATTERN", BOB[1])  # Bob is the owner
    first, second = _thread(conn)
    conn.execute(
        "UPDATE emails SET summary = 'Bob sent the okapi deck as asked' WHERE id = ?", (second,)
    )
    action = f.action_item(conn, "Send the okapi deck to Alice", email_id=first, owner="Bob Jones")
    model.labels = lambda user: [
        {
            "item": _handle(user, "Send the okapi deck"),
            "label": "completed_by",
            "target": "M2",
            "evidence": "Bob sent the okapi deck",
        }
    ]

    stats = _backfill(conn, budget=TokenBudget(100_000))

    row = _row(conn, "action_items", action)
    assert (row["status"], row["closed_by"]) == ("done", f"email:{second}")
    assert (
        row["closed_at"]
        == conn.execute("SELECT date_received FROM emails WHERE id = ?", (second,)).fetchone()[0]
    )
    assert stats.calls == 1 and stats.by_label["completed_by"] == 1
    evidence = conn.execute(
        "SELECT evidence, method, prompt_hash, reply_id FROM item_labels"
        " WHERE label = 'completed_by'"
    ).fetchone()
    assert tuple(evidence)[:2] == ("Bob sent the okapi deck", "model")
    assert len(evidence[2]) == 16 and evidence[3] == "msg_test_reply"
    sent = model.calls[0]
    assert sent["output_config"] == {"effort": "low"}
    assert "untrusted_" in sent["messages"][0]["content"]


def test_a_completion_whose_quote_is_not_in_the_message_is_dropped(conn, model):
    first, second = _thread(conn)
    action = f.action_item(conn, "Send the okapi deck", email_id=first)
    model.labels = lambda user: [
        {
            "item": _handle(user, "Send the okapi deck"),
            "label": "completed_by",
            "target": "M2",
            "evidence": "the deck went out on Friday afternoon",
        }
    ]

    _backfill(conn, budget=TokenBudget(100_000))

    assert _row(conn, "action_items", action)["status"] == "open"


def test_a_message_from_before_the_item_completes_nothing(conn, model):
    first, second = _thread(conn)
    conn.execute("UPDATE emails SET summary = 'deck sent already' WHERE id = ?", (first,))
    action = f.action_item(conn, "Send the okapi deck", email_id=second)
    model.labels = lambda user: [
        {
            "item": _handle(user, "Send the okapi deck"),
            "label": "completed_by",
            "target": "M1",
            "evidence": "deck sent already",
        }
    ]

    _backfill(conn, budget=TokenBudget(100_000))

    assert _row(conn, "action_items", action)["status"] == "open"


def test_a_commitment_can_close(conn, model):
    """Bob promised in his own email; his later email shows it done."""
    _ask, promised, booked = _thread(conn, n=3)
    conn.execute(
        "UPDATE emails SET sender_name = ?, sender_address = ?, summary = ? WHERE id = ?",
        (*BOB, "Bob booked the okapi venue", booked),
    )
    conn.commit()
    bob = conn.execute("SELECT id FROM people WHERE email = ?", (BOB[1],)).fetchone()[0]
    promise = _commitment(
        conn, promised, "Bob will book the okapi venue", by_person="Bob Jones", by_person_id=bob
    )
    model.labels = lambda user: [
        {
            "item": _handle(user, "book the okapi venue"),
            "label": "completed_by",
            "target": "M3",
            "evidence": "Bob booked the okapi venue",
        }
    ]

    _backfill(conn, budget=TokenBudget(100_000))

    row = _row(conn, "commitments", promise)
    assert (row["status"], row["closed_by"]) == ("done", f"email:{booked}")


def test_a_newer_value_supersedes_the_older_one(conn, model):
    """Alice, who opened the thread, restates Bob's figure with a new value."""
    _opening, first, second = _thread(conn, n=3)
    f.action_item(conn, "Approve the okapi budget", email_id=first)  # opens the gate
    old = _fact(conn, first, "Okapi pilot budget is 5000 euros")
    new = _fact(conn, second, "Okapi pilot budget is 7000 euros")

    def labels(user):
        assert "ALIKE" in user
        return [
            {
                "item": _handle(user, "5000 euros"),
                "label": "superseded_by",
                "target": _handle(user, "7000 euros"),
                "evidence": "budget is 7000 euros",
            }
        ]

    model.labels = labels
    _backfill(conn, budget=TokenBudget(100_000))

    row = _row(conn, "key_facts", old)
    assert row["superseded_by"] == new
    assert (
        row["invalid_at"]
        == conn.execute("SELECT date_received FROM emails WHERE id = ?", (second,)).fetchone()[0]
    )
    assert c.hidden_ids(conn, "key_facts", [old, new]) == {old}


def test_a_proposal_never_supersedes_a_decision_made(conn, model):
    first, second = _thread(conn)
    made = f.decision(conn, "We adopt the okapi plan", email_id=first, status="made")
    f.decision(conn, "We could adopt the zebra plan instead", email_id=second, status="proposed")
    model.labels = lambda user: [
        {
            "item": _handle(user, "okapi plan"),
            "label": "superseded_by",
            "target": _handle(user, "zebra plan"),
        }
    ]

    _backfill(conn, budget=TokenBudget(100_000))

    assert _row(conn, "decisions", made)["superseded_by"] is None


def test_a_label_naming_an_unknown_handle_or_a_cycle_is_dropped(conn, model):
    first, second = _thread(conn)
    a = f.decision(conn, "Adopt the okapi plan", email_id=first)
    b = f.decision(conn, "We will adopt the okapi rollout plan for all branches", email_id=second)
    model.labels = lambda user: [
        {"item": "D99", "label": "duplicate_of", "target": "D1"},
        {"item": "D2", "label": "duplicate_of", "target": "D1"},
        {"item": "D1", "label": "duplicate_of", "target": "D2"},
        {"item": "D1", "label": "deleted", "target": "D2"},
    ]

    _backfill(conn, budget=TokenBudget(100_000))

    assert _row(conn, "decisions", b)["canonical_id"] == a
    assert _row(conn, "decisions", a)["canonical_id"] is None


def test_a_thread_of_one_message_or_with_nothing_open_gets_no_call(conn, model):
    (alone,) = _thread(conn, n=1, subject="Lone note")
    f.action_item(conn, "Send the okapi deck", email_id=alone)
    first, second = _thread(conn, subject="Facts only")
    conn.execute(
        "UPDATE emails SET conversation_id = 'conv-facts' WHERE id IN (?, ?)", (first, second)
    )
    _fact(conn, first, "The okapi pilot has 4 branches")
    model.labels = []

    stats = _backfill(conn, budget=TokenBudget(100_000))

    assert model.calls == []
    assert stats.gated == 0


def test_a_thread_already_called_on_these_items_is_not_called_again(conn, model):
    first, second = _thread(conn)
    f.action_item(conn, "Send the okapi deck", email_id=first)
    model.labels = []
    _backfill(conn, budget=TokenBudget(100_000))

    _backfill(conn, budget=TokenBudget(100_000))

    assert len(model.calls) == 1


def test_the_budget_stops_the_calls(conn, model):
    first, second = _thread(conn)
    f.action_item(conn, "Send the okapi deck", email_id=first)
    model.labels = []

    stats = _backfill(conn, budget=TokenBudget(10))

    assert model.calls == []
    assert stats.stop_kind == "budget"
    assert conn.execute("SELECT model_state FROM consolidation_threads").fetchone()[0] == "backlog"


def test_a_reply_with_no_json_counts_an_attempt(conn, model, monkeypatch):
    first, second = _thread(conn)
    f.action_item(conn, "Send the okapi deck", email_id=first)
    monkeypatch.setattr(
        "src.extract.vertex_fallback.create_with_refusal_fallback",
        lambda client, **kw: _response("no labels today"),
    )

    stats = _backfill(conn, budget=TokenBudget(100_000))

    assert stats.failed == 1
    assert conn.execute("SELECT attempts FROM consolidation_threads").fetchone()[0] == 1


def test_the_hourly_queue_takes_pending_threads_only(conn, model):
    _start(conn)
    first, second = _thread(conn)
    f.action_item(conn, "Send the okapi deck", email_id=first)
    model.labels = []

    stats = c.consolidate(conn, budget=TokenBudget(100_000))

    assert stats.calls == 1
    state = conn.execute(
        "SELECT model_state, model_signature FROM consolidation_threads"
    ).fetchone()
    assert state[0] == "done" and state[1]


# --- the estimate --------------------------------------------------------------------------


def test_the_estimate_prices_the_calls_without_making_one_or_writing(conn, model):
    first, second = _thread(conn)
    f.action_item(conn, "Send the okapi deck", email_id=first)
    f.decision(conn, "Adopt the okapi plan", email_id=first)
    f.decision(conn, "Adopt the okapi plan", email_id=second)

    est = c.estimate(conn, since=f.ago(days=365)[:10])

    assert est["threads"] == 1 and est["calls"] == 1 and est["rule_labels"] == 1
    assert est["input_tokens"] > 100 and est["cost_usd"] > 0
    assert model.calls == []
    assert conn.execute("SELECT COUNT(*) FROM item_labels").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM consolidation_threads").fetchone()[0] == 0


# --- the command ----------------------------------------------------------------------------


def _run(monkeypatch, db, *argv):
    import sys

    from src import cli, config

    monkeypatch.setattr(sys, "argv", ["brain", "--db", str(db), "maint", "consolidate", *argv])
    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    monkeypatch.setattr(config, "is_replica", lambda: False)
    try:
        cli.main()
    except SystemExit as e:
        return e.code
    return 0


def test_the_command_is_a_dry_run_by_default_and_needs_a_budget(tmp_path, monkeypatch, capsys):
    db = tmp_path / "brain.db"
    conn = create_database(str(db))
    first, second = _thread(conn)
    f.decision(conn, "Adopt the okapi plan", email_id=first)
    f.decision(conn, "Adopt the okapi plan", email_id=second)
    conn.close()

    assert _run(monkeypatch, db) == 0
    assert "dry run" in capsys.readouterr().out
    assert _run(monkeypatch, db, "--apply") == 2
    assert _run(monkeypatch, db, "--apply", "--rules-only") == 0
    out = capsys.readouterr().out
    assert "1 duplicates" in out
    run_id = out.split()[1].rstrip(":")
    assert _run(monkeypatch, db, "--revert", run_id) == 0
    assert "reverted 1 labels" in capsys.readouterr().out


# --- the hourly hook ------------------------------------------------------------------------


def test_the_hourly_sync_runs_the_pass_within_its_slice():
    import inspect

    from src.pipeline import sync

    source = inspect.getsource(sync.run)

    assert "run_in_sync(db_path, CONSOLIDATE_SYNC_DEADLINE_S)" in source
    assert isinstance(sync.CONSOLIDATE_SYNC_DEADLINE_S, float)


def test_run_in_sync_starts_at_the_mark_and_with_no_tokens_calls_nothing(
    tmp_path, monkeypatch, model
):
    db = tmp_path / "brain.db"
    conn = create_database(str(db))
    conn.close()
    monkeypatch.setenv(c.SYNC_TOKENS_VAR, "0")

    first = c.run_in_sync(str(db), 60.0)

    assert first.started_from_scratch
    store = sqlite3.connect(db)
    a, b = _thread(store)
    f.action_item(store, "Send the okapi deck", email_id=a)
    f.action_item(store, "Send the okapi deck", email_id=b)
    store.close()

    second = c.run_in_sync(str(db), 60.0)

    assert second.threads == 1 and second.rule_labels == 1
    assert second.calls == 0 and model.calls == []
