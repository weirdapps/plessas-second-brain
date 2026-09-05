"""Step 7 of `cmd_sync` has to stop on a clock, not on running out of work.

`run_conversation_extraction` took `limit` and nothing else, and `cmd_sync`
called it with no arguments at all, so `limit=0` meant "every pending
conversation, one at a time". That is unbounded by construction: the pending
list is whatever `sync-tokens-to-vps.sh` has rsynced since the last run, which
on a busy Claude Code day is dozens of transcripts, each its own LLM call.

The behaviour asserted here is the same contract Steps 6 and 8 already keep:
spend up to `deadline_s`, then leave the rest queued for the next run and the
nightly `sb-conversation-sync` unit, which has 1800 s rather than 600 s.
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
    return conv["session_id"], {"summary": "x"}, False


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

    def always_refuses(conv):
        return conv["session_id"], None, False

    with (
        patch.object(local, "collect_conversations", return_value=staged),
        patch("src.extract.claude_extract._get_client_and_model", return_value=(object(), "m")),
        patch.object(local, "extract_conversation_inline", side_effect=always_refuses),
    ):
        for _ in range(local.CONVERSATION_MAX_ATTEMPTS):
            local.run_conversation_extraction()

        with patch.object(local, "extract_conversation_inline") as ex:
            local.run_conversation_extraction()

    assert ex.call_count == 0


def test_giving_up_does_not_mark_a_conversation_extracted(staged, monkeypatch):
    """A given-up session must not land in processed_ids: that set is what the
    loader treats as done, so an abandoned transcript would read as ingested."""
    monkeypatch.setattr(local, "CONVERSATION_MAX_ATTEMPTS", 1)

    with (
        patch.object(local, "collect_conversations", return_value=staged),
        patch("src.extract.claude_extract._get_client_and_model", return_value=(object(), "m")),
        patch.object(
            local,
            "extract_conversation_inline",
            side_effect=lambda c: (c["session_id"], None, False),
        ),
    ):
        local.run_conversation_extraction()

    state = json.loads(local.CONV_STATE_FILE.read_text())

    assert state["processed_ids"] == []
    assert set(state["given_up_ids"]) == {c["session_id"] for c in staged}


def test_a_transient_failure_is_retried_before_the_cap(staged, monkeypatch):
    """Give-up must not fire on the first bad call. A single quota blip or a
    network reset has to stay recoverable, or one flaky minute permanently
    drops a conversation from the knowledge base."""
    monkeypatch.setattr(local, "CONVERSATION_MAX_ATTEMPTS", 3)

    with (
        patch.object(local, "collect_conversations", return_value=staged),
        patch("src.extract.claude_extract._get_client_and_model", return_value=(object(), "m")),
        patch.object(
            local,
            "extract_conversation_inline",
            side_effect=lambda c: (c["session_id"], None, False),
        ),
    ):
        local.run_conversation_extraction()

        with patch.object(local, "extract_conversation_inline", side_effect=_extraction_for) as ex:
            local.run_conversation_extraction()

    assert ex.call_count == len(staged)


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
