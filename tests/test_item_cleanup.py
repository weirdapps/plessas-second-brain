"""The one-off clean-ups of extracted items (src/store/item_cleanup.py): each counts by default
and writes only with --apply, and none is run against real data in a test.

- forget-itemless: the decisions, actions and commitments of news and automation mail;
- mark-boilerplate: key-fact text that recurs in five or more distinct threads;
- reextract-stacked: the bodies of emails holding both untagged and attachment-tagged facts,
  extracted again, their untagged items replaced, never deleted on attachment_id IS NULL
  alone.
"""

import json
import sys
from types import SimpleNamespace

import pytest

import src.config as config
from src.extract import claude_extract
from src.llm_cost import OverBudget, TokenBudget
from src.store import item_cleanup as ic
from src.store.schema import create_database

OWNER = "owner@example.com"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "USER_EMAIL_PATTERN", OWNER)
    conn = create_database(str(tmp_path / "brain.db"))
    yield conn
    conn.close()


def _email(conn, message_id, *, source_class="mail", conversation=None, days=1, content="Body."):
    return conn.execute(
        "INSERT INTO emails (message_id, date_received, subject, sender_name, sender_address,"
        " content, mailbox_name, conversation_id, source_class)"
        " VALUES (?, ?, 'Subject', 'Sam', 'sam@example.com', ?, 'Inbox', ?, ?)",
        (message_id, f"2026-10-{10 + days:02d}T09:00:00Z", content, conversation, source_class),
    ).lastrowid


def _count(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


# --- forget-itemless -------------------------------------------------------------------------


@pytest.fixture
def itemless(conn):
    ids = {
        "mail": _email(conn, "m", source_class="mail"),
        "news": _email(conn, "n", source_class="news"),
        "automation": _email(conn, "a", source_class="automation"),
    }
    for label, email_id in ids.items():
        conn.execute(
            "INSERT INTO decisions (email_id, decision) VALUES (?, ?)", (email_id, f"{label} d")
        )
        conn.execute(
            "INSERT INTO decisions (email_id, decision, attachment_id) VALUES (?, ?, 9)",
            (email_id, f"{label} attachment d"),
        )
        conn.execute(
            "INSERT INTO action_items (email_id, task) VALUES (?, ?)", (email_id, f"{label} a")
        )
        conn.execute(
            "INSERT INTO commitments (email_id, commitment) VALUES (?, ?)", (email_id, f"{label} c")
        )
        conn.execute(
            "INSERT INTO key_facts (email_id, fact) VALUES (?, ?)", (email_id, f"{label} f")
        )
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")  # a no-op inside a transaction
    conn.execute("INSERT INTO decisions (decision, teams_thread_id) VALUES ('teams d', 1)")
    conn.commit()
    return ids


def test_forget_itemless_counts_by_class_and_changes_nothing_by_default(conn, itemless):
    stats = ic.forget_itemless(conn)

    assert stats["applied"] is False
    assert stats["decisions"] == {"automation": 2, "news": 2}
    assert stats["action_items"] == {"automation": 1, "news": 1}
    assert stats["commitments"] == {"automation": 1, "news": 1}
    assert _count(conn, "SELECT COUNT(*) FROM decisions") == 7


def test_forget_itemless_applied_deletes_their_items_and_keeps_the_rest(conn, itemless):
    ic.forget_itemless(conn, apply=True)

    assert sorted(r[0] for r in conn.execute("SELECT decision FROM decisions")) == [
        "mail attachment d",
        "mail d",
        "teams d",
    ]
    assert [r[0] for r in conn.execute("SELECT task FROM action_items")] == ["mail a"]
    assert [r[0] for r in conn.execute("SELECT commitment FROM commitments")] == ["mail c"]
    assert _count(conn, "SELECT COUNT(*) FROM key_facts") == 3
    assert ic.forget_itemless(conn)["decisions"] == {}


# --- mark-boilerplate --------------------------------------------------------------------------


def test_fact_text_folds_accents_case_and_punctuation():
    assert ic.fact_text("Το e-mail είναι ΕΜΠΙΣΤΕΥΤΙΚΟ!") == ic.fact_text(
        "το e mail ειναι εμπιστευτικο"
    )
    assert ic.fact_key("...") is None
    assert ic.fact_key("Passcode: 123") != ic.fact_key("Passcode: 124")


@pytest.fixture
def repeated(conn):
    """A notice in five threads, a report line in four, a fact repeated five times in one."""
    notice = ["This e-mail is CONFIDENTIAL.", "this e mail is confidential"]
    for n in range(5):
        email_id = _email(conn, f"notice-{n}", conversation=f"conv-{n}")
        conn.execute(
            "INSERT INTO key_facts (email_id, fact) VALUES (?, ?)", (email_id, notice[n % 2])
        )
        # a second email of the same thread repeats it: still one thread
        twin = _email(conn, f"notice-{n}-re", conversation=f"conv-{n}")
        conn.execute("INSERT INTO key_facts (email_id, fact) VALUES (?, ?)", (twin, notice[0]))
    for n in range(4):
        email_id = _email(conn, f"report-{n}", conversation=f"report-{n}")
        conn.execute(
            "INSERT INTO key_facts (email_id, fact) VALUES (?, 'Out of cash: 0')", (email_id,)
        )
    one_thread = _email(conn, "budget", conversation="budget")
    for _ in range(5):
        conn.execute(
            "INSERT INTO key_facts (email_id, fact) VALUES (?, 'Budget: 40,000 euros')",
            (one_thread,),
        )
    conn.commit()


def test_mark_boilerplate_finds_text_in_five_threads_and_writes_nothing_by_default(conn, repeated):
    stats = ic.mark_boilerplate(conn)

    assert (stats["keys"], stats["rows"], stats["applied"]) == (1, 10, False)
    assert stats["commonest"][0][0] == 5
    assert _count(conn, "SELECT COUNT(*) FROM key_facts WHERE boilerplate = 1") == 0
    assert _count(conn, "SELECT COUNT(*) FROM boilerplate_fact_keys") == 0
    assert conn.execute("SELECT name FROM sqlite_temp_master WHERE type = 'table'").fetchall() == []


def test_mark_boilerplate_applied_marks_the_rows_and_keeps_the_key(conn, repeated):
    stats = ic.mark_boilerplate(conn, apply=True)

    assert stats["newly_marked"] == 10
    marked = {r[0] for r in conn.execute("SELECT fact FROM key_facts WHERE boilerplate = 1")}
    assert marked == {"This e-mail is CONFIDENTIAL.", "this e mail is confidential"}
    assert _count(conn, "SELECT COUNT(*) FROM key_facts") == 19  # nothing deleted
    assert ic.is_boilerplate(conn, "THIS E-MAIL IS CONFIDENTIAL")
    assert not ic.is_boilerplate(conn, "this email is confidential")  # another word
    assert not ic.is_boilerplate(conn, "Out of cash: 0")
    again = ic.mark_boilerplate(conn, apply=True)
    assert (again["newly_marked"], again["already_marked"]) == (0, 10)


def test_a_lower_threshold_takes_the_report_line_too(conn, repeated):
    assert ic.mark_boilerplate(conn, min_threads=4)["keys"] == 2


def test_teams_and_whatsapp_threads_count_as_threads(conn):
    conn.execute("PRAGMA foreign_keys = OFF")
    for n in range(3):
        conn.execute(
            "INSERT INTO key_facts (fact, teams_thread_id) VALUES ('Standup at 9', ?)", (n,)
        )
        conn.execute(
            "INSERT INTO key_facts (fact, whatsapp_thread_id) VALUES ('Standup at 9', ?)", (n,)
        )
    conn.commit()

    assert ic.mark_boilerplate(conn)["keys"] == 1


def test_one_thread_is_no_threshold(conn):
    with pytest.raises(ValueError):
        ic.mark_boilerplate(conn, min_threads=1)


# --- reextract-stacked ---------------------------------------------------------------------------


@pytest.fixture
def stacked(conn):
    """An email with untagged facts (its body's, and an old generation of its attachment's)
    beside a tagged one, and an older one alike; an email with untagged facts only; an email
    with tagged facts only."""
    ids = {
        "stacked": _email(conn, "s", days=5, content="We approved the pilot budget."),
        "body_only": _email(conn, "b", days=4),
        "tagged_only": _email(conn, "t", days=3),
        "older": _email(conn, "o", days=1, content="We approved the pilot budget."),
    }
    rows = [
        ("stacked", "body fact", None),
        ("stacked", "old attachment fact", None),
        ("stacked", "new attachment fact", 7),
        ("body_only", "body only fact", None),
        ("tagged_only", "tagged fact", 8),
        ("older", "older body fact", None),
        ("older", "older attachment fact", 9),
    ]
    for label, fact, attachment_id in rows:
        conn.execute(
            "INSERT INTO key_facts (email_id, fact, attachment_id) VALUES (?, ?, ?)",
            (ids[label], fact, attachment_id),
        )
    for label in ("stacked", "older"):
        conn.execute(
            "INSERT INTO decisions (email_id, decision) VALUES (?, 'old decision')", (ids[label],)
        )
        conn.execute(
            "INSERT INTO decisions (email_id, decision, attachment_id)"
            " VALUES (?, 'attachment decision', 7)",
            (ids[label],),
        )
        conn.execute(
            "INSERT INTO commitments (email_id, commitment) VALUES (?, 'old c')", (ids[label],)
        )
    conn.commit()
    return ids


def _response(text, tokens=(100, 10)):
    return SimpleNamespace(
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


def _reply():
    answer = {
        "item_kind": "work_thread",
        "language": "en",
        "summary": "s",
        "topics": ["pilot"],
        "sentiment": "informational",
        "urgency": "low",
        "decisions": [
            {
                "evidence": "We approved the pilot budget.",
                "decision": "Approve the pilot budget",
                "decided_by": "Sam",
                "owner_role": "owner_fyi",
                "status": "made",
            }
        ],
        "action_items": [],
        "commitments": [],
        "key_facts": ["new body fact"],
        "people": [],
    }
    return _response(json.dumps(answer))


@pytest.fixture
def model(monkeypatch, tmp_path):
    stub = SimpleNamespace(calls=[], answer=lambda kwargs: _reply())

    def create(client, *, after_refusal=None, **kwargs):
        stub.calls.append(kwargs)
        return stub.answer(kwargs)

    monkeypatch.setenv("BRAIN_EXTRACT_CALL_SHAPE", "v2-text")
    monkeypatch.setattr(claude_extract, "_get_client_and_model", lambda: (object(), "m"))
    monkeypatch.setattr("src.extract.vertex_fallback.create_with_refusal_fallback", create)
    monkeypatch.setattr(claude_extract, "USAGE_LOG_DIR", tmp_path)
    return stub


def test_only_emails_with_both_kinds_of_facts_are_taken_newest_first(conn, stacked):
    assert ic.stacked_emails(conn) == [stacked["stacked"], stacked["older"]]
    assert ic.stacked_emails(conn, limit=1) == [stacked["stacked"]]


def test_the_estimate_prices_the_calls_without_making_one(conn, stacked, model):
    est = ic.estimate_stacked(conn, ic.stacked_emails(conn))

    assert est["calls"] == 2
    assert est["system_prompt_tokens"] > 1000
    assert est["cost_usd"] > 0 and est["cost_usd_cached"] > 0
    assert model.calls == []


def test_the_body_is_extracted_again_and_its_untagged_items_replaced(
    conn, stacked, model, tmp_path
):
    stats = ic.reextract_stacked(conn, budget=TokenBudget(10**9), extracted_dir=tmp_path)

    assert (stats["done"], stats["failed"], stats["left"]) == (2, 0, 0)
    email_id = stacked["stacked"]
    facts = sorted(
        tuple(r)
        for r in conn.execute(
            "SELECT fact, attachment_id FROM key_facts WHERE email_id = ?", (email_id,)
        )
    )
    assert facts == [("new attachment fact", 7), ("new body fact", None)]
    decisions = sorted(
        (r[0], r[1], r[2])
        for r in conn.execute(
            "SELECT decision, COALESCE(status, ''), COALESCE(attachment_id, 0) FROM decisions"
            " WHERE email_id = ?",
            (email_id,),
        )
    )
    assert decisions == [("Approve the pilot budget", "made", 0), ("attachment decision", "", 7)]
    assert _count(conn, "SELECT COUNT(*) FROM commitments WHERE email_id = ?", email_id) == 0
    # The emails it did not select keep every row.
    assert _count(conn, "SELECT COUNT(*) FROM key_facts WHERE fact = 'body only fact'") == 1
    assert _count(conn, "SELECT COUNT(*) FROM key_facts WHERE fact = 'tagged fact'") == 1
    # Each call states the schema and sends no structured outputs.
    assert all("format" not in call["output_config"] for call in model.calls)
    assert len(list(tmp_path.glob("**/*.json"))) == 2


def test_a_second_run_takes_nothing_it_has_done(conn, stacked, model):
    ic.reextract_stacked(conn, budget=TokenBudget(10**9))
    calls = len(model.calls)

    stats = ic.reextract_stacked(conn, budget=TokenBudget(10**9))

    assert (stats["selected"], len(model.calls)) == (0, calls)


def test_the_budget_stops_the_run_and_the_next_run_resumes(conn, stacked, model):
    first = ic.reextract_stacked(conn, budget=TokenBudget(10**9), limit=1)
    assert (first["done"], first["left"]) == (1, 1)

    stopped = ic.reextract_stacked(conn, budget=TokenBudget(10))

    assert stopped["stop_kind"] == "budget"
    assert stopped["done"] == 0
    # The email the budget refused keeps its rows as they were.
    older = stacked["older"]
    assert _count(conn, "SELECT COUNT(*) FROM key_facts WHERE email_id = ?", older) == 2
    assert ic.stacked_emails(conn) == [older]


def test_an_unusable_reply_leaves_the_email_as_it_was_and_counts_an_attempt(conn, stacked, model):
    model.answer = lambda kwargs: _response("no json here", tokens=(1, 1))

    for _ in range(ic.MAX_ATTEMPTS):
        stats = ic.reextract_stacked(conn, budget=TokenBudget(10**9))
        assert stats["failed"] == 2

    assert _count(conn, "SELECT COUNT(*) FROM key_facts WHERE fact = 'old attachment fact'") == 1
    assert ic.stacked_emails(conn) == []  # three failures each: no longer offered


def test_a_service_failure_stops_the_run_without_counting_an_attempt(conn, stacked, model):
    """An unset model, region or credential stands for any failure of the service: no item
    is at fault (policy_bridge.is_transient), so none is marked."""

    def down(kwargs):
        raise claude_extract.ClaudeNotConfigured("no model set")

    model.answer = down

    stats = ic.reextract_stacked(conn, budget=TokenBudget(10**9))

    assert stats["stop_kind"] == "service"
    assert ic.stacked_emails(conn) == [stacked["stacked"], stacked["older"]]
    assert _count(conn, "SELECT COUNT(*) FROM key_facts WHERE fact = 'old attachment fact'") == 1


def test_over_budget_is_what_extract_one_raises_for_a_call_that_does_not_fit(model):
    email = {
        "message_id": "x",
        "date_received": "2026-10-01T00:00:00Z",
        "sender": {"name": "Sam", "address": "sam@example.com"},
        "to_recipients": [],
        "cc_recipients": [],
        "subject": "s",
        "content": "c",
    }

    with pytest.raises(OverBudget):
        claude_extract.extract_one(email, budget=TokenBudget(10))
    assert model.calls == []


def test_extract_one_charges_the_budget_what_each_call_reports(model):
    email = {
        "message_id": "x",
        "date_received": "2026-10-01T00:00:00Z",
        "sender": {"name": "Sam", "address": "sam@example.com"},
        "to_recipients": [],
        "cc_recipients": [],
        "subject": "s",
        "content": "We approved the pilot budget.",
    }
    budget = TokenBudget(10**9)

    claude_extract.extract_one(email, budget=budget)

    assert (budget.spent, budget.calls) == (110, 1)


# --- the commands ------------------------------------------------------------------------------


def _run(monkeypatch, db, *argv):
    from src import cli

    monkeypatch.setattr(sys, "argv", ["brain", "--db", str(db), "maint", *argv])
    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    monkeypatch.setattr(config, "is_replica", lambda: False)
    try:
        cli.main()
    except SystemExit as exit_info:
        return exit_info.code
    return 0


def test_the_commands_are_dry_runs_by_default(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "USER_EMAIL_PATTERN", OWNER)
    db = tmp_path / "brain.db"
    conn = create_database(str(db))
    email_id = _email(conn, "n", source_class="news")
    conn.execute("INSERT INTO decisions (email_id, decision) VALUES (?, 'd')", (email_id,))
    conn.commit()
    conn.close()

    assert _run(monkeypatch, db, "forget-itemless") == 0
    assert "decisions: 1 to delete" in capsys.readouterr().out
    assert _run(monkeypatch, db, "mark-boilerplate") == 0
    assert "dry run" in capsys.readouterr().out
    assert _run(monkeypatch, db, "reextract-stacked") == 0
    assert "dry run" in capsys.readouterr().out
    assert _run(monkeypatch, db, "forget-itemless", "--apply") == 0
    assert "decisions: 1 deleted" in capsys.readouterr().out


def test_reextract_stacked_apply_needs_a_token_budget(tmp_path, monkeypatch, capsys):
    db = tmp_path / "brain.db"
    create_database(str(db)).close()

    assert _run(monkeypatch, db, "reextract-stacked", "--apply") == 2
    assert "--token-budget" in capsys.readouterr().err


def test_the_commands_refuse_a_replica(tmp_path, monkeypatch):
    from src import cli

    db = tmp_path / "brain.db"
    create_database(str(db)).close()
    monkeypatch.setattr(sys, "argv", ["brain", "--db", str(db), "maint", "forget-itemless"])
    monkeypatch.setattr(config, "is_replica", lambda: True)

    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code == 2
