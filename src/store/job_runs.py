"""Per-run metrics (issue 173): a job_runs row per run and per stage, tokens keyed by run id.

`brain <command>` opens a run around the command (src/cli.py main); a job with stages marks
each one with mark_stage(), which closes the stage before it; complete()'s usage log calls
add_usage() for every Claude call, so the tokens land on the run and on the stage they fell
in, and the log line names the run. One run per process, shared with the worker threads
that extraction starts, so the state is module-level behind a lock rather than a context
variable (which a thread pool does not inherit).

The run row is written 'running' when the run opens, so a run its unit's timeout killed
stays visible, and finished with the stage rows when it ends; one left 'running' by a dead
process is closed 'failed' by the next run on its host (_reap). Every write is its own short
transaction on its own connection, apart from the job's. Nothing here raises into the job:
a store without the table (behind the code, or a test store) or a busy one records nothing.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import sys
import threading
import traceback
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Read-only commands the CLI does not record; a job that only reads records nothing either.
# topic-pages is listed among the readers for its --estimate, but its build is a timer job.
_RECORD_ANYWAY = frozenset({"topic-pages"})
_NEVER = frozenset({"migrate"})

_FIELDS = ("llm_calls", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")

_lock = threading.Lock()
_state: dict[str, Any] | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _zero() -> dict[str, int]:
    return dict.fromkeys(_FIELDS, 0)


def should_record(command: str) -> bool:
    """Whether `brain <command>` is a job whose runs are recorded."""
    from src.cli import READ_ONLY_COMMANDS

    if command in _NEVER:
        return False
    return command not in READ_ONLY_COMMANDS or command in _RECORD_ANYWAY


def current_run_id() -> str | None:
    """The id of the run this process is in, or None outside one."""
    with _lock:
        return _state["run_id"] if _state else None


class Run:
    """The handle record() yields: the command sets exit_code to what it returned."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.exit_code: int | None = None


def _connect(db_path: str) -> sqlite3.Connection | None:
    if not Path(db_path).exists():
        return None
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        has = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'job_runs'"
        ).fetchone()
        if has:
            return conn
        conn.close()
    except sqlite3.Error:
        pass
    return None


def _write(db_path: str, rows: list[dict[str, Any]], update: bool) -> None:
    conn = _connect(db_path)
    if conn is None:
        return
    try:
        with conn:
            for row in rows:
                cols = list(row)
                if update:
                    sets = ", ".join(f"{c} = ?" for c in cols if c not in ("run_id", "stage"))
                    vals = [row[c] for c in cols if c not in ("run_id", "stage")]
                    cur = conn.execute(
                        f"UPDATE job_runs SET {sets} WHERE run_id = ? AND stage = ?",  # noqa: S608
                        [*vals, row["run_id"], row["stage"]],
                    )
                    if cur.rowcount:
                        continue
                conn.execute(
                    f"INSERT INTO job_runs ({', '.join(cols)}) "  # noqa: S608
                    f"VALUES ({', '.join('?' * len(cols))})",
                    [row[c] for c in cols],
                )
    except sqlite3.Error as exc:
        # Never into the job, but never silent either: a run row left 'running' is a lie.
        print(f"job_runs: not recorded ({type(exc).__name__}: {exc})", file=sys.stderr)
    finally:
        conn.close()


def _release_writes(exc: BaseException) -> None:
    """Roll back the write a failed job left open. The traceback keeps the job's frames,
    and so its connection, alive until the process ends: an uncommitted write there holds
    the lock, the run's last row waits out its timeout behind it, and the row read
    'running' for ever. The process is exiting on the error, so the write was lost anyway."""
    for frame, _ in traceback.walk_tb(exc.__traceback__):
        for value in list(frame.f_locals.values()):
            if isinstance(value, sqlite3.Connection):
                try:
                    if value.in_transaction:
                        value.rollback()
                except sqlite3.Error:  # closed, or another thread's
                    pass


def _close_stage(state: dict[str, Any], at: str) -> None:
    stage = state.get("stage")
    if stage is None:
        return
    stage["finished_at"] = at
    state["stages"].append(stage)
    state["stage"] = None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # EPERM: alive, someone else's
        return True
    return True


def _reap(db_path: str, host: str, at: str) -> None:
    """Close this host's 'running' run rows whose process is gone.

    A run whose closing write lost the lock (2026-11-18: "database is locked") or that
    SIGKILL ended leaves its row 'running' for ever, a WARN in every daily health report.
    Only this host's rows, since a pid means nothing on another machine. A reused pid
    keeps its row open: wrong the safe way round."""
    conn = _connect(db_path)
    if conn is None:
        return
    try:
        rows = conn.execute(
            "SELECT run_id, pid FROM job_runs WHERE stage = 'run' AND status = 'running'"
            " AND host = ? AND pid IS NOT NULL",
            (host,),
        ).fetchall()
        dead = [run_id for run_id, pid in rows if not _pid_alive(pid)]
        if dead:
            with conn:
                conn.executemany(
                    "UPDATE job_runs SET status = 'failed', finished_at = ?, error = ?"
                    " WHERE run_id = ? AND stage = 'run' AND status = 'running'",
                    [
                        (at, "process gone, run never closed; closed by a later run", r)
                        for r in dead
                    ],
                )
    except sqlite3.Error as exc:
        print(f"job_runs: orphans not closed ({type(exc).__name__}: {exc})", file=sys.stderr)
    finally:
        conn.close()


@contextmanager
def record(job: str, db_path: str) -> Generator[Run]:
    """Record one run of `job` against the store at db_path."""
    global _state
    run = Run(uuid.uuid4().hex)
    started = _now()
    host = socket.gethostname()
    base = {
        "run_id": run.run_id,
        "job": job,
        "host": host,
        "pid": os.getpid(),
    }
    with _lock:
        _state = {"run_id": run.run_id, "totals": _zero(), "stage": None, "stages": []}
    _reap(db_path, host, started)
    _write(db_path, [{**base, "stage": "run", "started_at": started, "status": "running"}], False)
    status, code, error = "ok", None, None
    try:
        yield run
        code = run.exit_code
        if code not in (None, 0):
            status = "failed"
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else int(exc.code is not None)
        status = "ok" if code == 0 else "failed"
        _release_writes(exc)
        raise
    except BaseException as exc:
        _release_writes(exc)
        # The row replicates and is readable through sql_query: credentials masked, and
        # the text kept short, since an exception can quote what it failed on.
        from src.redact import redact_secrets

        status, code, error = "failed", 1, redact_secrets(f"{type(exc).__name__}: {exc}")[:500]
        raise
    finally:
        finished = _now()
        with _lock:
            state = _state or {"totals": _zero(), "stage": None, "stages": []}
            _close_stage(state, finished)
            _state = None
        rows = [
            {
                **base,
                "stage": "run",
                "started_at": started,
                "finished_at": finished,
                "status": status,
                "exit_code": code,
                "error": error,
                **state["totals"],
            }
        ]
        for stage in state["stages"]:
            rows.append(
                {
                    **base,
                    "stage": stage["name"],
                    "started_at": stage["started_at"],
                    "finished_at": stage["finished_at"],
                    "status": status if stage is state["stages"][-1] else "ok",
                    **stage["counts"],
                }
            )
        _write(db_path, rows, True)


def mark_stage(name: str) -> None:
    """Start stage `name` of the current run, closing the one before; outside a run, nothing."""
    with _lock:
        if _state is None:
            return
        at = _now()
        _close_stage(_state, at)
        _state["stage"] = {"name": name, "started_at": at, "counts": _zero()}


def add_usage(usage: Any) -> None:
    """Add one Claude call's usage to the current run and stage; outside a run, nothing."""

    def tokens(attr: str) -> int:
        value = getattr(usage, attr, None)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    add = {
        "llm_calls": 1,
        "input_tokens": tokens("input_tokens"),
        "output_tokens": tokens("output_tokens"),
        "cache_read_tokens": tokens("cache_read_input_tokens"),
        "cache_write_tokens": tokens("cache_creation_input_tokens"),
    }
    with _lock:
        if _state is None:
            return
        targets = [_state["totals"]]
        if _state["stage"] is not None:
            targets.append(_state["stage"]["counts"])
        for counts in targets:
            for key, value in add.items():
                counts[key] += value
