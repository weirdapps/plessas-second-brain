"""The hourly sync's stages must sum to less than its unit timeout.

`sb-outlook-sync` runs under `TimeoutStartSec=600`. Its stages were budgeted
independently — some by count, some not at all — and bounding them one at a
time failed three times in a row on 2026-08-18: registration (#24), then Phase 2
by count (#27), then Phase 1 (#28). Each fix stopped one overrun and revealed
the next, because independent per-stage limits cannot sum to a guarantee. Every
scheduled run that day was SIGTERMed at ten minutes, with only the 90 s retry
completing.

This file is the missing guarantee: the budgets are asserted together, so
raising any one of them fails here rather than in production ten minutes later.

Both slow stages needed a wall-clock bound rather than a count, for opposite
reasons. Phase 1 varies by three orders of magnitude per item (a text part is
~0.1 s, OCR — the most common extraction_method in this corpus — or a 30 MB
.xlsb workbook runs for minutes). Phase 2 is network-bound, so 25 calls read as
modest and cost 6-8 minutes at ~15-25 s each. The giveaway on the last failed
run was 1 min 20 s of CPU across 11 min 30 s of wall clock.
"""

from src.cli import (
    IMAGE_CLASSIFY_SYNC_BUDGET_S,
    PHASE1_SYNC_DEADLINE_S,
    PHASE2_SYNC_DEADLINE_S,
    SYNC_FIXED_WORK_S,
    SYNC_UNIT_TIMEOUT_S,
)

# Stage 7 (conversation sync) is small and has no budget of its own.
STEP7_ALLOWANCE_S = 30.0


def _worst_case_run_s() -> float:
    return (
        SYNC_FIXED_WORK_S
        + PHASE1_SYNC_DEADLINE_S
        + PHASE2_SYNC_DEADLINE_S
        + STEP7_ALLOWANCE_S
        + IMAGE_CLASSIFY_SYNC_BUDGET_S
    )


def test_all_stage_budgets_fit_inside_the_unit_timeout():
    worst = _worst_case_run_s()

    assert worst < SYNC_UNIT_TIMEOUT_S, (
        f"stages sum to {worst:.0f}s against a {SYNC_UNIT_TIMEOUT_S:.0f}s timeout"
    )


def test_the_sum_keeps_real_margin():
    """Landing just inside means one slow Vertex call reintroduces the timeout.
    The failures being guarded here were 15-90 s over, not milliseconds."""
    worst = _worst_case_run_s()

    assert worst < SYNC_UNIT_TIMEOUT_S * 0.85, (
        f"only {SYNC_UNIT_TIMEOUT_S - worst:.0f}s of headroom"
    )


def test_image_step_does_not_claim_most_of_the_run():
    """IMAGE_CLASSIFY_BUDGET_S defaults to 480 s because it was sized for the
    30-minute daily/catch-up units. Inherited unchanged it takes 80% of this
    unit, which is why runs still died once Step 6 was bounded."""
    assert IMAGE_CLASSIFY_SYNC_BUDGET_S < SYNC_UNIT_TIMEOUT_S * 0.25


def test_the_two_llm_bound_stages_are_budgeted_in_time_not_count():
    """Regression guard on the shape of the fix: these must stay floats used as
    deadlines. A count reintroduces the bug the moment per-item cost changes."""
    assert isinstance(PHASE1_SYNC_DEADLINE_S, float)
    assert isinstance(PHASE2_SYNC_DEADLINE_S, float)
