"""The hourly sync's email extraction must stop at its deadline.

cmd_sync Step 2 called run_extraction with no bound at all. Five consecutive
quota errors made it sleep QUOTA_PAUSE_SECONDS (an hour) in-process, inside a
unit budgeted at 600 s, and a backlog after an outage could use up the whole
unit on its own. tests/test_sync_budget.py assumed a figure for the stage that
nothing enforced. With a deadline the step returns, the rest stays pending, and
the next hourly run is the retry.
"""

import pytest

from src.extract import local


@pytest.fixture
def pending(monkeypatch, tmp_path):
    """Ten staged emails, a fresh state file, and a model that always hits quota."""
    monkeypatch.setattr(local, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(local, "EXTRACTED_DIR", tmp_path / "extracted")
    monkeypatch.setattr(local, "LOG_FILE", tmp_path / "extract.log")
    monkeypatch.setattr(
        local, "collect_emails", lambda: [{"message_id": f"m{i}"} for i in range(10)]
    )
    monkeypatch.setattr(
        "src.extract.claude_extract._get_client_and_model", lambda: (object(), "model")
    )
    calls = []

    def quota_failure(email, api_key, engine="claude"):
        calls.append(email["message_id"])
        return email["message_id"], None, True

    monkeypatch.setattr(local, "extract_inline", quota_failure)

    def no_sleeping(_seconds):
        raise AssertionError("slept inside a run that has a deadline")

    monkeypatch.setattr(local.time, "sleep", no_sleeping)
    return calls


@pytest.mark.parametrize("workers", [1, 3])
def test_a_quota_pause_ends_the_run_instead_of_sleeping(pending, workers):
    local.run_extraction(workers=workers, deadline_s=60.0)

    assert pending, "the run should have attempted something before pausing"


def test_the_deadline_stops_the_loop_between_items(pending, monkeypatch):
    clock = iter(range(0, 1000, 10))  # ten seconds per tick
    monkeypatch.setattr(local.time, "monotonic", lambda: float(next(clock)))

    local.run_extraction(workers=1, deadline_s=25.0)

    assert len(pending) < 10


def test_without_a_deadline_the_quota_pause_still_sleeps(pending):
    """Manual runs (python -m src.extract.local) keep the original behaviour."""
    with pytest.raises(AssertionError, match="slept"):
        local.run_extraction(workers=1)
