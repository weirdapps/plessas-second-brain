"""A message whose get-mail fails is fetched again later, not skipped for good.

The export advances its cursor over every message it listed, so that one bad
message cannot wedge a folder. A message whose body could not be fetched was
dropped with a warning and never looked at again: on 2026-06-12 seven Archive
messages came back as an outlook-cli status object instead of a message, and six
of them never reached the store. Now each failure is recorded in the folder's
cursor file and fetched by its Graph id on later runs, counted only in runs where
some get-mail succeeded (an outage uses up no attempts), until it is fetched or
given up; mail-reconcile lists what is still missing after that.
"""

import subprocess
from unittest.mock import patch

import pytest

from src.export import outlook_export
from src.export.outlook_cli import OutlookCliAuthRequired, OutlookCliError
from src.export.state import OutlookSyncState, load_outlook_sync_state, save_outlook_sync_state

CURSOR = "2026-10-01T00:00:00Z"


def _summary(message_id, received="2026-10-01T01:00:00Z"):
    return {"Id": message_id, "ReceivedDateTime": received}


def _message(message_id, received="2026-10-01T01:00:00Z"):
    return {"Id": message_id, "ReceivedDateTime": received, "Body": {"Content": "x"}}


@pytest.fixture
def export(tmp_path, monkeypatch):
    """export(listed, outcomes, retries=None): one hourly run of the Archive folder.

    `outcomes` maps a message id to what its get-mail does: "ok", "incomplete",
    "upstream", "timeout" or "auth"; staged messages are collected in .staged.
    """
    path = tmp_path / "outlook_sync_archive.json"
    staged: list[str] = []
    calls: list[str] = []
    monkeypatch.setattr(
        outlook_export,
        "commit_messages_to_db",
        lambda messages, folder: staged.extend(m["Id"] for m in messages),
    )
    monkeypatch.setattr(
        outlook_export, "download_attachments_for_messages", lambda messages: {"scanned": 0}
    )

    def go(listed, outcomes, retries=None):
        state = load_outlook_sync_state(path)
        if not path.exists():
            state = OutlookSyncState(last_seen_received_at=CURSOR, folder="Archive")
        if retries is not None:
            state.fetch_retries = retries
        save_outlook_sync_state(path, state)

        def cli(args, timeout_sec=60):
            if args[0] == "auth-check":
                return {"ok": True}
            if args[0] == "list-mail":
                return [_summary(i) for i in listed]
            message_id = args[1]
            calls.append(message_id)
            outcome = outcomes.get(message_id, "ok")
            if outcome == "incomplete":
                return {"status": "ok", "sessionFile": "x", "account": "y"}
            if outcome == "upstream":
                raise OutlookCliError(exit_code=5, stderr="404", retryable=True)
            if outcome == "timeout":
                raise subprocess.TimeoutExpired(cmd="outlook-cli", timeout=60)
            if outcome == "auth":
                raise OutlookCliAuthRequired("expired")
            return _message(message_id)

        with patch.object(outlook_export, "run_outlook_cli", cli):
            return outlook_export.run_hourly_sync(state_path=path, folder="Archive", concurrency=1)

    go.staged = staged
    go.calls = calls
    go.state = lambda: load_outlook_sync_state(path)
    return go


@pytest.mark.parametrize("failure", ["incomplete", "upstream", "timeout"])
def test_a_failed_get_mail_is_recorded_and_the_run_goes_on(export, failure):
    result = export(["a", "b"], {"b": failure})

    assert result["status"] == "ok"
    assert export.staged == ["a"]
    state = export.state()
    assert state.fetch_retries == {"b": {"received": "2026-10-01T01:00:00Z", "attempts": 1}}
    assert state.last_seen_received_at == "2026-10-01T01:00:00Z", "the cursor still advances"


def test_a_recorded_failure_is_fetched_on_the_next_run(export):
    export(["a", "b"], {"b": "incomplete"})
    export.staged.clear()

    export([], {})

    assert export.staged == ["b"]
    assert export.state().fetch_retries == {}


def test_a_failure_re_listed_by_the_inclusive_cursor_is_fetched_once_per_run(export):
    export(["a", "b"], {"b": "upstream"})
    export.calls.clear()

    export(["b"], {})

    assert export.calls == ["b"]
    assert export.state().fetch_retries == {}


def test_a_message_that_keeps_failing_is_given_up_after_the_cap(export):
    seed = {"b": {"received": "2026-10-01T01:00:00Z", "attempts": 0}}
    # Every run fetches something else, so each failure of b counts.
    for n in range(outlook_export.FETCH_MAX_ATTEMPTS):
        export([f"fresh{n}"], {"b": "upstream"}, retries=seed if n == 0 else None)

    state = export.state()
    assert "b" not in state.fetch_retries
    assert [g["id"] for g in state.fetch_gave_up] == ["b"]
    export.calls.clear()
    export(["later"], {})
    assert export.calls == ["later"], "a message given up is not fetched again"


def test_a_run_where_no_get_mail_succeeds_uses_up_no_attempts(export):
    export(
        [], {"b": "upstream"}, retries={"b": {"received": "2026-10-01T01:00:00Z", "attempts": 2}}
    )
    for _ in range(outlook_export.FETCH_MAX_ATTEMPTS + 2):
        export(["c"], {"b": "upstream", "c": "upstream"})

    state = export.state()
    assert state.fetch_retries["b"]["attempts"] == 2
    assert state.fetch_retries["c"]["attempts"] == 0
    assert state.fetch_gave_up == []


def test_a_staging_failure_keeps_the_retry_record(export, monkeypatch):
    """Fetched but not staged, a retried message must stay on the list."""

    def fail(messages, folder):
        raise OSError("disk full")

    monkeypatch.setattr(outlook_export, "commit_messages_to_db", fail)
    seed = {"b": {"received": "2026-10-01T01:00:00Z", "attempts": 1}}

    with pytest.raises(OSError):
        export(["a"], {"a": "upstream"}, retries=seed)

    assert export.state().fetch_retries == seed


def test_an_auth_loss_records_nothing_and_keeps_the_cursor(export):
    with pytest.raises(OutlookCliAuthRequired):
        export(["a", "b"], {"b": "auth"})

    state = export.state()
    assert state.fetch_retries == {}
    assert state.last_seen_received_at == CURSOR


def test_retries_one_run_takes_on_are_bounded(export, monkeypatch):
    monkeypatch.setattr(outlook_export, "FETCH_RETRIES_PER_RUN", 3)
    retries = {f"r{n}": {"received": "2026-10-01T01:00:00Z", "attempts": 0} for n in range(5)}

    export([], {}, retries=retries)

    assert sorted(export.calls) == ["r0", "r1", "r2"]
    assert sorted(export.state().fetch_retries) == ["r3", "r4"]
