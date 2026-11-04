"""The per-route effort sweep (issue 211): `--sweep low,medium,high` runs the same cases once
per effort, each a run of its own in results.jsonl, and prints one line per effort to
compare: pass rate, rows cut short, thinking and output tokens, cost. The estimate counts
every effort before anything is called, and nothing is called without --yes.

The model is the fake of test_extraction_eval_call_shape, installed as complete().
"""

import pytest

from src.evals import extraction as ev
from src.evals import read_jsonl
from src.extract import call_shape
from tests.evals.test_extraction_eval_call_shape import MODEL, SCRIPT, FakeModel
from tests.patching import unset_env

OWNER = ("Owner Person", "owner@example.com")


@pytest.fixture
def files(tmp_path):
    return ev.Files(tmp_path / "evals" / "extraction")


@pytest.fixture
def model(monkeypatch):
    from src.extract import claude_extract

    monkeypatch.setenv("CLAUDE_EXTRACT_MODEL", MODEL)
    unset_env(monkeypatch, "BRAIN_EXTRACT_CALL_SHAPE")

    def install(cases):
        fake = FakeModel(cases)
        monkeypatch.setattr(claude_extract, "complete", fake)
        return fake

    return install


@pytest.fixture
def cli():
    import importlib.util

    spec = importlib.util.spec_from_file_location("extraction_eval_cli_sweep", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


@pytest.fixture
def cases(monkeypatch):
    monkeypatch.setenv("BRAIN_USER_NAME", OWNER[0])
    monkeypatch.setenv("BRAIN_USER_EMAIL_PATTERN", OWNER[1])
    return ev.synthetic_cases(owner_name=OWNER[0], owner_pattern=OWNER[1])


def _argv(files, *extra):
    return ["--run", "--synthetic", "--reps", "1", "--dir", str(files.root), *extra]


def test_the_eval_offers_the_efforts_production_takes(cli):
    assert cli.__globals__["EFFORTS"] is call_shape.EFFORTS


def test_a_sweep_runs_once_per_effort_and_sends_each(cli, files, model, cases, capsys):
    fake = model(cases)

    assert cli(_argv(files, "--yes", "--call-shape", "v2-text", "--sweep", "low,medium")) == 0

    records = read_jsonl(files.results)
    assert [r["effort"] for r in records] == ["low", "medium"]
    sent = [c["output_config"]["effort"] for c in fake.calls]
    assert sent == ["low"] * len(cases) + ["medium"] * len(cases)
    out = capsys.readouterr().out
    assert "effort sweep" in out
    assert out.count("\n  low ") == 1 and out.count("\n  medium ") == 1


def test_a_sweep_without_yes_calls_nothing_and_counts_every_effort(
    cli, files, model, cases, capsys
):
    fake = model(cases)

    assert cli(_argv(files, "--sweep", "low,medium,high")) == 2

    assert fake.calls == []
    assert "x 3 efforts" in capsys.readouterr().out


def test_a_sweep_rejects_a_value_that_is_not_an_effort(cli, files, model, cases):
    model(cases)

    with pytest.raises(SystemExit):
        cli(_argv(files, "--yes", "--sweep", "low,hihg"))


def test_sweep_and_effort_are_one_or_the_other(cli, files, model, cases):
    model(cases)

    with pytest.raises(SystemExit):
        cli(_argv(files, "--yes", "--sweep", "low", "--effort", "high"))


def test_the_sweep_table_reads_the_records():
    record = {
        "effort": "low",
        "pass_rate": 0.9,
        "case_pass_rate": 0.5,
        "rows": {"scored": 6, "truncated": 1, "refused": 0, "errors": 0},
        "usage": {"output_tokens": 1200, "thinking_tokens": 300},
        "cost_usd": 0.12,
    }

    [header, line] = ev.sweep_table([record])

    assert header.split() == [
        "effort",
        "pass",
        "rows",
        "cut",
        "refused",
        "errors",
        "output",
        "thinking",
        "cost",
    ]
    assert line.split() == ["low", "90.0%", "6", "1", "0", "0", "1,200", "300", "$0.12"]
