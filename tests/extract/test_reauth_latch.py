"""Tests for the process-wide failed-reauth latch in claude_extract.

On Linux ``reauth()`` has no local remedy: it polls for PUSH_WAIT_SECONDS (1020s)
waiting for the Mac's token push. ``call_with_policy`` runs it per item, and three of
its call sites drive a ThreadPoolExecutor (local.py:397, attachment_pipeline.py:361,
image_pipeline.py:284). Before the latch, an outage cost every worker its own full
wait: four workers spent 68 minutes discovering, four times over, the single fact the
first worker had already established in 17.

The latch stops the repetition without removing the capability. One wait still happens,
which is what the 30-minute units can afford and are meant to get.
"""

import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import google.auth.exceptions as gauth
import pytest

from src.extract import claude_extract
from src.llm_policy import ReauthResult

WORKERS = 4

# Deadline generous enough that decide() returns WAIT_FOR_PUSH rather than
# UNRECOVERABLE_AUTH. The budget test is `now + wait + max_call > deadline`, i.e.
# now + 1020 + 120 > now + BUDGET_SECONDS, which is false for anything above 1140.
# Written as a literal rather than imported from llm_policy on purpose: an expectation
# derived from the code it is checking cannot fail when that code's numbers move.
BUDGET_SECONDS = 5000

# Ceiling on how long the first worker is held inside reauth(), NOT the mechanism that
# detects a broken latch. See second_entrant_or_timeout: the first worker leaves the
# instant a second one arrives, so a broken latch is caught by arrival, not by outliving
# a sleep. This bound only stops a passing run from hanging.
HOLD_SECONDS = 0.5


@pytest.fixture(autouse=True)
def _reset_latch():
    """The latch is module-global state; a test that sets it would poison later ones."""
    claude_extract.reset_reauth_latch()
    yield
    claude_extract.reset_reauth_latch()


def _auth_error() -> gauth.RefreshError:
    """The failure that actually occurs on a stale ADC — classified AUTH_REAUTH_REQUIRED."""
    return gauth.RefreshError("invalid_grant: Bad Request")


def _ok_response():
    return types.SimpleNamespace(stop_reason="end_turn", content=[])


def test_concurrent_workers_pay_the_push_wait_only_once(monkeypatch):
    """Four workers hit an auth failure together; exactly one of them waits.

    Would this pass with the behaviour removed? No. Two mutations kill it:

      * Delete the latch and call ``reauth(is_linux=is_linux)`` directly, as the code
        did before: each of the four workers invokes it once before its Attempt's
        reauth_used flag stops it, so ``reauth_calls`` lands at 4.
      * Keep the flag but release the lock before calling reauth (check-then-call):
        all four pass the check while the flag is still clear and all four enter
        reauth. Also 4. This is the mutation that matters, because it is what an
        implementation guarding only the flag reads would look like, and it is
        invisible without forced overlap.

    TWO SYNCHRONISATION POINTS, BOTH FORCED RATHER THAN HOPED FOR. An earlier test in
    this project passed eight times out of eight with its mutex removed, purely on GIL
    scheduling, so neither of these is a sleep:

      * ``at_the_latch``, a Barrier(WORKERS) inside fn(), makes all four workers reach
        the latch simultaneously. If fewer arrive it raises and the test fails, so the
        run cannot quietly degrade into a sequential one that proves nothing.
      * ``second_entrant_or_timeout``, a Barrier(2) inside the stub reauth, holds the
        first worker until a SECOND worker enters. That inverts the detection: a broken
        latch is caught the instant another worker arrives, not by whether it happens to
        arrive inside a fixed sleep window. Under the correct latch nobody else ever
        enters, the barrier times out, and the test passes; under either mutation the
        second entrant releases it immediately and the count is already wrong. The
        failing path is therefore fast and certain rather than racing a timer.
    """
    monkeypatch.setenv("PTS_LLM_DEADLINE", repr(time.time() + BUDGET_SECONDS))
    # CI is ubuntu-latest and development is macOS; pin the branch under test.
    monkeypatch.setattr(claude_extract, "running_on_linux", lambda: True)

    at_the_latch = threading.Barrier(WORKERS, timeout=10)
    second_entrant_or_timeout = threading.Barrier(2, timeout=HOLD_SECONDS)
    reauth_calls: list[dict] = []
    calls_lock = threading.Lock()

    def slow_failing_reauth(**kwargs):
        with calls_lock:
            reauth_calls.append(kwargs)
        try:
            # Returns only when another worker also gets in here, which the latch is
            # supposed to make impossible. BrokenBarrierError is the PASSING path.
            second_entrant_or_timeout.wait()
        except threading.BrokenBarrierError:
            pass
        return ReauthResult.FAILED

    def worker(_):
        # Only the first SDK call rendezvouses: the barrier resets after WORKERS
        # arrivals, and the winning worker's retry would otherwise block on a barrier
        # the other three have already left.
        first_call = [True]

        def fn():
            if first_call[0]:
                first_call[0] = False
                at_the_latch.wait()
            raise _auth_error()

        with pytest.raises(gauth.RefreshError):
            claude_extract.call_with_policy(fn, max_call_seconds=120.0)
        return "failed-fast"

    with patch.object(claude_extract, "reauth", side_effect=slow_failing_reauth):
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            results = list(pool.map(worker, range(WORKERS)))

    assert len(reauth_calls) == 1
    # Every worker still terminates by raising, so the three that skipped the wait
    # failed fast rather than hanging or returning a bogus success.
    assert results == ["failed-fast"] * WORKERS


def test_a_failed_reauth_latches_for_a_later_caller(monkeypatch):
    """The latch outlives the call that set it. Sequential, so no timing is involved.

    Would this pass with the behaviour removed? No. Without the latch the second
    ``call_with_policy`` runs its own reauth and the count is 2. This is the same
    property as the concurrent test but isolated from thread scheduling entirely, so
    the two fail for different reasons: this one for a missing latch, that one for a
    racy one.
    """
    monkeypatch.setenv("PTS_LLM_DEADLINE", repr(time.time() + BUDGET_SECONDS))
    monkeypatch.setattr(claude_extract, "running_on_linux", lambda: True)

    reauth_calls: list[dict] = []

    def failing_reauth(**kwargs):
        reauth_calls.append(kwargs)
        return ReauthResult.FAILED

    def always_auth_fails():
        raise _auth_error()

    with patch.object(claude_extract, "reauth", side_effect=failing_reauth):
        for _ in range(2):
            with pytest.raises(gauth.RefreshError):
                claude_extract.call_with_policy(always_auth_fails, max_call_seconds=120.0)

    assert len(reauth_calls) == 1


def test_a_successful_reauth_leaves_the_latch_clear(monkeypatch):
    """Success must not latch: the credential is good, so later workers proceed.

    Would this pass with the behaviour removed? No. Latching on any result, or on
    anything other than FAILED, makes the second ``call_with_policy`` skip its reauth
    and fail; the count drops to 1 and, because the skip is terminal, the second call
    raises instead of returning a response. Both assertions below catch it.

    This is the test that keeps the capability the latch is not supposed to remove.
    """
    monkeypatch.setenv("PTS_LLM_DEADLINE", repr(time.time() + BUDGET_SECONDS))
    monkeypatch.setattr(claude_extract, "running_on_linux", lambda: True)

    reauth_calls: list[dict] = []

    def succeeding_reauth(**kwargs):
        reauth_calls.append(kwargs)
        return ReauthResult.SUCCEEDED

    def one_auth_error_then_ok():
        calls = []

        def fn():
            calls.append(1)
            if len(calls) == 1:
                raise _auth_error()
            return _ok_response()

        return fn

    with patch.object(claude_extract, "reauth", side_effect=succeeding_reauth):
        first = claude_extract.call_with_policy(one_auth_error_then_ok(), max_call_seconds=120.0)
        second = claude_extract.call_with_policy(one_auth_error_then_ok(), max_call_seconds=120.0)

    assert len(reauth_calls) == 2
    assert first is not None and second is not None
