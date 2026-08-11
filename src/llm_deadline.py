"""Compute and export PTS_LLM_DEADLINE so the shared policy's budget test is real.

Nothing in the estate exports this variable today, so ``resolve_deadline()`` falls back
to a flat ``now + DEFAULT_BUDGET_SECONDS`` (900) for every run, regardless of which unit
is executing. That is not cosmetic in either direction:

  * Too small for the long units. On Linux ``decide()`` grants a token-push wait only
    when the remaining budget can fund ``PUSH_WAIT_SECONDS + max_call`` (1020 + 120 =
    1140). Against a flat 900 that test can never pass, so the six units with 1800s or
    3600s of real headroom, the ones the wait exists for, never get it.
  * Too large for the short units. sb-calendar-sync and sb-conversation-sync have a real
    TimeoutStartSec of 300. A 900s budget lets the policy plan retries the unit will be
    SIGTERMed in the middle of.

FOUR SEPARATE QUESTIONS, answered by four separate mechanisms. They are easy to
conflate on a later read, and conflating them is how a budget quietly becomes wrong:

  1. WHICH unit is this process?      ``_detect_systemd_unit`` reads /proc/self/cgroup.
     Exact membership in the table below, never a prefix or a glob. No unit, no
     deadline; a guessed unit is worse than none.
  2. WHAT SHOULD its timeout be?      ``_UNIT_TIMEOUT_SECONDS``, the checked-in table.
     This is the expected value, and the thing a reviewer can read.
  3. WHAT IS its timeout right now?   ``_query_systemd_unit`` asks systemd, gated on
     LoadState. The SMALLER of (2) and (3) wins, with a warning naming both, because
     the dangerous drift is a unit whose timeout was cut while the table was not.
  4. WHEN DID THE UNIT START?         ``ExecMainStartTimestamp``, from the same query,
     via ``_deadline_anchor``. The budget is the UNIT's, so the deadline has to count
     from the UNIT's start, not from whichever of its processes happens to be asking.
     Six of the ten units run more than one ``src.cli`` process per invocation and
     nothing carries the deadline between them (``grep -rl PTS_LLM_DEADLINE
     ~/.local/bin ~/scripts`` on the VPS returns nothing), so a per-process anchor
     hands each sibling a fresh full budget measured from its own start.

     Concretely, before this: sb-attachment-pass.sh runs process-attachments, then
     process-images, then process-sharepoint inside one 3600s unit. Phase two starts at
     t=1800 and anchors its 3390s budget there, so an auth error at t=2700 satisfies
     2700 + 1020 + 120 <= 5190, the policy grants a token-push wait, and systemd
     SIGKILLs 900 seconds into it. The flat 900s default this module replaces could
     never fund a wait at all, so the branch that added the wait is the branch that
     could kill a unit with it.

Ported from news's ``main.py`` (``_deadline_reserve_seconds``, ``_llm_budget_seconds``,
``_query_systemd_timeout``, ``install_llm_deadline``), which this mirrors deliberately
rather than reinventing. ``src/llm_policy.py`` is a vendored copy under a SHA drift
guard and is never edited; this module sits beside it and only reads from it.
"""

import logging
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from src.llm_policy import MAX_ATTEMPTS, ROW_CAPS, backoff

# Effective TimeoutStartSec of each scheduled second-brain unit, read live from the VPS
# on 2026-08-11 with `systemctl --user show sb-<name>.service -p TimeoutStartUSec`.
# These ten are the whole scheduled set. A unit missing from this map gets no deadline
# rather than a guessed one.
_UNIT_TIMEOUT_SECONDS: dict[str, int] = {
    "sb-attachments": 3600,
    "sb-calendar-sync": 300,
    "sb-conversation-sync": 300,
    "sb-curate-docs": 1800,
    "sb-daily-sync": 1800,
    "sb-news-sync": 1800,
    "sb-noon-catchup": 1800,
    "sb-outlook-sync": 600,
    "sb-reverse-ingest": 1800,
    "sb-teams-sync": 600,
}

# TimeoutStopSec on all ten units, which is also the user manager's
# DefaultTimeoutStopUSec. Held back from the budget so a terminal GIVE_UP still has room
# to finish its bookkeeping and flush its logs before systemd's SIGTERM.
_SHUTDOWN_GRACE_SECONDS = 90

# Every ``call_with_policy`` call site in this repo passes 120.0: claude_extract's
# extract_one and extract_conversation, attachment_pipeline, image_vision and
# calendar_extractor. news reads the equivalent per profile from config; here it is
# uniform, and test_max_call_seconds_matches_every_call_site fails if a site diverges.
MAX_CALL_SECONDS = 120.0

# Largest single backoff decide() can actually emit: RATE_LIMIT at n=3, so 240s.
# Derived, not written down, so it tracks ROW_CAPS and MAX_ATTEMPTS. n >= 4 is
# unreachable because decide() tests `attempt.total >= MAX_ATTEMPTS` before it consults
# the row, and total >= n always holds, which is why backoff()'s own 600s ceiling never
# applies. Reported in the startup log; deliberately NOT part of the reserve below.
_MAX_REACHABLE_BACKOFF_SECONDS = max(
    backoff(n, outcome) for outcome in ROW_CAPS for n in range(1, MAX_ATTEMPTS)
)


_CGROUP_PATH = Path("/proc/self/cgroup")
_SERVICE_SUFFIX = ".service"


def _detect_systemd_unit(cgroup_path: Path = _CGROUP_PATH) -> str | None:
    """The systemd unit this process is running under, or None when there isn't one.

    HOW THE PROCESS KNOWS WHICH UNIT IT IS, and why the obvious answer does not work.
    news had a ``--profile`` argument; here the units run shell wrappers that invoke a
    Python entrypoint, and unit-file changes were withdrawn from the spec, so the
    candidate was the CLI subcommand. It cannot answer the question. THREE units invoke
    ``src.cli sync``, and two of them pass byte-identical argv:

        sb-daily-sync   (1800)  sync --engine claude --workers 8
        sb-noon-catchup (1800)  sync --engine claude --workers 8 --skip-export
        sb-outlook-sync ( 600)  sync --engine claude --workers 8 --skip-export

    So argv maps ``sync`` to both 1800 and 600 with nothing to choose between them.
    Taking the larger over-budgets sb-outlook-sync by 1200s, which is the SIGKILL-with-
    nothing-written case this whole mechanism exists to prevent. Taking the smaller
    silently denies the token-push wait to the two 30-minute units that can most afford
    it. Neither reading is right and argv offers no third. Two units do not run
    ``src.cli`` at all, which a subcommand table could not reach in any case.

    The cgroup answers it exactly. Under a systemd user manager the process's cgroup
    path ends in its own unit. Verified on the VPS on 2026-08-11, cgroup v2:

        0::/user.slice/user-1000.slice/user@1000.service/app.slice/sb-daily-sync.service

    It survives the two layers of bash wrapper between the unit and Python, because
    children inherit their parent's cgroup — also verified, through a nested
    bash -> bash -> child. It needs no unit-file change, no new CLI argument and no
    wrapper edit.

    Returns None wherever the answer is not certain: no ``/proc`` (every developer Mac),
    or no segment matching a known unit (CI, and a hand-run over SSH, which sits in a
    ``session-N.scope``). Membership in ``_UNIT_TIMEOUT_SECONDS`` is what rejects that
    noise, and it is also what rejects the ``user@1000.service`` slice that every path
    above contains.
    """
    try:
        raw = cgroup_path.read_text()
    except OSError:
        return None
    # cgroup v2 writes a single "0::<path>" line; v1 writes several
    # "N:controller:<path>" lines. Scanning every segment of every line, innermost
    # first, covers both without branching on the version.
    for line in raw.splitlines():
        for segment in reversed(line.split("/")):
            if segment.endswith(_SERVICE_SUFFIX):
                unit = segment[: -len(_SERVICE_SUFFIX)]
                # EXACT table key. Not a prefix, not an `sb-*` glob. A future
                # sb-something-new.service has no entry, so its real TimeoutStartSec is
                # unknown and it must get no deadline rather than the wrong one — the
                # same "no unit, no guess" rule as everywhere else here. This is also
                # what rejects the enclosing user@1000.service slice.
                if unit in _UNIT_TIMEOUT_SECONDS:
                    return unit
    return None


def _parse_systemd_duration(s: str) -> int | None:
    """Parse a systemd duration to seconds. Returns None if unrecognised.

    Handles the two forms TimeoutStartUSec can return:
    - Raw microsecond integer: "1800000000" -> 1800
    - Human-readable suffixes from some systemd builds: "30min", "10min", "5min", "1h",
      "1min 30s". Infinity and zero are treated as "no timeout" (None).

    The VPS returns the SUFFIXED form for all ten sb units, so that is the branch that
    actually runs in production; the integer branch is kept because other systemd builds
    emit it and the cost of covering both is one regex.
    """
    s = s.strip()
    if not s or s in ("infinity", "0"):
        return None
    # Raw microseconds: all digits, no spaces or letters
    if re.fullmatch(r"\d+", s):
        us = int(s)
        return us // 1_000_000 if us >= 1_000_000 else None
    # Duration with suffix(es): accumulate seconds
    total = 0
    _UNIT_RE = re.compile(r"(\d+)\s*(h|min|s)\b", re.IGNORECASE)
    for m in _UNIT_RE.finditer(s):
        n, unit = int(m.group(1)), m.group(2).lower()
        if unit == "h":
            total += n * 3600
        elif unit == "min":
            total += n * 60
        else:
            total += n
    return total if total > 0 else None


# "Tue 2026-08-11 02:00:06 EEST", the form the VPS emits. The leading weekday and the
# trailing zone abbreviation are both optional and both DISCARDED — see
# _parse_systemd_timestamp for why reading either would be a mistake.
_TIMESTAMP_RE = re.compile(
    r"^(?:\S+\s+)?(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:\s+\S+)?$"
)


def _parse_systemd_timestamp(s: str) -> float | None:
    """Parse a systemd timestamp to POSIX seconds. None if absent or unrecognised.

    LOCAL TIME, AND THAT IS NOT AN ASSUMPTION. systemctl is a client that renders the
    timestamp itself, with the environment it inherits from THIS process, so its notion of
    local time and Python's are the same by construction. The zone abbreviation is
    therefore redundant, and parsing it would be worse than ignoring it: strptime's %Z
    understands UTC, GMT and the running machine's own zone names and nothing else, so
    "EEST" read on a UTC CI box fails outright. The weekday is dropped for the same reason
    — it is locale-dependent output being fed to a locale-dependent parser.

    The one hour a year that is genuinely ambiguous, the repeated hour at a DST fallback,
    resolves through fold=0 to the earlier of the two instants. That makes the anchor
    earlier, the budget shorter and the deadline sooner, which is the safe direction.

    The "@1786402806" form is accepted because `systemctl --timestamp=unix` emits it.
    Nothing here passes that flag (see _query_systemd_unit) but a future caller might.
    """
    s = s.strip()
    # systemd prints "n/a" for a unit that has never run; some builds print a bare 0.
    if not s or s in ("n/a", "0"):
        return None
    if s.startswith("@"):
        try:
            return float(s[1:])
        except ValueError:
            return None
    m = _TIMESTAMP_RE.match(s)
    if m is None:
        return None
    try:
        naive = datetime(*(int(g) for g in m.groups()))  # type: ignore[arg-type]
    except ValueError:  # e.g. month 13, day 32
        return None
    return naive.astimezone().timestamp()


class _UnitFacts(NamedTuple):
    """What one `systemctl show` says about a unit. Every field degrades to None."""

    timeout_seconds: int | None
    start_time: float | None


_NO_UNIT_FACTS = _UnitFacts(None, None)


def _query_systemd_unit(unit: str) -> _UnitFacts:
    """Ask systemd for a unit's start timeout and its start time. None on any failure.

    ONE CALL FOR BOTH FACTS, deliberately: they are read together, they must describe the
    same activation, and a second fork is a second chance to disagree.

    Degrades silently on macOS, in CI, and anywhere systemctl is absent or the unit is
    unknown. A cross-check that raises is worse than the table drift it guards against.

    LoadState is queried in the same call and gates the answer, because `systemctl show`
    does NOT fail on an unknown unit: it exits 0 and prints the manager's DEFAULTS.
    Verified on the VPS on 2026-08-11 — `sb-does-not-exist` returns TimeoutStartUSec=
    "1min 30s" with LoadState=not-found and rc=0, while every real sb unit returns its
    true value with LoadState=loaded. Trusting the exit code alone would make every
    developer Mac and every CI run adopt 90s as its unit timeout, drive the margin
    negative and refuse to run. Only a loaded unit's timeout means anything.

    NO `--timestamp=unix`, though it would remove the parsing entirely. It is a global
    systemctl option, so on a build that predates it systemctl exits non-zero and takes
    the TimeoutStartUSec cross-check down with it — trading a parser for a silent loss of
    the drift guard. Verified present on the VPS (systemd 259) and still not used.

    ExecMainStartTimestamp is the start of the unit's main process. All ten sb units are
    Type=oneshot with exactly one ExecStart and no ExecStartPre (verified on the VPS on
    2026-08-11), so it coincides with InactiveExitTimestamp, i.e. with the instant
    TimeoutStartSec begins counting. A unit that later grows an ExecStartPre, or a second
    ExecStart line, would break that identity and should switch to InactiveExitTimestamp.
    """
    try:
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                f"{unit}.service",
                "-p",
                "LoadState",
                "-p",
                "TimeoutStartUSec",
                "-p",
                "ExecMainStartTimestamp",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return _NO_UNIT_FACTS
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if fields.get("LoadState") != "loaded":
            return _NO_UNIT_FACTS
        return _UnitFacts(
            _parse_systemd_duration(fields.get("TimeoutStartUSec", "")),
            _parse_systemd_timestamp(fields.get("ExecMainStartTimestamp", "")),
        )
    except Exception:  # noqa: BLE001 - FileNotFoundError, TimeoutExpired, etc.
        return _NO_UNIT_FACTS


def _deadline_reserve_seconds(max_call_seconds: float) -> float:
    """Seconds held back from a unit's TimeoutStartSec. 210s on today's config.

    210 = max_call 120 + shutdown_grace 90. It is a function rather than a literal
    because max_call belongs to the call site: pinning 210 would silently go wrong the
    moment one call site's max_call_seconds differed from the rest.

    WHY ``max_backoff`` IS ABSENT, and why restoring it would be a regression rather than
    a correction. The spec writes the reserve as ``max_call_seconds + max_backoff +
    shutdown_grace``. That term is double-counted. ``decide()``'s budget test is
    FORWARD-LOOKING — ``now + sleep_s + max_call_seconds > deadline`` in llm_policy — so
    it already refuses any backoff whose sleep plus the following call would not fit, and
    no backoff can push the loop past the deadline. Subtracting the maximum again charges
    for it twice. Literally, the reserve would become 450s and sb-calendar-sync and
    sb-conversation-sync, at 300s, would go MINUS 150s, so the startup check in
    ``_llm_budget_seconds`` would refuse to run two of the ten production units.

    ``max_call_seconds`` is NOT double-counted and must stay. ``call_with_policy`` calls
    ``fn()`` unconditionally at the top of its loop with no deadline test before the
    FIRST call, so if earlier work has eaten the budget that call still starts and can
    run its whole timeout past the deadline. This term is the only thing covering that.
    """
    return max_call_seconds + _SHUTDOWN_GRACE_SECONDS


def _llm_budget_seconds(
    unit: str,
    max_call_seconds: float,
    unit_timeout_seconds: float | None = None,
) -> float | None:
    """Seconds of LLM budget this unit can fund. None when the unit is unknown.

    The reserve, and the reasoning behind which terms it does and does not contain,
    lives in ``_deadline_reserve_seconds``.

    Raises RuntimeError when the margin is not positive. A unit that cannot fund one
    worst-case call plus its shutdown grace cannot produce output at all, and an
    immediate loud failure beats a SIGTERM later with nothing to show for it. On today's
    numbers the threshold sits at 211s and the smallest unit is 300s, so the check fires
    for nobody; it exists to catch a future TimeoutStartSec cut before it ships.
    """
    if unit_timeout_seconds is None:
        unit_timeout_seconds = _UNIT_TIMEOUT_SECONDS.get(unit)
    if unit_timeout_seconds is None:
        return None

    budget = unit_timeout_seconds - _deadline_reserve_seconds(max_call_seconds)
    if budget <= 0:
        raise RuntimeError(
            f"unit '{unit}' cannot fund a single LLM call: TimeoutStartSec="
            f"{unit_timeout_seconds:g}s minus max_call={max_call_seconds:g}s minus "
            f"shutdown_grace={_SHUTDOWN_GRACE_SECONDS}s leaves {budget:g}s. Raise the "
            f"unit's TimeoutStartSec above "
            f"{_deadline_reserve_seconds(max_call_seconds):g}s, or lower the call site's "
            f"max_call_seconds. Refusing to run."
        )
    return budget


def _deadline_anchor(
    unit: str,
    unit_start: float | None,
    now: float,
    unit_timeout_seconds: float | None,
    logger: logging.Logger,
) -> float:
    """The instant the budget counts from: the unit's start, or this process's if unusable.

    A WRONG ANCHOR IS WORSE THAN A PER-PROCESS ONE, which is why all three rejections
    below fall back to ``now`` rather than to a repaired value. Anchoring too EARLY only
    shortens the budget; anchoring too LATE is the SIGKILL this module exists to prevent,
    and there is no way to tell one bad timestamp from the other.

      * Absent or unparseable. The host does not offer the fact, or offers it in a shape
        this build does not know. Logged, because the fallback silently restores exactly
        the per-process behaviour that the anchoring exists to remove.
      * In the future. Clock skew, or a timestamp from a different clock entirely. It
        would push the deadline out by the whole error.
      * Older than the unit's own timeout. Impossible for a live activation: systemd would
        have killed the unit already, so a process still running and reading this must be
        looking at a previous activation's timestamp — a lingering child, say. Without
        this the budget goes negative and the run makes no calls at all while looking
        perfectly healthy. Note the boundary is >=, and a process that starts LEGITIMATELY
        late is unaffected: it is by definition still inside the timeout, so it correctly
        gets the small remainder rather than a fresh full budget.
    """
    if unit_start is None:
        logger.warning(
            "unit '%s': no usable ExecMainStartTimestamp; anchoring the deadline at this "
            "process's own start, so a sibling process in the same unit will get its own "
            "full budget.",
            unit,
        )
        return now
    if unit_start > now:
        logger.warning(
            "unit '%s': ExecMainStartTimestamp %g is in the future (now %g); anchoring at "
            "this process's own start instead.",
            unit,
            unit_start,
            now,
        )
        return now
    if unit_timeout_seconds is not None and now - unit_start >= unit_timeout_seconds:
        logger.warning(
            "unit '%s': ExecMainStartTimestamp %g is %gs old, at or past the unit's own "
            "TimeoutStartSec of %gs, so it cannot be this activation; anchoring at this "
            "process's own start instead.",
            unit,
            unit_start,
            now - unit_start,
            unit_timeout_seconds,
        )
        return now
    return unit_start


def install_llm_deadline(
    unit: str | None,
    now: float | None = None,
    max_call_seconds: float = MAX_CALL_SECONDS,
) -> float | None:
    """Export PTS_LLM_DEADLINE for this run. Returns the deadline in effect, or None.

    ``unit`` is the systemd unit this process is running under, or None when it is not
    running under one at all — a hand-run, a test, an MCP server. None means NO deadline
    is installed and the policy keeps its own default, which is the correct answer: a
    guessed unit is worse than no unit, because the budget it produces is asserted with
    the same confidence as a real one.

    ``setdefault``, not assignment: an operator running a pass by hand, or a future
    runner that computes a better value, must still win.

    ``now`` is THIS PROCESS's clock reading, not the origin of the budget. The budget
    counts from the UNIT's start (see ``_deadline_anchor``), so that every process the
    unit runs converges on the same absolute deadline. ``now`` is what the anchor is
    sanity-checked against, and what it falls back to.

    Wall clock, not monotonic, throughout. PTS_LLM_DEADLINE is an absolute POSIX time and
    ``resolve_deadline`` compares it against ``time.time()``; a monotonic value here
    would switch the whole mechanism off silently. ExecMainStartTimestamp is on that same
    epoch, which is why it can serve as the anchor at all — systemd's separate
    ``...TimestampMonotonic`` field cannot, and must not be substituted for it.
    """
    logger = logging.getLogger(__name__)

    if unit is None:
        logger.info(
            "no systemd unit for this process; leaving PTS_LLM_DEADLINE unset "
            "(the policy applies its own default budget)"
        )
        return None

    # Cross-check the table against the live systemd value. The dangerous direction is
    # LOWERED: if a unit's TimeoutStartSec was reduced and the table was not, the code
    # over-budgets and can grant a token-push wait the unit cannot survive, ending in
    # SIGKILL with nothing written. Querying at startup catches that silently and safely.
    table_timeout = _UNIT_TIMEOUT_SECONDS.get(unit)
    effective_timeout: float | None = table_timeout
    unit_start: float | None = None
    if table_timeout is not None:
        facts = _query_systemd_unit(unit)
        unit_start = facts.start_time
        if facts.timeout_seconds is not None and facts.timeout_seconds != table_timeout:
            effective_timeout = min(table_timeout, facts.timeout_seconds)
            logger.warning(
                "unit '%s': live TimeoutStartSec=%gs disagrees with table value=%gs; "
                "using the smaller (%gs). Update _UNIT_TIMEOUT_SECONDS to silence this.",
                unit,
                facts.timeout_seconds,
                table_timeout,
                effective_timeout,
            )

    budget = _llm_budget_seconds(unit, max_call_seconds, unit_timeout_seconds=effective_timeout)
    if budget is None:
        logger.info(
            "unit '%s' is not in the timeout table; leaving PTS_LLM_DEADLINE unset "
            "(the policy applies its own default budget)",
            unit,
        )
        return None

    now = time.time() if now is None else now
    anchor = _deadline_anchor(unit, unit_start, now, effective_timeout, logger)
    deadline = anchor + budget
    os.environ.setdefault("PTS_LLM_DEADLINE", repr(deadline))
    installed = float(os.environ["PTS_LLM_DEADLINE"])
    if installed != deadline:
        # setdefault kept an inherited value. Report what is actually in effect:
        # announcing the computed budget here would describe a policy the run is not
        # running under.
        logger.info(
            "PTS_LLM_DEADLINE was already set to %g and is kept; the computed budget "
            "for '%s' would have been %gs.",
            installed,
            unit,
            budget,
        )
        return installed
    logger.info(
        "LLM budget for '%s': %gs (TimeoutStartSec=%gs - max_call=%gs - grace=%gs), "
        "anchored at the unit's start, %gs ago, leaving %gs for this process. "
        "Largest backoff the policy can emit is %gs.",
        unit,
        budget,
        effective_timeout,
        max_call_seconds,
        _SHUTDOWN_GRACE_SECONDS,
        now - anchor,
        deadline - now,
        _MAX_REACHABLE_BACKOFF_SECONDS,
    )
    return deadline


def install_llm_deadline_for_this_process() -> float | None:
    """Entrypoint hook: work out this process's unit and install its deadline.

    The one line an entrypoint calls. Detection is deliberately behind this seam, so
    changing how a process identifies its unit touches ``_detect_systemd_unit`` and
    nothing else — every function below it takes a unit NAME.

    THE LOGGING HANDLER BELOW IS A DELIBERATE LOCAL CHOICE, not an oversight of failing
    to configure logging globally. ``src/cli.py`` configures none, so without a handler
    the budget INFO line — the only record of which budget a run is actually under — is
    swallowed by the root logger, while the drift WARNING still reaches stderr through
    logging's last-resort handler. That asymmetry is the worst of the options: the loud
    case stays loud and the quiet case disappears, so "log what was actually installed"
    silently stops being true in production while looking fine in tests.

    ``logging.basicConfig`` in ``main()`` is formally the entrypoint's job and was
    considered and rejected: it would switch INFO on for every module across all ten VPS
    units at once, a far larger behavioural change than this task should carry. Scoping
    one handler to one logger is the smaller blast radius.

    IDEMPOTENT. The ``logger.handlers`` half of the guard is what makes repeated calls in
    one process safe: without it, a second call would stack a second StreamHandler and
    every subsequent line would be emitted twice. The root-handlers half defers to any
    real logging configuration, so when one exists this adds nothing and the record
    propagates normally.

    The unit wrappers redirect stderr into the per-unit log file, so that is where it
    lands.
    """
    logger = logging.getLogger(__name__)
    if not logging.getLogger().handlers and not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return install_llm_deadline(_detect_systemd_unit())
