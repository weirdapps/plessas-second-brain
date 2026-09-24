"""Step 7 of `cmd_sync` has to stop on a clock, not on running out of work.

`run_conversation_extraction` took `limit` and nothing else, and `cmd_sync`
called it with no arguments at all, so `limit=0` meant "every pending
conversation, one at a time". That is unbounded by construction: the pending
list is whatever `sync-tokens-to-vps.sh` has rsynced since the last run, which
on a busy Claude Code day is dozens of transcripts, each its own LLM call.

The behaviour asserted here is the same contract Steps 6 and 8 already keep:
spend up to `deadline_s`, then leave the rest queued for the next run and the
separate `sb-conversation-sync` unit, which has 900 s to itself.
"""

import itertools
import json
from unittest.mock import patch

import pytest

from src.extract import local


@pytest.fixture
def staged(tmp_path, monkeypatch):
    """Ten pending conversations and writable state/output/log paths under tmp_path.

    LOG_FILE has to be redirected too. It defaults to `data/extract.log`, and
    `log()` opens it without creating the parent, so these tests pass on a
    checkout that happens to have a `data/` directory and fail on one that does
    not. That is exactly how they went green locally and red on CI.
    """
    monkeypatch.setattr(local, "CONV_EXTRACTED_DIR", tmp_path / "extracted")
    monkeypatch.setattr(local, "CONV_STATE_FILE", tmp_path / "state" / "conv.json")
    monkeypatch.setattr(local, "LOG_FILE", tmp_path / "extract.log")
    return [{"session_id": f"sess-{i:03d}"} for i in range(10)]


def _extraction_for(conv):
    return conv["session_id"], {"summary": "x"}, False, None


def _with_a_fresh_success(staged):
    """collect_conversations for successive runs: the staged ones, plus a new
    conversation each run that extracts, so every run shows the model working."""
    runs = iter(range(1000))
    return lambda: [*staged, {"session_id": f"ok-{next(runs)}"}]


def _refuses_but_the_fresh_ones(conv):
    if conv["session_id"].startswith("ok-"):
        return _extraction_for(conv)
    return conv["session_id"], None, False, "fault"


def test_stops_once_the_deadline_is_spent(staged, monkeypatch):
    """Each item costs 10 s of a 25 s budget, so the third check is the one that
    trips. Asserting "fewer than all ten" rather than an exact count keeps this
    from breaking on whether the check runs before or after the call."""
    # Infinite, because patching time.monotonic patches it for everything that
    # runs during the test, not just the loop under test. A fixed-length list
    # ends as a StopIteration from somewhere unrelated.
    clock = itertools.count(0.0, 10.0)
    monkeypatch.setattr(local.time, "monotonic", lambda: next(clock))

    with (
        patch.object(local, "collect_conversations", return_value=staged),
        patch("src.extract.claude_extract._get_client_and_model", return_value=(object(), "m")),
        patch.object(local, "extract_conversation_inline", side_effect=_extraction_for) as ex,
    ):
        local.run_conversation_extraction(deadline_s=25.0)

    assert 0 < ex.call_count < len(staged)


def test_no_deadline_still_processes_everything(staged):
    """`sb-conversation-sync` and any hand-run must keep the old behaviour, so
    the bound has to be opt-in rather than a new default ceiling."""
    with (
        patch.object(local, "collect_conversations", return_value=staged),
        patch("src.extract.claude_extract._get_client_and_model", return_value=(object(), "m")),
        patch.object(local, "extract_conversation_inline", side_effect=_extraction_for) as ex,
    ):
        local.run_conversation_extraction()

    assert ex.call_count == len(staged)


def test_a_session_staged_in_many_batches_is_offered_once(tmp_path, monkeypatch):
    """`collect_conversations` concatenates every batch file ever written, and a
    long-running session is re-exported into each batch it is still open for.
    On the VPS that turned 1,696 distinct sessions into 5,265 list entries, one
    of them 248 times. Deduplication is what makes the pending count mean
    "conversations left", and it has to keep the LAST copy: batches are sorted
    by timestamp, so the newest export is the most complete transcript."""
    staging = tmp_path / "staging"
    staging.mkdir()
    for i, turns in enumerate([1, 2, 7]):
        (staging / f"conversation-batch-2026041{i}_000000.json").write_text(
            json.dumps({"conversations": [{"session_id": "sess-a", "turns": turns}]})
        )
    monkeypatch.setattr(local, "CONV_STAGING_DIR", staging)

    convs = local.collect_conversations()

    assert [c["session_id"] for c in convs] == ["sess-a"]
    assert convs[0]["turns"] == 7


def test_a_conversation_that_keeps_being_refused_is_given_up_on(staged, monkeypatch):
    """One session, 995a679f, was refused 2,680 times on 2026-09-05 alone. It sat
    at the head of the pending list, and because a failure was never recorded it
    was re-offered on every run, so the loop never reached the other 3,263.
    Attachments and SharePoint links already have this ("abandoned, no longer
    retried"); conversations were the pipeline that did not."""
    monkeypatch.setattr(local, "CONVERSATION_MAX_ATTEMPTS", 2)

    with (
        patch.object(local, "collect_conversations", side_effect=_with_a_fresh_success(staged)),
        patch("src.extract.claude_extract._get_client_and_model", return_value=(object(), "m")),
        patch.object(local, "extract_conversation_inline", side_effect=_refuses_but_the_fresh_ones),
    ):
        for _ in range(local.CONVERSATION_MAX_ATTEMPTS):
            local.run_conversation_extraction()

        with patch.object(
            local, "extract_conversation_inline", side_effect=_refuses_but_the_fresh_ones
        ) as ex:
            local.run_conversation_extraction()

    assert [c.args[0]["session_id"] for c in ex.call_args_list] == ["ok-2"]


def test_giving_up_does_not_mark_a_conversation_extracted(staged, monkeypatch):
    """A given-up session must not land in processed_ids: that set is what the
    loader treats as done, so an abandoned transcript would read as ingested."""
    monkeypatch.setattr(local, "CONVERSATION_MAX_ATTEMPTS", 1)

    with (
        patch.object(local, "collect_conversations", side_effect=_with_a_fresh_success(staged)),
        patch("src.extract.claude_extract._get_client_and_model", return_value=(object(), "m")),
        patch.object(local, "extract_conversation_inline", side_effect=_refuses_but_the_fresh_ones),
    ):
        local.run_conversation_extraction()

    state = json.loads(local.CONV_STATE_FILE.read_text())

    assert state["processed_ids"] == ["ok-0"]
    assert set(state["given_up_ids"]) == {c["session_id"] for c in staged}


def test_a_failure_below_the_cap_is_offered_again(staged, monkeypatch):
    """Give-up must not fire on the first bad run: one flaky run must not
    permanently drop a conversation from the knowledge base."""
    monkeypatch.setattr(local, "CONVERSATION_MAX_ATTEMPTS", 3)

    with (
        patch.object(local, "collect_conversations", return_value=staged),
        patch("src.extract.claude_extract._get_client_and_model", return_value=(object(), "m")),
        patch.object(local, "extract_conversation_inline", side_effect=_refuses_but_the_fresh_ones),
    ):
        with patch.object(
            local, "collect_conversations", return_value=[*staged, {"session_id": "ok-0"}]
        ):
            local.run_conversation_extraction()

        with patch.object(local, "extract_conversation_inline", side_effect=_extraction_for) as ex:
            local.run_conversation_extraction()

    assert ex.call_count == len(staged)


def test_the_newest_conversations_go_first(staged, monkeypatch):
    """Step 7 has 30 s. An old conversation the model will not take held the head
    of the list and spent every hourly budget."""
    convs = [
        {"session_id": "old", "started_at": "2026-09-01T09:00:00Z"},
        {"session_id": "new", "started_at": "2026-09-24T09:00:00Z"},
        {"session_id": "mid", "started_at": "2026-09-10T09:00:00Z"},
    ]

    with (
        patch.object(local, "collect_conversations", return_value=convs),
        patch("src.extract.claude_extract._get_client_and_model", return_value=(object(), "m")),
        patch.object(local, "extract_conversation_inline", side_effect=_extraction_for) as ex,
    ):
        local.run_conversation_extraction()

    assert [c.args[0]["session_id"] for c in ex.call_args_list] == ["new", "mid", "old"]


def test_work_done_before_the_deadline_is_saved(staged, tmp_path, monkeypatch):
    """A deferred item is only free if the ones already paid for are durable.
    Without the final state write, a budget-limited run would re-extract the
    same conversations every hour and never advance."""
    # Infinite, because patching time.monotonic patches it for everything that
    # runs during the test, not just the loop under test. A fixed-length list
    # ends as a StopIteration from somewhere unrelated.
    clock = itertools.count(0.0, 10.0)
    monkeypatch.setattr(local.time, "monotonic", lambda: next(clock))

    with (
        patch.object(local, "collect_conversations", return_value=staged),
        patch("src.extract.claude_extract._get_client_and_model", return_value=(object(), "m")),
        patch.object(local, "extract_conversation_inline", side_effect=_extraction_for),
    ):
        local.run_conversation_extraction(deadline_s=25.0)

    state = local.CONV_STATE_FILE
    assert state.exists()

    processed = json.loads(state.read_text())["processed_ids"]
    assert len(processed) > 0


def test_a_quota_or_auth_failure_never_counts_toward_giving_up(staged, monkeypatch):
    """extract_conversation_inline says a quota or auth failure is not countable,
    and it is not counted even in runs where other conversations succeed."""
    monkeypatch.setattr(local, "CONVERSATION_MAX_ATTEMPTS", 1)

    def not_countable(conv):
        if conv["session_id"].startswith("ok-"):
            return _extraction_for(conv)
        return conv["session_id"], None, False, None

    with (
        patch.object(local, "collect_conversations", side_effect=_with_a_fresh_success(staged)),
        patch("src.extract.claude_extract._get_client_and_model", return_value=(object(), "m")),
        patch.object(local, "extract_conversation_inline", side_effect=not_countable),
    ):
        local.run_conversation_extraction()
        local.run_conversation_extraction()

        with patch.object(local, "extract_conversation_inline", side_effect=_extraction_for) as ex:
            local.run_conversation_extraction()

    assert ex.call_count == len(staged) + 1


def test_a_run_in_which_nothing_succeeded_counts_nothing(staged, monkeypatch):
    """An outage failed every conversation it touched, and three outage runs gave
    them all up for good. The run's own successes decide, not earlier runs'."""
    monkeypatch.setattr(local, "CONVERSATION_MAX_ATTEMPTS", 1)
    local.CONV_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    local.CONV_STATE_FILE.write_text(json.dumps({"processed_ids": ["earlier"]}))

    with (
        patch.object(local, "collect_conversations", return_value=staged),
        patch("src.extract.claude_extract._get_client_and_model", return_value=(object(), "m")),
        patch.object(local, "extract_conversation_inline", side_effect=_refuses_but_the_fresh_ones),
    ):
        local.run_conversation_extraction()
        local.run_conversation_extraction()

    state = json.loads(local.CONV_STATE_FILE.read_text())
    assert state["failed_attempts"] == {}
    assert state["given_up_ids"] == []


def _status(code):
    from src.extract import policy_bridge

    cls = policy_bridge.anthropic.APIStatusError
    exc = cls.__new__(cls)
    exc.status_code = code
    return exc


def _overloaded():
    """The SDK's 529, through the module that already imports the SDK."""
    from src.extract import policy_bridge

    cls = policy_bridge.anthropic.OverloadedError
    return cls.__new__(cls)


@pytest.mark.parametrize(
    ("error", "quota", "failure"),
    [
        pytest.param(
            ValueError("no text block (stop_reason='refusal')"), False, "fault", id="refusal"
        ),
        pytest.param(RuntimeError("400 prompt is too long"), False, "fault", id="rejected"),
        pytest.param(ConnectionError("connection reset"), False, None, id="service"),
        pytest.param(RuntimeError("429 RESOURCE_EXHAUSTED"), True, None, id="quota"),
        pytest.param(_overloaded(), True, None, id="overloaded-529"),
        pytest.param(_status(408), False, "timeout", id="request-timeout-408"),
        pytest.param(
            ValueError("Failed to parse JSON: Expecting ',' delimiter: line 1 column 4291"),
            False,
            "fault",
            id="429-in-a-reply-error",
        ),
    ],
)
def test_one_attempt_and_only_the_conversations_own_failure_counts(
    staged, monkeypatch, error, quota, failure
):
    """The policy inside complete() is the only retry; a 529 overload is quota, as
    it is for email."""
    seen = []

    def fail(conversation):
        seen.append(conversation["session_id"])
        raise error

    monkeypatch.setattr("src.extract.claude_extract.extract_conversation", fail)
    monkeypatch.setattr(local.time, "sleep", lambda s: None)

    result = local.extract_conversation_inline({"session_id": "s"})

    assert result == ("s", None, quota, failure)
    assert len(seen) == 1


def test_an_expired_credential_is_not_the_conversations_fault(staged, monkeypatch):
    import google.auth.exceptions as gauth

    def expired(conversation):
        raise gauth.RefreshError("invalid_grant")

    monkeypatch.setattr("src.extract.claude_extract.extract_conversation", expired)
    monkeypatch.setattr(local.time, "sleep", lambda s: None)

    assert local.extract_conversation_inline({"session_id": "s"}) == ("s", None, False, None)


def test_a_conversation_still_going_on_waits(staged, monkeypatch):
    """Staged part-way through, it would load its first turns for good: a loaded
    session is never staged again."""
    from datetime import UTC, datetime, timedelta

    def ended(minutes_ago):
        return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )

    convs = [
        {"session_id": "live", "started_at": "2026-09-24T09:00:00Z", "ended_at": ended(5)},
        {"session_id": "done", "started_at": "2026-09-24T06:00:00Z", "ended_at": ended(180)},
    ]

    with (
        patch.object(local, "collect_conversations", return_value=convs),
        patch("src.extract.claude_extract._get_client_and_model", return_value=(object(), "m")),
        patch.object(local, "extract_conversation_inline", side_effect=_extraction_for) as ex,
    ):
        local.run_conversation_extraction()

    assert [c.args[0]["session_id"] for c in ex.call_args_list] == ["done"]


def test_a_conversation_that_keeps_timing_out_is_given_up_on_the_longer_cap(staged, monkeypatch):
    def times_out(conv):
        if conv["session_id"].startswith("ok-"):
            return _extraction_for(conv)
        return conv["session_id"], None, False, "timeout"

    logged: list[str] = []
    monkeypatch.setattr(local, "log", logged.append)

    def gave_up():
        return [m for m in logged if m.startswith("GAVE UP")]

    with (
        patch.object(local, "collect_conversations", side_effect=_with_a_fresh_success(staged)),
        patch("src.extract.claude_extract._get_client_and_model", return_value=(object(), "m")),
        patch.object(local, "extract_conversation_inline", side_effect=times_out),
    ):
        for _ in range(local.CONVERSATION_MAX_TIMEOUTS - 1):
            local.run_conversation_extraction()
        state = json.loads(local.CONV_STATE_FILE.read_text())
        assert state["given_up_ids"] == []
        assert gave_up() == []

        local.run_conversation_extraction()

    state = json.loads(local.CONV_STATE_FILE.read_text())
    assert set(state["given_up_ids"]) == {c["session_id"] for c in staged}
    assert len(gave_up()) == len(staged)


def test_a_run_of_quota_errors_ends_the_run(staged, monkeypatch):
    """The email loop stops after CONSECUTIVE_FAIL_THRESHOLD quota errors in a
    row; the conversation loop read the flag and went on, spending each
    conversation's policy retries on an overloaded service until the unit's end.
    The run then says it was cut short, not complete."""
    logged: list[str] = []
    monkeypatch.setattr(local, "log", logged.append)

    def quota(conv):
        return conv["session_id"], None, True, None

    with (
        patch.object(local, "collect_conversations", return_value=staged),
        patch("src.extract.claude_extract._get_client_and_model", return_value=(object(), "m")),
        patch.object(local, "extract_conversation_inline", side_effect=quota) as ex,
    ):
        local.run_conversation_extraction()

    assert ex.call_count == local.CONSECUTIVE_FAIL_THRESHOLD
    state = json.loads(local.CONV_STATE_FILE.read_text())
    assert state["failed_attempts"] == {} and state["timeout_attempts"] == {}
    assert "=== CONVERSATION EXTRACTION CUT SHORT ===" in logged


def test_a_fault_between_quota_errors_leaves_their_count(staged):
    answers = iter(["quota", "fault"] + ["quota"] * len(staged))

    def mixed(conv):
        if next(answers) == "quota":
            return conv["session_id"], None, True, None
        return conv["session_id"], None, False, "fault"

    with (
        patch.object(local, "collect_conversations", return_value=staged),
        patch("src.extract.claude_extract._get_client_and_model", return_value=(object(), "m")),
        patch.object(local, "extract_conversation_inline", side_effect=mixed) as ex,
    ):
        local.run_conversation_extraction()

    assert ex.call_count == local.CONSECUTIVE_FAIL_THRESHOLD + 1


def test_a_success_between_quota_errors_starts_the_count_again(staged):
    answers = iter([True] * (local.CONSECUTIVE_FAIL_THRESHOLD - 1) + [False] + [True] * len(staged))

    def mixed(conv):
        if next(answers):
            return conv["session_id"], None, True, None
        return _extraction_for(conv)

    more = [*staged, *({"session_id": f"more-{n}"} for n in range(5))]
    with (
        patch.object(local, "collect_conversations", return_value=more),
        patch("src.extract.claude_extract._get_client_and_model", return_value=(object(), "m")),
        patch.object(local, "extract_conversation_inline", side_effect=mixed) as ex,
    ):
        local.run_conversation_extraction()

    assert ex.call_count == local.CONSECUTIVE_FAIL_THRESHOLD * 2 < len(more)
