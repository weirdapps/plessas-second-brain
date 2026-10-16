"""`brain topic-pages` and `brain briefs`: what each runs, and where each may run.

Both write or mail only on the producer. Their read-only halves (`topic-pages --estimate`,
`briefs --dry-run`) run on a replica, so a Mac can price the pages or list the briefs
from its pulled copy.
"""

import json
from types import SimpleNamespace

import pytest

from src import cli
from src.store.schema import create_database
from tests import factories as f
from tests.brain_cli import brain


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    email_id = f.email(conn, subject="Okapi plan", topics=["okapi budget"])
    f.decision(conn, "Adopt the okapi plan", email_id=email_id)
    conn.close()
    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    monkeypatch.setenv("BRAIN_TOPIC_PAGES", "okapi budget")
    return path, email_id


def _model(monkeypatch, email_id, calls):
    from src.extract import claude_extract

    def fake(**kwargs):
        calls.append(kwargs)
        payload = {"status": [{"text": "Adopted", "refs": [f"email:{email_id}"]}]}
        return SimpleNamespace(
            content=[SimpleNamespace(text=json.dumps(payload))],
            model="claude-sonnet-5-5",
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=100, output_tokens=10),
        )

    monkeypatch.setattr(claude_extract, "complete", fake)


def test_the_nightly_job_builds_the_named_page(db, monkeypatch, capsys):
    path, email_id = db
    calls: list = []
    _model(monkeypatch, email_id, calls)
    monkeypatch.setenv("BRAIN_ROLE", "producer")

    code, out, _ = brain(monkeypatch, capsys, "--db", str(path), "topic-pages")

    assert code == 0 and len(calls) == 1
    assert "built 1" in out


def test_the_estimate_runs_on_a_replica_and_calls_nothing(db, monkeypatch, capsys):
    path, email_id = db
    calls: list = []
    _model(monkeypatch, email_id, calls)
    monkeypatch.setenv("BRAIN_ROLE", "replica")

    code, out, _ = brain(monkeypatch, capsys, "--db", str(path), "topic-pages", "--estimate")

    assert code == 0 and calls == []
    assert "to_build 1" in out and "cost_usd" in out


@pytest.mark.parametrize("argv", [["topic-pages"], ["maint", "topic-pages"], ["briefs"]])
def test_a_replica_refuses_the_writing_half(db, monkeypatch, capsys, argv):
    path, _ = db
    monkeypatch.setenv("BRAIN_ROLE", "replica")

    code, _, err = brain(monkeypatch, capsys, "--db", str(path), *argv)

    assert code == 2 and "replica" in err


def test_maint_topic_pages_builds_a_clean_page_again(db, monkeypatch, capsys):
    path, email_id = db
    calls: list = []
    _model(monkeypatch, email_id, calls)
    monkeypatch.setenv("BRAIN_ROLE", "producer")
    brain(monkeypatch, capsys, "--db", str(path), "topic-pages")

    brain(monkeypatch, capsys, "--db", str(path), "topic-pages")
    assert len(calls) == 1
    code, _, _ = brain(monkeypatch, capsys, "--db", str(path), "maint", "topic-pages")

    assert code == 0 and len(calls) == 2


def test_the_findings_review_runs_on_a_replica(db, monkeypatch, capsys):
    path, _ = db
    monkeypatch.setenv("BRAIN_ROLE", "replica")

    code, out, _ = brain(monkeypatch, capsys, "--db", str(path), "findings")

    assert code == 0 and out.startswith("findings: none held")


def test_briefs_dry_run_runs_on_a_replica(db, monkeypatch, capsys):
    path, _ = db
    monkeypatch.setenv("BRAIN_ROLE", "replica")
    monkeypatch.setenv("BRAIN_BRIEF_PILOT_UNTIL", "2999-01-01")

    code, out, _ = brain(monkeypatch, capsys, "--db", str(path), "briefs", "--dry-run")

    assert code == 0 and "upcoming 0" in out
