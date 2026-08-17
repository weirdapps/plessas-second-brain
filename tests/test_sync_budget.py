"""Budget calibration for the hourly sync's per-run work.

`sb-outlook-sync` runs under `TimeoutStartSec=10min`. Step 6 called run_phase2()
with no limit, which is harmless while nothing new arrives and fatal the moment
it does: on 2026-08-18 the first unattended run after the attachment registrar
landed registered 200 files, and the ~100 LLM summaries that followed ran past
the deadline. systemd SIGTERMed it at 01:10:06 with `Result=timeout`, and
`Restart=on-failure` then queued the same overrun again.

Registration and text extraction are cheap and stay at 200 per run so the
backlog still drains in about two days. Only the LLM pass is capped here; the
nightly sb-attachments job drains the rest of it under its own 3390 s budget.

Asserted as arithmetic because the real thing takes ten minutes to fail.
"""

from src.cli import ATTACHMENT_REGISTER_LIMIT, PHASE2_SYNC_LIMIT

# systemd TimeoutStartSec on sb-outlook-sync.service.
SYNC_TIMEOUT_S = 600

# Measured span from unit start to Step 6 on the 2026-08-18 01:00 run: mail
# fetch, extraction, load, people dedup, embeddings rebuild, then Phase 1 over
# a full 200-file registration batch.
OBSERVED_WORK_BEFORE_PHASE2_S = 300

# Vision/summary calls against Vertex, rounded up from observed latency.
SECONDS_PER_LLM_CALL = 5


def test_phase2_pass_fits_inside_the_unit_timeout():
    worst_case = OBSERVED_WORK_BEFORE_PHASE2_S + PHASE2_SYNC_LIMIT * SECONDS_PER_LLM_CALL

    assert worst_case < SYNC_TIMEOUT_S, (
        f"worst-case sync is {worst_case}s against a {SYNC_TIMEOUT_S}s timeout"
    )


def test_phase2_cap_leaves_real_margin():
    """Landing just inside the deadline means the next slow Vertex call breaks it."""
    worst_case = OBSERVED_WORK_BEFORE_PHASE2_S + PHASE2_SYNC_LIMIT * SECONDS_PER_LLM_CALL

    assert worst_case < SYNC_TIMEOUT_S * 0.8


def test_registration_still_outpaces_the_llm_cap():
    """Registering is cheap; throttling it to the LLM rate would stretch the
    backlog from days into weeks for no benefit."""
    assert ATTACHMENT_REGISTER_LIMIT > PHASE2_SYNC_LIMIT
