"""Shared Vertex LLM retry and auth policy.

Vendored by copy into plessas-trading-stack, news and plessas-second-brain.
The canonical copy lives here; `scripts/sync-llm-policy.sh` propagates it and a
SHA256 drift test in each repo fails if a copy is edited locally.

Design contract, from docs/superpowers/specs/2026-08-09-llm-policy-design.md:
  * `decide()` is PURE. No clock, no environment, no subprocess. `now` is a
    parameter. This is what makes the table exhaustively testable.
  * `reauth()` is the only impure function, and it exists here rather than in a
    transport because the remedy is host-dependent, not transport-dependent.
  * stdlib only. Three repos with three different dependency sets import this.
"""

from __future__ import annotations

import enum
import json
import math
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

MAX_ATTEMPTS = 4
DEFAULT_BUDGET_SECONDS = 900
PUSH_INTERVAL_SECONDS = 900
PUSH_TOLERANCE_SECONDS = 120
# `decide()` RESERVES this against the budget and `reauth()` SPENDS it. The design is
# only correct while the reservation equals the spend, so both read it from here rather
# than each recomputing the sum: two literals that merely happen to agree are one edit
# away from giving up sixteen minutes before the push lands, on the one host where
# waiting is the only remedy.
PUSH_WAIT_SECONDS = float(PUSH_INTERVAL_SECONDS + PUSH_TOLERANCE_SECONDS)


class Outcome(enum.Enum):
    """What the transport observed. Mapped from an envelope or an exception."""

    OK = "ok"
    AUTH_REAUTH_REQUIRED = "auth_reauth_required"
    REFUSAL = "refusal"
    RATE_LIMIT = "rate_limit"
    API_ERROR = "api_error"
    TIMEOUT = "timeout"
    EMPTY = "empty"
    UNPARSEABLE = "unparseable"
    TRUNCATED = "truncated"


class Action(enum.Enum):
    """What the caller must do next."""

    RETURN = "return"
    REAUTH_RETRY = "reauth_retry"
    WAIT_FOR_PUSH = "wait_for_push"
    PLAIN_RETRY = "plain_retry"
    GIVE_UP = "give_up"
    UNRECOVERABLE_AUTH = "unrecoverable_auth"


class ReauthResult(enum.Enum):
    """Tri-state. SKIPPED means "not attempted", so it must not spend the budget."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class Attempt:
    """Immutable attempt accounting.

    Two counters, deliberately. A single scalar is evadable: an interleaving of
    EMPTY, TIMEOUT, EMPTY, TIMEOUT never advances either row past its cap. The
    global `total` closes that hole.
    """

    total: int = 0
    per_outcome: Mapping[Outcome, int] = field(default_factory=dict)
    reauth_used: bool = False

    def bump(self, outcome: Outcome) -> Attempt:
        counts = dict(self.per_outcome)
        counts[outcome] = counts.get(outcome, 0) + 1
        return Attempt(self.total + 1, counts, self.reauth_used)

    def with_reauth_used(self) -> Attempt:
        return Attempt(self.total, dict(self.per_outcome), True)

    def count(self, outcome: Outcome) -> int:
        return self.per_outcome.get(outcome, 0)


@dataclass(frozen=True)
class Decision:
    action: Action
    sleep_s: float
    reason: str


def backoff(n: int, outcome: Outcome) -> float:
    """Exponential backoff, capped. ``n`` is the per-outcome occurrence count.

    ``n >= 1`` for every call reached through ``decide()``, which rejects an
    attempt whose count for the outcome being decided is zero. The guard tests
    the same quantity passed here, so the two cannot drift apart.
    """
    exponent = n - 1
    if outcome is Outcome.RATE_LIMIT:
        return min(600.0, 60.0 * (2.0**exponent))
    return min(300.0, 30.0 * (2.0**exponent))


def resolve_deadline(now: float, env: Mapping[str, str]) -> float:
    """Absolute deadline for this run, from ``PTS_LLM_DEADLINE`` or a default.

    ``now`` and the returned value are POSIX seconds, on the same clock as
    ``PTS_LLM_DEADLINE`` itself: callers pass ``time.time()``, never
    ``time.monotonic()``. The variable can only be wall clock -- monotonic time has no
    fixed epoch, so no systemd runner could compute a value for it, and the deadline
    formula is ``start + TimeoutStartSec - margin``, which is wall-clock arithmetic. A
    monotonic ``now`` (uptime, ~1e5) compared against an epoch deadline (~1.79e9) makes
    ``now + sleep + max_call > deadline`` permanently false, which switches the whole
    deadline mechanism off silently, exactly when an operator has bothered to set it.

    ``env`` is a parameter rather than a direct ``os.environ`` read so this stays
    pure and testable. Every consumer must call this instead of reading the
    variable itself: three repos inventing three defaults is the divergence this
    module exists to end.

    An unset, blank, unparseable or non-finite value yields
    ``now + DEFAULT_BUDGET_SECONDS``, which is deliberately shorter than any
    scheduled unit's real budget, so a misconfiguration degrades to giving up
    early rather than running unbounded.  ``float("inf")`` and ``float("nan")``
    parse without raising, so a plain ``ValueError`` catch is not enough: against
    ``inf`` the budget check ``now + sleep + max_call > deadline`` is never true,
    and against ``nan`` every comparison is false -- either one silently disables
    the deadline mechanism entirely.
    """
    raw = env.get("PTS_LLM_DEADLINE", "").strip()
    if not raw:
        return now + DEFAULT_BUDGET_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return now + DEFAULT_BUDGET_SECONDS
    if not math.isfinite(value):
        return now + DEFAULT_BUDGET_SECONDS
    return value


# Give up once the per-outcome count EXCEEDS this value.
ROW_CAPS: Mapping[Outcome, int] = {
    Outcome.REFUSAL: 2,
    # RATE_LIMIT=3: the lower bound is pinned by the at-cap test (count=3 must still retry);
    # lowering this to 2 would break that test. The upper bound is unreachable: the row cap
    # fires when count > 3, i.e. count >= 4; since total >= count always holds, total >= 4 ==
    # MAX_ATTEMPTS fires first. RATE_LIMIT therefore gets 4 attempts where cap=2 outcomes get
    # 3 — the intended behaviour, enforced by the global cap rather than by this row entry.
    # Do not "tidy" this to 2: that changes the observable retry count, not just the comment.
    Outcome.RATE_LIMIT: 3,
    Outcome.API_ERROR: 2,
    Outcome.TIMEOUT: 2,
    Outcome.EMPTY: 1,
    Outcome.UNPARSEABLE: 1,
}


def decide(
    outcome: Outcome,
    attempt: Attempt,
    now: float,
    deadline: float,
    max_call_seconds: float,
    *,
    is_linux: bool = False,
) -> Decision:
    """Decide what to do after one LLM call. Pure.

    `attempt` must already include the current outcome (call `bump()` first). This
    is enforced for every non-OK outcome: a caller that never bumps holds every
    counter at 0, so no cap is ever reachable, every call returns PLAIN_RETRY, and
    the loop burns the whole budget in paid calls -- or never terminates at all when
    the deadline is distant. Violating it raises `ValueError`, which is a real failure
    mode of this function and part of the caller contract.

    `OK` is the only exemption, and "it returns before `backoff()` or `ROW_CAPS` are
    reached" is NOT the criterion -- `TRUNCATED` returns before both and is still
    guarded. The boundary is narrower than that: `OK` is the one outcome where raising
    would destroy something, a completed and already billed response. Do not widen the
    exemption to another early-returning outcome on the "returns first" reasoning alone.

    `is_linux` selects the auth remedy and MUST be the same value the caller passes to
    `reauth()` in the same run; `running_on_linux()` is the intended source for both.
    It stays an explicit parameter here, with no host detection, because `decide()` is
    pure. Its default of False is wrong for seventeen of the eighteen scheduled jobs.

    Caller contract for the two auth actions: after acting on REAUTH_RETRY or
    WAIT_FOR_PUSH, the caller MUST record the spent budget with
    `Attempt.with_reauth_used()`, regardless of the outcome of the remedy (see
    `reauth()` for the sole exception, ReauthResult.SKIPPED, which spends nothing).
    Without that call the auth row never yields UNRECOVERABLE_AUTH; it yields a
    plain GIVE_UP instead -- a semantically different terminal action that
    downstream integrations branch on.

    Evaluation order matters and is fixed by the spec:
      0. OK short-circuits, BEFORE any deadline test. A completed, billed
         response that arrived late is still a response; discarding it was a
         defect in the first draft of this design.
      0b. Bump guard: non-OK outcomes require ``attempt.bump(outcome)`` to have
          been called. Placed after OK so a success is never lost to a
          bookkeeping slip.
      0c. TRUNCATED gives up immediately; a retry with identical parameters
          re-truncates, so it is pure waste.
      1. Global attempt cap.
      2. Auth, which is host-dependent (Task 3).
      3. The row, then the forward-looking budget test (Task 4).
    """
    if outcome is Outcome.OK:
        return Decision(Action.RETURN, 0.0, "ok")

    # The guard sits BELOW the OK short-circuit on purpose. It protects backoff()
    # from n=0 and keeps the retry budget meaningful, and neither concern exists on
    # the success path. Raising here would discard a completed, billed response
    # over a bookkeeping slip, which is the exact failure the OK-first ordering was
    # introduced to prevent.
    #
    # That destroyed value, not "OK returns early", is what earns the exemption. The
    # TRUNCATED branch below also returns before backoff() and ROW_CAPS and is still
    # guarded, because a caller that mis-bumps a TRUNCATED loses nothing worth keeping.
    # Only OK sits above this line.
    if attempt.count(outcome) < 1:
        raise ValueError(
            f"decide() requires attempt.bump({outcome!r}) first: the caller bumps the "
            f"outcome it is deciding on, so count is at least 1 on entry. Got "
            f"count={attempt.count(outcome)}, total={attempt.total}."
        )

    if outcome is Outcome.TRUNCATED:
        return Decision(
            Action.GIVE_UP, 0.0, "truncated at max_tokens; an identical retry re-truncates"
        )

    if attempt.total >= MAX_ATTEMPTS:
        return Decision(Action.GIVE_UP, 0.0, f"global attempt cap {MAX_ATTEMPTS} reached")

    if outcome is Outcome.AUTH_REAUTH_REQUIRED:
        if attempt.reauth_used:
            return Decision(Action.UNRECOVERABLE_AUTH, 0.0, "reauth already attempted this process")
        if is_linux:
            # The VPS has no re-auth: its ADC holds only a refresh_token, and
            # gcloud-refresh.sh detects staleness without curing it. The remedy is
            # the Mac's 15-minute token push, so the only correct move is to wait.
            wait = PUSH_WAIT_SECONDS
            if now + wait + max_call_seconds > deadline:
                return Decision(
                    Action.UNRECOVERABLE_AUTH,
                    0.0,
                    "remaining budget cannot fund a token-push wait",
                )
            # sleep_s is 0.0 on purpose: reauth() owns the wait. The reservation
            # above still charges the budget for it, because the time is spent
            # either way; only the party spending it differs.
            return Decision(Action.WAIT_FOR_PUSH, 0.0, "waiting for the Mac token push")
        return Decision(Action.REAUTH_RETRY, 0.0, "running the host reauth script")

    cap = ROW_CAPS[outcome]
    seen = attempt.count(outcome)
    if seen > cap:
        return Decision(Action.GIVE_UP, 0.0, f"{outcome.value} cap {cap} exceeded")

    sleep_s = backoff(seen, outcome)
    if now + sleep_s + max_call_seconds > deadline:
        return Decision(Action.GIVE_UP, 0.0, "the next attempt would not fit inside the budget")
    return Decision(Action.PLAIN_RETRY, sleep_s, f"retry after {outcome.value}")


MAC_REAUTH_SCRIPT = (
    Path.home() / "SourceCode" / "claude-config" / "scripts" / "local-bin" / "gcloud-auto-login.sh"
)

_REAUTH_LOCK = threading.Lock()
_POST_REAUTH: list[Callable[[], None]] = []


def register_post_reauth(callback: Callable[[], None]) -> None:
    """Register transport state to invalidate after a successful reauth.

    second-brain registers `reset_client_cache`: its AnthropicVertex client is
    cached, so without this a retry reuses the stale in-memory credential and the
    reauth is structurally incapable of helping. news registers nothing.
    """
    _POST_REAUTH.append(callback)


def reset_post_reauth() -> None:
    """Clear registered callbacks. For tests."""
    _POST_REAUTH.clear()


def default_adc_probe() -> bool:
    """True when Application Default Credentials currently work.

    Deliberately ADC and not `gcloud auth print-access-token`: the user token can
    look valid while ADC is stale, which is the false-green that let an
    invalid_rapt ship an unsynthesised digest on 2026-08-01.
    """
    try:
        result = subprocess.run(
            ["gcloud", "auth", "application-default", "print-access-token"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def running_on_linux() -> bool:
    """True on the VPS, False on the Mac. The intended source for both `is_linux` flags.

    Named so it does not collide with `reauth()`'s `is_linux` parameter, whose body has
    to be able to call it. Seventeen of the eighteen scheduled LLM jobs run on the VPS,
    so the useful value here is the detected one, not either literal.
    """
    return sys.platform.startswith("linux")


def reauth(
    *,
    script: Path | None = None,
    adc_probe: Callable[[], bool] | None = None,
    timeout: float = 200.0,
    is_linux: bool | None = None,
    poll_interval: float = 30.0,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> ReauthResult:
    """Attempt a host re-auth. Serialised process-wide. Never trusts an exit code.

    Caller contract: after acting on REAUTH_RETRY or WAIT_FOR_PUSH, the caller
    MUST record the spent budget with ``Attempt.with_reauth_used()`` regardless of
    the returned ReauthResult. The sole exception is SKIPPED, which by definition
    did not spend it. Without that call the auth row never yields
    UNRECOVERABLE_AUTH; it yields a plain GIVE_UP instead, a semantically
    different terminal action that downstream integrations branch on.

    On Linux there is no local remedy: the ADC file holds only a refresh token and
    ``gcloud-refresh.sh`` detects staleness without curing it. The only cure is the
    Mac's 15-minute token push, so this polls for ADC to recover and fires the
    post-reauth callbacks when it does. Those callbacks are load-bearing: a
    transport that caches its API client will otherwise retry with the dead
    credential still in memory.

    `is_linux` selects the remedy and MUST match the value the caller passed to
    `decide()` in the same run; `running_on_linux()` is the intended source for both.
    Passing it to `decide()` and forgetting it here yields WAIT_FOR_PUSH followed by the
    macOS script path on a headless VPS: no wait, no callbacks, and a plausible-looking
    FAILED. The default of None therefore detects the host here rather than assuming a
    Mac -- this function is already impure, so the detection costs it nothing. Only tests
    and callers that genuinely know better should pass a literal.
    """
    probe = adc_probe or default_adc_probe
    if is_linux is None:
        is_linux = running_on_linux()

    with _REAUTH_LOCK:
        if probe():
            return ReauthResult.SKIPPED

        if is_linux:
            waited = 0.0
            budget = PUSH_WAIT_SECONDS
            # A non-positive poll_interval freezes this loop: `step` is 0 so `waited`
            # never advances, or is negative and walks it backwards. Measured: 200,000
            # probes with poll_interval=0.0 and no progress. Clamp rather than raise --
            # this is the one function whose entire promise is that it terminates.
            poll = max(poll_interval, 1.0)
            while waited < budget:
                step = min(poll, budget - waited)
                sleep_fn(step)
                waited += step
                if probe():
                    for callback in _POST_REAUTH:
                        callback()
                    return ReauthResult.SUCCEEDED
            return ReauthResult.FAILED

        # Explicit argument wins over the env override, so tests are hermetic even on a
        # machine that happens to export PTS_REAUTH_CMD.
        override = os.environ.get("PTS_REAUTH_CMD", "").strip()
        target = script or (Path(override) if override else MAC_REAUTH_SCRIPT)
        if not target.exists():
            return ReauthResult.FAILED
        try:
            result = subprocess.run(
                [str(target)], capture_output=True, text=True, timeout=timeout, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return ReauthResult.FAILED
        if result.returncode != 0:
            return ReauthResult.FAILED
        if not probe():
            # Exit 0 but ADC still dead: the script took a no-op path (lock held,
            # cooldown, already-authed). Not attempted, so do not spend the budget.
            return ReauthResult.SKIPPED
        for callback in _POST_REAUTH:
            callback()
        return ReauthResult.SUCCEEDED


def trace(
    path: Path,
    *,
    job: str,
    call_site: str,
    model: str,
    region: str,
    outcome: Outcome,
    action: Action,
    attempt: Attempt,
    latency_ms: int,
    ts: float,
    in_tok: int | None = None,
    out_tok: int | None = None,
) -> None:
    """Append one decision record. Never raises: telemetry must not fell a job."""
    record: dict[str, object] = {
        "ts": ts,
        "job": job,
        "call_site": call_site,
        "model": model,
        "region": region,
        "outcome": outcome.value,
        "action": action.value,
        "attempt": attempt.total,
        "latency_ms": latency_ms,
    }
    if in_tok is not None:
        record["in_tok"] = in_tok
    if out_tok is not None:
        record["out_tok"] = out_tok
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
    except Exception:
        # Deliberately broad. The docstring's promise is absolute, and the failure
        # modes are not all OSError: a `str` passed where a Path is declared raises
        # AttributeError on `.parent`, which under a narrow catch would fell the very
        # job this record exists to explain.
        return
