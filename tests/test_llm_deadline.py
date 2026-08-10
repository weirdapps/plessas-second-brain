"""Tests for PTS_LLM_DEADLINE computation and installation.

Every expected number below is written as a literal. None is derived from the module
under test: an expectation computed from the code it is checking cannot fail when that
code's numbers move, which is the whole failure it is supposed to catch.

Live reference values, read from the VPS on 2026-08-11 with
`systemctl --user show sb-<name>.service -p LoadState -p TimeoutStartUSec -p
TimeoutStopUSec`:

    sb-attachments  1h     sb-curate-docs    30min    sb-outlook-sync  10min
    sb-daily-sync   30min  sb-news-sync      30min    sb-teams-sync    10min
    sb-noon-catchup 30min  sb-reverse-ingest 30min    sb-calendar-sync  5min
    sb-conversation-sync 5min          TimeoutStopUSec = 1min 30s on all ten
"""

import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from src import llm_deadline
from src.cli import main as cli_main

# TimeoutStartSec of every scheduled unit, and the budget each one yields once the
# 210s reserve (max_call 120 + shutdown grace 90) is held back.
UNIT_TIMEOUT_AND_BUDGET = {
    "sb-attachments": (3600, 3390),
    "sb-calendar-sync": (300, 90),
    "sb-conversation-sync": (300, 90),
    "sb-curate-docs": (1800, 1590),
    "sb-daily-sync": (1800, 1590),
    "sb-news-sync": (1800, 1590),
    "sb-noon-catchup": (1800, 1590),
    "sb-outlook-sync": (600, 390),
    "sb-reverse-ingest": (1800, 1590),
    "sb-teams-sync": (600, 390),
}

# PUSH_WAIT_SECONDS (900 + 120) plus one worst-case call (120). decide() grants a
# token-push wait only to a budget at or above this.
WAIT_NEEDS_SECONDS = 1140


@pytest.fixture(autouse=True)
def _clean_deadline_env():
    """PTS_LLM_DEADLINE is real process state; install_llm_deadline sets it for keeps.

    monkeypatch cannot undo a variable the code under test created, so pop it by hand
    on both sides. Without this, the first test to install a deadline makes every later
    test exercise the setdefault-kept branch instead of the one it meant to.
    """
    os.environ.pop("PTS_LLM_DEADLINE", None)
    yield
    os.environ.pop("PTS_LLM_DEADLINE", None)


def _fake_systemctl(stdout: str, returncode: int = 0):
    """Stand-in for subprocess.run returning one `systemctl show` result."""

    def _run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args, returncode=returncode, stdout=stdout, stderr=""
        )

    return _run


def _no_systemctl(*args, **kwargs):
    """systemctl absent, as on every developer Mac and in CI."""
    raise FileNotFoundError("systemctl")


# --------------------------------------------------------------------------- reserve


def test_the_reserve_is_a_function_of_max_call_not_a_constant():
    """Doubling max_call must move the reserve by exactly that much.

    Would this pass with the behaviour removed? No. Replacing the body with a literal
    `return 210.0` — the tempting simplification, since 210 is the only value today's
    call sites produce — makes both assertions fail. This is the mutation that went
    unnoticed in news until a comment caught it.
    """
    assert llm_deadline._deadline_reserve_seconds(120.0) == 210.0
    assert llm_deadline._deadline_reserve_seconds(240.0) == 330.0


def test_the_reserve_excludes_the_largest_backoff():
    """The 240s max backoff is deliberately not reserved; decide() already refuses it.

    Would this pass with the behaviour removed? No. Adding max_backoff back, as the
    spec's formula says, makes the reserve 450 and this assertion reads 210.
    Concretely it would also push the two 300s units to a margin of minus 150 and stop
    them booting, which test_every_scheduled_unit_has_a_positive_margin then catches.
    """
    assert llm_deadline._deadline_reserve_seconds(120.0) == 210.0


# ---------------------------------------------------------------------------- budget


@pytest.mark.parametrize(
    ("unit", "timeout", "budget"), [(u, t, b) for u, (t, b) in UNIT_TIMEOUT_AND_BUDGET.items()]
)
def test_each_unit_gets_its_own_budget(unit, timeout, budget):
    """Ten units, ten budgets, each an exact literal.

    Would this pass with the behaviour removed? No. Returning the flat
    DEFAULT_BUDGET_SECONDS of 900 that the estate falls back to today — the exact
    status quo this task exists to replace — matches none of the ten.
    """
    assert llm_deadline._llm_budget_seconds(unit, 120.0, unit_timeout_seconds=timeout) == budget


def test_every_scheduled_unit_has_a_positive_margin():
    """Nothing in the estate refuses to boot under the 210s reserve.

    The smallest unit is 300s and lands at exactly +90. Verified independently rather
    than taken on trust, because a reserve that bricks a production unit is the one
    failure mode of this whole mechanism.

    Would this pass with the behaviour removed? It is a guard rather than a behaviour,
    so the mutation it answers is to the reserve, not to this file: any reserve at or
    above 300 (adding max_backoff gets there, at 450) makes _llm_budget_seconds raise
    for the two 5-minute units and this test error out.
    """
    margins = {
        unit: llm_deadline._llm_budget_seconds(unit, 120.0, unit_timeout_seconds=timeout)
        for unit, (timeout, _) in UNIT_TIMEOUT_AND_BUDGET.items()
    }
    assert min(margins.values()) == 90
    assert all(m > 0 for m in margins.values())


def test_only_the_long_units_can_fund_a_token_push_wait():
    """Six units can afford the wait, four cannot. The split is the point of the port.

    Would this pass with the behaviour removed? No. Under today's flat 900s default,
    900 < 1140, so NO unit funds a wait and the qualifying set is empty rather than
    these six. Under a per-unit budget it is exactly these six.
    """
    funds_wait = {
        unit
        for unit, (timeout, _) in UNIT_TIMEOUT_AND_BUDGET.items()
        if llm_deadline._llm_budget_seconds(unit, 120.0, unit_timeout_seconds=timeout)
        >= WAIT_NEEDS_SECONDS
    }
    assert funds_wait == {
        "sb-attachments",
        "sb-curate-docs",
        "sb-daily-sync",
        "sb-news-sync",
        "sb-noon-catchup",
        "sb-reverse-ingest",
    }


def test_a_unit_that_cannot_fund_one_call_refuses_to_run():
    """Non-positive margin is a RuntimeError at startup, not a SIGTERM later.

    Would this pass with the behaviour removed? No. Deleting the `if budget <= 0`
    guard returns -10.0 and pytest.raises fails.
    """
    with pytest.raises(RuntimeError, match="cannot fund a single LLM call"):
        llm_deadline._llm_budget_seconds("sb-daily-sync", 120.0, unit_timeout_seconds=200)


def test_a_margin_of_exactly_zero_also_refuses():
    """The boundary is `<= 0`, not `< 0`: zero budget funds nothing.

    Would this pass with the behaviour removed? No. Loosening the guard to `budget < 0`
    returns 0.0 for a 210s unit instead of raising.
    """
    with pytest.raises(RuntimeError):
        llm_deadline._llm_budget_seconds("sb-daily-sync", 120.0, unit_timeout_seconds=210)


def test_an_unknown_unit_gets_no_budget():
    """Would this pass with the behaviour removed? No. Defaulting an unknown unit to
    any number at all — the guessed budget this design refuses — makes this None fail.
    """
    assert llm_deadline._llm_budget_seconds("sb-invented", 120.0) is None


# ------------------------------------------------------------------ unit detection

# Exactly what /proc/self/cgroup contained on the VPS on 2026-08-11, cgroup v2.
CGROUP_V2 = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/{unit}.service\n"


def _cgroup_file(base, contents: str) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    f = base / "cgroup"
    f.write_text(contents)
    return f


def test_the_unit_comes_from_the_cgroup(tmp_path):
    """Would this pass with the behaviour removed? No. Returning None unconditionally —
    which is what an entrypoint with no way to identify itself is reduced to — fails
    this, and with it every deadline in production.
    """
    path = _cgroup_file(tmp_path, CGROUP_V2.format(unit="sb-daily-sync"))
    assert llm_deadline._detect_systemd_unit(path) == "sb-daily-sync"


def test_two_units_running_identical_argv_are_told_apart(tmp_path):
    """The reason detection is not the CLI subcommand. sb-noon-catchup and
    sb-outlook-sync both invoke `src.cli sync --engine claude --workers 8
    --skip-export`, byte for byte, and their timeouts differ by 1200s. Nothing in argv
    separates them; the cgroup does, and the budgets that come out must differ.

    Would this pass with the behaviour removed? No. Any argv-derived mapping gives both
    units the same answer, so one of these two assertions is wrong whichever value it
    picks. This test is the specification of the design decision.
    """
    noon = _cgroup_file(tmp_path / "a", CGROUP_V2.format(unit="sb-noon-catchup"))
    outlook = _cgroup_file(tmp_path / "b", CGROUP_V2.format(unit="sb-outlook-sync"))

    assert llm_deadline._detect_systemd_unit(noon) == "sb-noon-catchup"
    assert llm_deadline._detect_systemd_unit(outlook) == "sb-outlook-sync"
    assert llm_deadline._llm_budget_seconds("sb-noon-catchup", 120.0) == 1590
    assert llm_deadline._llm_budget_seconds("sb-outlook-sync", 120.0) == 390


def test_the_enclosing_user_manager_slice_is_not_mistaken_for_the_unit(tmp_path):
    """Every cgroup path contains `user@1000.service`, which also ends in `.service`.

    Would this pass with the behaviour removed? No. Returning the first `.service`
    segment found without checking it against the timeout table yields "user@1000"
    here instead of None — and, worse, on a real unit it would depend on scan order.
    """
    path = _cgroup_file(
        tmp_path, "0::/user.slice/user-1000.slice/user@1000.service/app.slice/other.service\n"
    )
    assert llm_deadline._detect_systemd_unit(path) is None


def test_a_future_sb_unit_absent_from_the_table_gets_no_deadline(tmp_path):
    """Membership is an exact table key, never a prefix or a glob.

    A `sb-something-new.service` added to the estate later has no entry here, so its
    real TimeoutStartSec is unknown. It must get no deadline rather than the wrong one:
    matching on the `sb-` prefix would hand it whatever budget the lookup fell through
    to, which is the guessed budget this design refuses.

    Would this pass with the behaviour removed? No, and it was a genuine gap — nothing
    caught this before. Relaxing the check to `unit.startswith("sb-")` leaves all 754
    other tests green, including the user@1000 one above, because "user@1000" and
    "other" fail a prefix test for unrelated reasons. Only an sb-prefixed name that is
    absent from the table separates exact membership from a glob.
    """
    path = _cgroup_file(tmp_path, CGROUP_V2.format(unit="sb-something-new"))
    assert llm_deadline._detect_systemd_unit(path) is None


def test_a_hand_run_over_ssh_has_no_unit(tmp_path):
    """An interactive session sits in a session-N.scope, not a .service.

    Would this pass with the behaviour removed? No. Falling back to any default unit
    when no match is found gives a hand-run somebody else's budget.
    """
    path = _cgroup_file(tmp_path, "0::/user.slice/user-1000.slice/session-42.scope\n")
    assert llm_deadline._detect_systemd_unit(path) is None


def test_cgroup_v1_multi_line_layout_also_resolves(tmp_path):
    """v1 writes one line per controller. Scanning every segment of every line covers
    both versions; a v2-only parser that split on "::" would return None here.
    """
    path = _cgroup_file(
        tmp_path,
        "12:pids:/user.slice/user-1000.slice/sb-teams-sync.service\n"
        "3:memory:/user.slice/user-1000.slice/sb-teams-sync.service\n",
    )
    assert llm_deadline._detect_systemd_unit(path) == "sb-teams-sync"


def test_no_proc_filesystem_means_no_unit(tmp_path):
    """Every developer Mac takes this path.

    Would this pass with the behaviour removed? No. Letting the OSError escape instead
    of catching it makes this raise, which would break the CLI on macOS outright.
    """
    assert llm_deadline._detect_systemd_unit(tmp_path / "absent") is None


def test_the_entrypoint_hook_installs_nothing_off_systemd(monkeypatch):
    """The hook is what src/cli.py calls, so it must be inert on a Mac and in CI.

    Would this pass with the behaviour removed? No. A hook that guessed a unit rather
    than accepting None would set PTS_LLM_DEADLINE on every developer machine.
    """
    monkeypatch.setattr(llm_deadline, "_detect_systemd_unit", lambda *a, **k: None)
    assert llm_deadline.install_llm_deadline_for_this_process() is None
    assert "PTS_LLM_DEADLINE" not in os.environ


def test_the_entrypoint_hook_installs_the_detected_units_budget(monkeypatch):
    """Would this pass with the behaviour removed? No. A hook that ignored the detected
    unit, or dropped the install call, leaves PTS_LLM_DEADLINE unset.
    """
    monkeypatch.setattr(llm_deadline, "_detect_systemd_unit", lambda *a, **k: "sb-attachments")
    monkeypatch.setattr(llm_deadline.subprocess, "run", _no_systemctl)
    installed = llm_deadline.install_llm_deadline_for_this_process()
    assert installed == pytest.approx(float(os.environ["PTS_LLM_DEADLINE"]))
    # 3600 - 210. Compared against the clock the hook itself read, since it takes no
    # `now`; the window is milliseconds and the tolerance is a second.
    assert installed - time.time() == pytest.approx(3390, abs=1.0)


def test_the_hook_does_not_stack_log_handlers_when_called_repeatedly(monkeypatch):
    """The handler install is idempotent: N calls, at most one handler.

    Nothing stops a process calling the hook more than once — an entrypoint that grew a
    second call, or a test module invoking it per case. Each extra StreamHandler would
    duplicate every subsequent line, so a log that exists to be read becomes a log that
    is read twice and trusted less.

    Would this pass with the behaviour removed? No. Dropping the `not logger.handlers`
    half of the guard — keeping only the root-handlers check, which is the natural way
    to write it if idempotency is not in mind — makes the third call leave 3 handlers
    and this assertion reads 1.
    """
    logger = logging.getLogger(llm_deadline.__name__)
    monkeypatch.setattr(llm_deadline, "_detect_systemd_unit", lambda *a, **k: None)
    # Strip both the root's handlers and this logger's, so the install branch is the one
    # actually taken. pytest installs a root handler of its own, which would otherwise
    # short-circuit the guard and make this test vacuous.
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    monkeypatch.setattr(logger, "handlers", [])

    for _ in range(3):
        llm_deadline.install_llm_deadline_for_this_process()

    assert len(logger.handlers) == 1


def test_the_hook_defers_to_existing_logging_configuration(monkeypatch):
    """When real logging config exists, add nothing and let the record propagate.

    Would this pass with the behaviour removed? No. Dropping the root-handlers half of
    the guard installs a private handler alongside the application's own, so every line
    from this module is emitted twice under any process that called basicConfig.
    """
    logger = logging.getLogger(llm_deadline.__name__)
    monkeypatch.setattr(llm_deadline, "_detect_systemd_unit", lambda *a, **k: None)
    monkeypatch.setattr(logging.getLogger(), "handlers", [logging.NullHandler()])
    monkeypatch.setattr(logger, "handlers", [])

    llm_deadline.install_llm_deadline_for_this_process()

    assert logger.handlers == []


# ----------------------------------------------------------------- duration parsing


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [
        ("1h", 3600),  # sb-attachments, as the VPS actually prints it
        ("30min", 1800),
        ("10min", 600),
        ("5min", 300),
        ("1min 30s", 90),  # TimeoutStopUSec on all ten
        ("1800000000", 1800),  # raw microseconds, from systemd builds that emit them
        ("infinity", None),
        ("0", None),
        ("", None),
        ("garbage", None),
    ],
)
def test_systemd_durations_parse(raw, seconds):
    """Would this pass with the behaviour removed? No. Dropping the suffix branch and
    keeping only the integer one returns None for the five forms the VPS actually
    emits, which is every real unit.
    """
    assert llm_deadline._parse_systemd_duration(raw) == seconds


# ------------------------------------------------------------------ systemd querying


def test_an_unknown_unit_is_ignored_even_though_systemctl_exits_zero(monkeypatch):
    """The LoadState gate. `systemctl show` prints manager DEFAULTS for a unit it has
    never heard of and still exits 0 — verified on the VPS, where sb-does-not-exist
    returns TimeoutStartUSec="1min 30s" with LoadState=not-found.

    Would this pass with the behaviour removed? No. Dropping the LoadState check and
    trusting the exit code returns 90 instead of None. That mutation is what turned
    news's CI red: every unit adopted 90s as its timeout, the margin went negative and
    the job refused to run.
    """
    monkeypatch.setattr(
        llm_deadline.subprocess,
        "run",
        _fake_systemctl("LoadState=not-found\nTimeoutStartUSec=1min 30s\n"),
    )
    assert llm_deadline._query_systemd_timeout("sb-does-not-exist") is None


def test_a_loaded_unit_reports_its_timeout(monkeypatch):
    """Would this pass with the behaviour removed? No. A LoadState gate that rejects
    everything — inverting the comparison, say — returns None and the cross-check goes
    permanently blind, which is a silent failure this is the only guard against.
    """
    monkeypatch.setattr(
        llm_deadline.subprocess,
        "run",
        _fake_systemctl("LoadState=loaded\nTimeoutStartUSec=30min\n"),
    )
    assert llm_deadline._query_systemd_timeout("sb-daily-sync") == 1800


def test_a_missing_systemctl_degrades_silently(monkeypatch):
    """Every developer Mac and every CI run takes this path.

    Would this pass with the behaviour removed? No. Letting FileNotFoundError escape,
    rather than catching it, propagates out of _query_systemd_timeout and this call
    raises instead of returning None.
    """
    monkeypatch.setattr(llm_deadline.subprocess, "run", _no_systemctl)
    assert llm_deadline._query_systemd_timeout("sb-daily-sync") is None


def test_a_nonzero_exit_degrades_silently(monkeypatch):
    monkeypatch.setattr(llm_deadline.subprocess, "run", _fake_systemctl("", returncode=1))
    assert llm_deadline._query_systemd_timeout("sb-daily-sync") is None


# ----------------------------------------------------------------------- installation


def test_installing_a_deadline_exports_the_computed_budget(monkeypatch):
    """Would this pass with the behaviour removed? No. Not calling setdefault at all —
    the status quo, where nothing exports the variable — leaves PTS_LLM_DEADLINE unset
    and both assertions fail.
    """
    monkeypatch.setattr(llm_deadline.subprocess, "run", _no_systemctl)
    deadline = llm_deadline.install_llm_deadline("sb-daily-sync", now=1_000_000.0)
    assert deadline == 1_001_590.0
    assert float(os.environ["PTS_LLM_DEADLINE"]) == 1_001_590.0


def test_an_inherited_deadline_wins_and_is_what_gets_returned(monkeypatch):
    """setdefault, and the return value must describe what is INSTALLED, not computed.

    Would this pass with the behaviour removed? No, and two different mutations break
    it separately. Using `os.environ[...] = ...` instead of setdefault overwrites the
    inherited 555 and both assertions fail. Keeping setdefault but returning the
    computed `deadline` passes the env assertion and fails the return assertion — that
    is the subtler bug, a log and a return value describing a budget the run is not
    under.
    """
    monkeypatch.setattr(llm_deadline.subprocess, "run", _no_systemctl)
    os.environ["PTS_LLM_DEADLINE"] = "555.0"
    returned = llm_deadline.install_llm_deadline("sb-daily-sync", now=1_000_000.0)
    assert float(os.environ["PTS_LLM_DEADLINE"]) == 555.0
    assert returned == 555.0


def test_no_unit_means_no_deadline(monkeypatch):
    """A command with no unit gets nothing, never a guess.

    Would this pass with the behaviour removed? No. Falling back to any default unit —
    the obvious "just use the common case" shortcut — installs a deadline and both
    assertions fail.
    """
    monkeypatch.setattr(llm_deadline.subprocess, "run", _no_systemctl)
    assert llm_deadline.install_llm_deadline(None) is None
    assert "PTS_LLM_DEADLINE" not in os.environ


def test_a_unit_outside_the_table_means_no_deadline(monkeypatch):
    monkeypatch.setattr(llm_deadline.subprocess, "run", _no_systemctl)
    assert llm_deadline.install_llm_deadline("sb-invented") is None
    assert "PTS_LLM_DEADLINE" not in os.environ


def test_a_lowered_live_timeout_beats_a_stale_table(monkeypatch, caplog):
    """The dangerous direction: unit cut to 10min, table still says 30min.

    Trusting the table would budget 1590s inside a 600s unit and grant a token-push
    wait the unit cannot survive — SIGKILL with nothing written. The live value must
    win, and the warning must name both numbers so the table can be fixed.

    Would this pass with the behaviour removed? No. Dropping the cross-check, or using
    the table unconditionally, yields 1590 and fails the first assertion.
    """
    monkeypatch.setattr(
        llm_deadline.subprocess,
        "run",
        _fake_systemctl("LoadState=loaded\nTimeoutStartUSec=10min\n"),
    )
    with caplog.at_level(logging.WARNING):
        deadline = llm_deadline.install_llm_deadline("sb-daily-sync", now=1_000_000.0)
    assert deadline == 1_000_390.0
    assert "600" in caplog.text and "1800" in caplog.text


def test_a_raised_live_timeout_still_uses_the_smaller_table_value(monkeypatch):
    """min() of the two, not "live always wins": a table lower than the unit is the
    conservative reading and is kept.

    Would this pass with the behaviour removed? No. Replacing min(table, systemd) with
    the live value alone yields 3390 rather than 390.
    """
    monkeypatch.setattr(
        llm_deadline.subprocess,
        "run",
        _fake_systemctl("LoadState=loaded\nTimeoutStartUSec=1h\n"),
    )
    deadline = llm_deadline.install_llm_deadline("sb-outlook-sync", now=1_000_000.0)
    assert deadline == 1_000_390.0


def test_agreement_between_table_and_systemd_warns_about_nothing(monkeypatch, caplog):
    """Would this pass with the behaviour removed? No. Warning unconditionally, rather
    than only on disagreement, makes every production run log a false drift alert;
    caplog.text would be non-empty.
    """
    monkeypatch.setattr(
        llm_deadline.subprocess,
        "run",
        _fake_systemctl("LoadState=loaded\nTimeoutStartUSec=30min\n"),
    )
    with caplog.at_level(logging.WARNING):
        deadline = llm_deadline.install_llm_deadline("sb-daily-sync", now=1_000_000.0)
    assert deadline == 1_001_590.0
    assert caplog.text == ""


# ------------------------------------------------------------------------- CLI wiring


def test_the_cli_installs_the_deadline_before_running_a_command():
    """src/cli.py must call the hook, and call it BEFORE dispatching.

    Order is load-bearing. ``call_with_policy`` reads PTS_LLM_DEADLINE at its first
    invocation, so a hook that ran after the command would leave the whole run on the
    policy's flat 900s default while looking perfectly wired.

    Would this pass with the behaviour removed? No, and this is the only test that
    catches it. Deleting the ``install_llm_deadline_for_this_process()`` line from
    src/cli.py disconnects the entire feature, and every other test in this file stays
    green because they all call the module directly. Under that mutation ``order`` is
    ["command"] and the assertion fails.
    """
    order = []
    with (
        patch(
            "src.cli.install_llm_deadline_for_this_process",
            side_effect=lambda: order.append("deadline"),
        ),
        patch("src.cli.cmd_stats", side_effect=lambda args: order.append("command")),
        patch.object(sys, "argv", ["brain", "stats"]),
    ):
        cli_main()

    assert order == ["deadline", "command"]


def test_a_starved_unit_stops_the_cli_with_a_nonzero_exit(capsys):
    """A unit that cannot fund one worst-case call refuses to run, loudly.

    Would this pass with the behaviour removed? No. Catching the RuntimeError and
    carrying on, or installing the deadline outside the try where it would surface as
    a bare traceback rather than the CLI's own error path, changes the exit code or
    the stderr text that this asserts. The command must also not run.
    """
    ran = []
    with (
        patch(
            "src.cli.install_llm_deadline_for_this_process",
            side_effect=RuntimeError("unit 'sb-x' cannot fund a single LLM call: ..."),
        ),
        patch("src.cli.cmd_stats", side_effect=lambda args: ran.append("command")),
        patch.object(sys, "argv", ["brain", "stats"]),
    ):
        with pytest.raises(SystemExit) as exc:
            cli_main()

    assert exc.value.code == 1
    assert "cannot fund a single LLM call" in capsys.readouterr().err
    assert ran == []


# ------------------------------------------------------------------------ drift guard


def test_max_call_seconds_matches_every_call_site():
    """The budget is only honest while the reserve's max_call matches what callers pass.

    news reads this per profile from config; here it is one constant, so nothing but
    this test connects it to reality. All five sites pass 120.0: claude_extract's
    extract_one and extract_conversation, attachment_pipeline, image_vision and
    calendar_extractor.

    Would this pass with the behaviour removed? It guards a constant rather than a
    behaviour, so the mutation is elsewhere: change any call site to a different
    max_call_seconds, or change MAX_CALL_SECONDS alone, and the two assertions diverge.
    """
    src = Path(__file__).resolve().parents[1] / "src"
    sites = [
        float(m.group(1))
        for py in src.rglob("*.py")
        for m in re.finditer(
            r"call_with_policy\([^)]*max_call_seconds=([0-9.]+)", py.read_text(), re.S
        )
    ]
    assert len(sites) == 5, f"expected 5 call_with_policy sites, found {len(sites)}"
    assert set(sites) == {120.0}
    assert llm_deadline.MAX_CALL_SECONDS == 120.0
