"""process-attachments and reextract take a token budget, and price their work before any spend.

The nightly pass runs `process-attachments --phase 2`; with BRAIN_ATTACHMENT_TOKEN_BUDGET set it
stops before the call that would pass the budget, leaves the rest pending and still exits 0, so
run_stage in sb-attachment-pass.sh does not read a planned stop as a failed stage.
"""

import argparse
import re
import sqlite3
import sys

import pytest

from src import cli
from src.extract import claude_extract
from src.llm_cost import TokenBudget
from src.store.schema import create_database


@pytest.fixture
def phase2(tmp_path, monkeypatch):
    """process-attachments --phase 2 with Phase 2 stubbed; returns (args, what Phase 2 got)."""
    db_path = tmp_path / "brain.db"
    create_database(str(db_path)).close()
    seen: dict = {}

    def _phase1(*_a, **_k):
        raise AssertionError("phase 1 must not run")

    def _phase2(db_path, **kw):
        seen.update(kw, ran=True)
        return {"processed": 2, "extracted": 2, "failed": 0, "deferred": 0, "over_budget": 5}

    monkeypatch.setattr("src.extract.attachment_pipeline.run_phase1", _phase1)
    monkeypatch.setattr("src.extract.attachment_pipeline.run_phase2", _phase2)
    monkeypatch.delenv("BRAIN_ATTACHMENT_TOKEN_BUDGET", raising=False)
    args = argparse.Namespace(
        db=db_path,
        phase=2,
        type=None,
        limit=0,
        workers=1,
        deadline_s=None,
        token_budget=None,
        estimate=False,
    )
    return args, seen


def test_the_flag_sets_the_budget(phase2):
    args, seen = phase2
    args.token_budget = 50_000

    assert cli.cmd_process_attachments(args) == 0

    assert isinstance(seen["token_budget"], TokenBudget)
    assert seen["token_budget"].limit == 50_000


def test_the_environment_sets_it_when_the_flag_does_not(phase2, monkeypatch):
    args, seen = phase2
    monkeypatch.setenv("BRAIN_ATTACHMENT_TOKEN_BUDGET", "70000")

    cli.cmd_process_attachments(args)

    assert seen["token_budget"].limit == 70_000


def test_a_zero_flag_lifts_the_environments_budget(phase2, monkeypatch):
    args, seen = phase2
    monkeypatch.setenv("BRAIN_ATTACHMENT_TOKEN_BUDGET", "70000")
    args.token_budget = 0

    cli.cmd_process_attachments(args)

    assert seen["token_budget"] is None


def test_neither_is_no_limit(phase2):
    args, seen = phase2

    cli.cmd_process_attachments(args)

    assert seen["token_budget"] is None


@pytest.mark.parametrize("bad", ["lots", "1.5", "-3"])
def test_a_bad_budget_in_the_environment_is_a_usage_error(phase2, monkeypatch, capsys, bad):
    args, seen = phase2
    monkeypatch.setenv("BRAIN_ATTACHMENT_TOKEN_BUDGET", bad)

    assert cli.cmd_process_attachments(args) == 2

    assert "BRAIN_ATTACHMENT_TOKEN_BUDGET" in capsys.readouterr().err
    assert "ran" not in seen


def test_a_phase_1_run_does_not_read_the_budget(phase2, monkeypatch):
    args, _seen = phase2
    monkeypatch.setenv("BRAIN_ATTACHMENT_TOKEN_BUDGET", "lots")
    monkeypatch.setattr(
        "src.extract.attachment_pipeline.run_phase1",
        lambda *a, **k: {"processed": 0, "extracted": 0, "failed": 0, "skipped": 0},
    )
    args.phase = 1

    assert cli.cmd_process_attachments(args) == 0


def test_a_run_the_budget_stopped_exits_0_and_counts_what_it_left(phase2, capsys):
    args, _seen = phase2
    args.token_budget = 50_000

    assert cli.cmd_process_attachments(args) == 0

    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if "token budget" in line.lower())
    assert "5" in line


def _pending_row(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject) VALUES (7, '2026-09-01', 'mail')"
    )
    conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size,"
        " file_path, exported_at) VALUES (1, 7, 'a.txt', 'text/plain', 1, '/x/a.txt',"
        " '2026-09-01')"
    )
    conn.execute(
        "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method,"
        " extraction_status, extracted_at, llm_status)"
        " VALUES (1, ?, 'direct_read', 'extracted', '2026-09-01', 'pending')",
        ("A note about the plan for the regional network. " * 30,),
    )
    conn.commit()
    conn.close()


def _no_model(monkeypatch):
    def refuse():
        raise AssertionError("an estimate must not reach the model")

    monkeypatch.setattr(claude_extract, "_get_client_and_model", refuse)


def test_the_estimate_prints_calls_tokens_and_cost_and_runs_nothing(phase2, monkeypatch, capsys):
    args, seen = phase2
    _pending_row(args.db)
    _no_model(monkeypatch)
    args.phase = None
    args.estimate = True

    assert cli.cmd_process_attachments(args) == 0

    out = capsys.readouterr().out
    assert "ran" not in seen
    assert re.search(r"attachments\s*: 1$", out, re.M) and re.search(r"calls\s*: 1$", out, re.M)
    assert "input tokens" in out and "output tokens" in out
    assert "$" in out and "claude-sonnet-5-5" in out


def test_the_flags_reach_the_command(monkeypatch):
    got = {}
    monkeypatch.setattr(cli, "cmd_process_attachments", lambda args: got.update(vars(args)))
    monkeypatch.setattr(
        sys,
        "argv",
        ["brain", "process-attachments", "--phase", "2", "--token-budget", "1000", "--estimate"],
    )

    cli.main()

    assert (got["token_budget"], got["estimate"]) == (1000, True)


# --- reextract -----------------------------------------------------------------


@pytest.fixture
def rx_args(tmp_path, monkeypatch):
    monkeypatch.delenv("BRAIN_ATTACHMENT_TOKEN_BUDGET", raising=False)
    return argparse.Namespace(
        db=str(tmp_path / "brain.db"),
        capped=False,
        long=False,
        zip=False,
        unread=False,
        partial=False,
        stale=False,
        formats=False,
        ocr=False,
        limit=0,
        after_id=0,
        dry_run=False,
        workers=1,
        root=str(tmp_path),
        full_parts=None,
        token_budget=None,
        estimate=False,
    )


def _fake_reextract(monkeypatch, calls):
    def fake(db, which, **kw):
        calls.append((which, kw))
        return dict.fromkeys(
            (
                "selected",
                "flagged",
                "reread",
                "resummarise",
                "missing",
                "kept",
                "unchanged",
                "relabelled",
                "ocr_close",
                "summarised",
                "failed",
                "over_budget",
                "highest_id",
            ),
            1,
        )

    monkeypatch.setattr("src.extract.reextract.reextract", fake)


def test_full_parts_needs_no_other_selector(rx_args, monkeypatch, capsys):
    calls: list = []
    _fake_reextract(monkeypatch, calls)
    rx_args.full_parts = [3, 4]

    assert cli.cmd_reextract(rx_args) == 0

    [(which, kw)] = calls
    assert which == set() and kw["full_parts"] == [3, 4]
    out = capsys.readouterr().out
    assert "flagged" in out and "over_budget" in out


def test_reextract_takes_the_budget(rx_args, monkeypatch):
    calls: list = []
    _fake_reextract(monkeypatch, calls)
    rx_args.long = True
    rx_args.token_budget = 9_000

    cli.cmd_reextract(rx_args)

    assert calls[0][1]["token_budget"].limit == 9_000


def test_the_reextract_estimate_prints_and_does_not_run(rx_args, monkeypatch, capsys):
    calls: list = []
    _fake_reextract(monkeypatch, calls)
    seen = {}

    def fake_estimate(db, which, **kw):
        seen.update(kw, which=which)
        return {
            "selected": 4,
            "unknown": 1,
            "rows": 3,
            "calls": 5,
            "input_tokens": 12_000,
            "output_tokens": 7_500,
            "chars_per_token": 1.6,
            "model": "claude-sonnet-5-5",
            "cost_usd": 0.11,
            "batch_cost_usd": 0.06,
        }

    monkeypatch.setattr("src.extract.reextract.estimate_reextract", fake_estimate)
    rx_args.long = True
    rx_args.estimate = True

    assert cli.cmd_reextract(rx_args) == 0

    out = capsys.readouterr().out
    assert calls == [] and seen["which"] == {"long"}
    assert re.search(r"calls\s*: 5$", out, re.M) and "12,000" in out and "$0.11" in out
    assert re.search(r"no text yet\s*: 1\b", out)  # read first, so not estimated


def test_the_reextract_flags_reach_the_command(monkeypatch):
    got = {}
    monkeypatch.setattr(cli, "cmd_reextract", lambda args: got.update(vars(args)))
    monkeypatch.setattr(
        sys,
        "argv",
        ["brain", "reextract", "--full-parts", "3", "4", "--token-budget", "100", "--estimate"],
    )

    cli.main()

    assert (got["full_parts"], got["token_budget"], got["estimate"]) == ([3, 4], 100, True)
