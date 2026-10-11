"""process-attachments exits 1 when a phase failed everything it tried (tests-ci-4).

The command printed 'Failed: N' and returned None whatever the counts, so a
phase-2 night where every attachment failed was logged by run_stage in
sb-attachment-pass.sh as a passing stage.
"""

import argparse
import sys

import pytest

from src import cli
from src.store.schema import create_database


def _stats(processed=0, extracted=0, failed=0, skipped=0):
    return {
        "processed": processed,
        "extracted": extracted,
        "failed": failed,
        "skipped": skipped,
        "deferred": 0,
    }


@pytest.fixture
def phases(tmp_path, monkeypatch):
    """Both phases stubbed; returns (args, dict of the stats each phase returns, calls)."""
    db_path = tmp_path / "brain.db"
    create_database(str(db_path)).close()
    result = {1: _stats(), 2: _stats()}
    calls: list[int] = []

    def _phase1(db_path, limit, file_type, deadline_s):
        calls.append(1)
        return result[1]

    def _phase2(db_path, limit, file_type, workers, deadline_s, token_budget=None):
        calls.append(2)
        return result[2]

    monkeypatch.setattr("src.extract.attachment_pipeline.run_phase1", _phase1)
    monkeypatch.setattr("src.extract.attachment_pipeline.run_phase2", _phase2)
    args = argparse.Namespace(
        db=db_path, phase=None, type=None, limit=0, workers=1, deadline_s=None
    )
    return args, result, calls


def test_phase2_failing_everything_returns_1(phases):
    args, result, _ = phases
    args.phase = 2
    result[2] = _stats(processed=3, failed=3)

    assert cli.cmd_process_attachments(args) == 1


def test_phase1_failing_everything_still_runs_phase2_then_returns_1(phases):
    args, result, calls = phases
    result[1] = _stats(processed=2, failed=2)
    result[2] = _stats(processed=2, extracted=2)

    assert cli.cmd_process_attachments(args) == 1
    assert calls == [1, 2]


def test_some_failures_beside_successes_return_0(phases):
    args, result, _ = phases
    result[1] = _stats(processed=3, extracted=2, failed=1)
    result[2] = _stats(processed=3, extracted=1, failed=2)

    assert cli.cmd_process_attachments(args) == 0


def test_nothing_to_do_returns_0(phases):
    args, _, _ = phases

    assert cli.cmd_process_attachments(args) == 0


def test_main_exits_1(phases, monkeypatch):
    args, result, _ = phases
    result[2] = _stats(processed=3, failed=3)
    monkeypatch.setattr(
        sys, "argv", ["brain", "--db", str(args.db), "process-attachments", "--phase", "2"]
    )
    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    monkeypatch.setenv("BRAIN_ROLE", "producer")

    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
