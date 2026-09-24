"""The scheduled syncs' email extraction must stop at its deadline.

cmd_sync Step 2 called run_extraction with no bound at all. Five consecutive
quota errors made it sleep QUOTA_PAUSE_SECONDS (an hour) in-process, inside a
unit budgeted in minutes, and a backlog after an outage could use up the whole
unit on its own. tests/test_sync_budget.py assumed a figure for the stage that
nothing enforced. With a deadline the step returns, the rest stays pending, and
the next scheduled run is the retry.

The clock in these tests moves with the work done, not with the number of times
it is read, so each deadline check has a test that fails when it is removed.
"""

import threading
from unittest.mock import patch

import pytest

from src.extract import local


@pytest.fixture
def staged(monkeypatch, tmp_path):
    """Returns stage(n, behaviour): n staged emails and the model's behaviour."""
    monkeypatch.setattr(local, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(local, "EXTRACTED_DIR", tmp_path / "extracted")
    monkeypatch.setattr(local, "LOG_FILE", tmp_path / "extract.log")
    monkeypatch.setattr(
        "src.extract.claude_extract._get_client_and_model", lambda: (object(), "model")
    )

    def no_sleeping(_seconds):
        raise AssertionError("slept inside a run that has a deadline")

    monkeypatch.setattr(local.time, "sleep", no_sleeping)
    calls: list[str] = []

    def stage(n, behaviour="ok", call_seconds=0.0):
        monkeypatch.setattr(
            local, "collect_emails", lambda: [{"message_id": f"m{i}"} for i in range(n)]
        )

        def extract(email, api_key, engine="claude"):
            calls.append(email["message_id"])
            if call_seconds:
                threading.Event().wait(call_seconds)  # not time.sleep, which is patched
            if behaviour == "quota":
                return email["message_id"], None, True, None
            return email["message_id"], {"summary": "s"}, False, None

        monkeypatch.setattr(local, "extract_inline", extract)
        return calls

    return stage


def _deadline_after(monkeypatch, calls, n):
    """The deadline (50 s after the start) passes once n calls have begun."""
    monkeypatch.setattr(local.time, "monotonic", lambda: 100.0 if len(calls) >= n else 0.0)


@pytest.mark.parametrize("workers", [1, 3])
def test_a_quota_pause_ends_the_run_instead_of_sleeping(staged, workers):
    calls = staged(10, "quota")

    result = local.run_extraction(workers=workers, deadline_s=60.0)

    assert calls, "the run should have attempted something before pausing"
    assert result["quota_paused"] is True


def test_the_deadline_stops_the_sequential_loop_between_items(staged, monkeypatch):
    """No quota errors here, so nothing but the deadline check can end the run."""
    calls = staged(10)
    _deadline_after(monkeypatch, calls, 2)

    result = local.run_extraction(workers=1, deadline_s=50.0)

    assert len(calls) == 2
    assert result == {"extracted": 2, "failed": 0, "quota_paused": False}


def test_the_deadline_stops_the_concurrent_path_at_a_chunk_boundary(staged, monkeypatch):
    """Production runs --workers 8. Chunks are workers*10, so 40 emails at three
    workers are a chunk of 30 and one of 10, and the second is never submitted."""
    calls = staged(40)
    _deadline_after(monkeypatch, calls, 30)

    result = local.run_extraction(workers=3, deadline_s=50.0)

    assert len(calls) == 30
    assert result["extracted"] == 30


def test_the_deadline_cancels_the_rest_of_a_chunk(staged, monkeypatch):
    """Calls already running finish and are kept; the queued ones never start,
    and a cancelled future is skipped rather than asked for its result."""
    calls = staged(10, call_seconds=0.05)
    _deadline_after(monkeypatch, calls, 3)

    result = local.run_extraction(workers=3, deadline_s=50.0)

    assert len(calls) < 10
    assert result["extracted"] == len(calls)


def test_with_a_deadline_the_newest_mail_goes_first(staged, monkeypatch):
    """A tail of old emails that fail on every run would otherwise spend each
    run's budget before fresh mail was reached."""
    calls = staged(0)
    monkeypatch.setattr(
        local,
        "collect_emails",
        lambda: [{"message_id": f"m{i}", "date_received": f"2026-09-2{i}"} for i in range(5)],
    )

    local.run_extraction(workers=1, deadline_s=600.0)

    assert calls == ["m4", "m3", "m2", "m1", "m0"]


def test_without_a_deadline_the_order_and_the_quota_sleep_are_unchanged(staged):
    """Manual runs (python -m src.extract.local) keep the original behaviour."""
    calls = staged(5, "quota")

    with pytest.raises(AssertionError, match="slept"):
        local.run_extraction(workers=1)
    assert calls[0] == "m0"


# ------------------------------------------------------------------ cmd_sync


def _sync(tmp_path, monkeypatch, unit, extraction_result):
    from src import cli, llm_deadline
    from src.store.schema import create_database

    db_path = tmp_path / "brain.db"
    conn = create_database(str(db_path))
    conn.execute(
        "INSERT INTO sync_metadata (key, value) VALUES ('last_sync_date', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("src.cli.DATA_ROOT", tmp_path)
    monkeypatch.setattr(llm_deadline, "_detect_systemd_unit", lambda: unit)
    args = type(
        "Args",
        (),
        {"db": db_path, "limit": None, "engine": "claude", "workers": 1, "skip_export": True},
    )()
    with (
        patch("src.extract.local.run_extraction", return_value=extraction_result) as run,
        patch("src.store.loader.load_extractions", return_value=0),
        patch("src.extract.attachment_pipeline.run_phase1", return_value={"processed": 0}),
        patch("src.export.conversation_export.export_conversations", return_value={"exported": 0}),
        patch("src.extract.image_pipeline.run_backfill", return_value={}),
    ):
        rc = cli.cmd_sync(args)
    return rc, run.call_args.kwargs["deadline_s"]


@pytest.mark.parametrize(
    ("unit", "deadline"),
    [
        ("sb-outlook-sync", 300.0),
        ("sb-daily-sync", 900.0),
        ("sb-noon-catchup", 900.0),
        (None, 300.0),
    ],
)
def test_cmd_sync_gives_extraction_the_deadline_of_its_unit(tmp_path, monkeypatch, unit, deadline):
    """The 30-minute units drain backlogs, so they must not inherit the hourly
    slice. No unit is cron, launchd or a unit off the table (the docs describe
    all three), so it gets the hourly slice, not an unbounded run."""
    monkeypatch.delenv("PTS_LLM_DEADLINE", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    ok = {"extracted": 0, "failed": 0, "quota_paused": False}

    assert _sync(tmp_path, monkeypatch, unit, ok) == (0, deadline)


def test_cmd_sync_fails_the_run_when_extraction_ended_on_a_quota_pause(tmp_path, monkeypatch):
    """Before the deadline, the same pause slept past the unit timeout and read
    red. It must not turn green just because it now returns in time."""
    paused = {"extracted": 3, "failed": 5, "quota_paused": True}

    rc, _ = _sync(tmp_path, monkeypatch, "sb-daily-sync", paused)

    assert rc == 75


def test_a_person_at_a_terminal_keeps_the_unbounded_run(monkeypatch):
    from src import cli, llm_deadline

    monkeypatch.setattr(llm_deadline, "_detect_systemd_unit", lambda: None)
    monkeypatch.delenv("PTS_LLM_DEADLINE", raising=False)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)

    assert cli._extract_deadline_s() is None


@pytest.mark.parametrize(("left_s", "expected"), [(10_000.0, 300.0), (400.0, 20.0), (100.0, 0.0)])
def test_the_slice_is_capped_by_what_is_left_of_the_unit(monkeypatch, left_s, expected):
    """A second sync in the same unit (the wrappers retry once on a database
    lock) got a fresh full slice and ran past the unit timeout. The unit-anchored
    LLM deadline says how much is left; the stages after extraction need theirs."""
    from src import cli, llm_deadline

    now = 1_000_000.0
    monkeypatch.setattr(llm_deadline, "_detect_systemd_unit", lambda: "sb-outlook-sync")
    monkeypatch.setattr("time.time", lambda: now)
    monkeypatch.setenv("PTS_LLM_DEADLINE", repr(now + left_s))

    assert cli._extract_deadline_s() == pytest.approx(expected)


def test_a_quota_pause_is_logged_as_a_quota_pause(staged, tmp_path):
    """The concurrent path logged every early end as the deadline, so a red 75
    sent whoever chased it to the wrong cause."""
    staged(40, "quota")

    local.run_extraction(workers=3, deadline_s=3600.0)

    log = (tmp_path / "extract.log").read_text()
    assert "Quota pause; " in log
    assert "Deadline reached" not in log


def test_newest_first_follows_the_mail_date_not_the_batch_order(staged, monkeypatch):
    """Archive and Sent bootstraps stage old mail into new batches, and batch
    names do not sort past batch-99999, so staging order is not age."""
    calls = staged(0)
    emails = [
        {"message_id": "old-in-new-batch", "date_received": "2025-03-01T09:00:00Z"},
        {"message_id": "fresh", "date_received": "2026-09-23T09:00:00Z"},
        {"message_id": "undated"},
        {"message_id": "middle", "date_received": "2026-01-01T09:00:00Z"},
    ]
    monkeypatch.setattr(local, "collect_emails", lambda: emails)

    local.run_extraction(workers=1, deadline_s=600.0)

    assert calls == ["fresh", "middle", "old-in-new-batch", "undated"]
