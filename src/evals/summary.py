"""The latest eval results, read back for the health check.

scripts/extraction_eval.py appends one record per run to evals/extraction/results.jsonl,
and scripts/retrieval_eval.py one per run to evals/retrieval/results.jsonl. This reads
the newest of each kind and compares it with the newest earlier run that measured the
same thing: the same set file and, for extraction, the same cases. A pilot on three
cases, or a run after the owner edited the set, is never compared, and does not hide
the run before it.

A fall of more than `drop_points` percentage points in the headline (extraction pass
rate, qa recall@10 per mode, known-item thread hit@10) is a warning, and so is any qa
recall or semantic call whose semantic half was not 'ok': the 2026-10-06 embedding
outage moved no number far enough for anyone to look.
"""

from datetime import UTC, datetime
from pathlib import Path

from src.evals import DROP_POINTS, eval_home, read_jsonl


def _records(path: Path, name: str) -> list[dict]:
    try:
        rows = read_jsonl(path, strict=False)
    except OSError:
        return []
    return [r for r in rows if r.get("eval") == name]


def _latest_and_previous(records: list[dict], same: tuple[str, ...]):
    """The newest record, and the newest earlier one that agrees with it on `same`."""
    if not records:
        return None, None
    latest = records[-1]
    key = [latest.get(field) for field in same]
    previous = next(
        (r for r in reversed(records[:-1]) if [r.get(field) for field in same] == key), None
    )
    return latest, previous


def _number(value) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _change(latest, previous) -> float | None:
    """latest - previous, in percentage points; None unless both are numbers."""
    a, b = _number(latest), _number(previous)
    return None if a is None or b is None else round((a - b) * 100, 2)


def _age_days(record: dict, now: datetime) -> float | None:
    try:
        ts = datetime.fromisoformat(str(record.get("ts")))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return round((now - ts).total_seconds() / 86400, 2)


def _fell(what: str, change: float | None, latest, previous, drop_points: float) -> list[str]:
    if change is None or -change <= drop_points:
        return []
    return [
        f"{what} fell {-change:.1f} points ({previous['value']:.1%} to {latest['value']:.1%}) "
        f"between {previous['date']} and {latest['date']}"
    ]


def _extraction(path: Path, drop_points: float, now: datetime) -> tuple[dict, list[str]]:
    latest, previous = _latest_and_previous(_records(path, "extraction"), ("set_sha", "selection"))
    out: dict = {"latest": None, "previous": None, "change_points": None}
    if latest is None:
        return out, []
    keep = (
        "date",
        "run_id",
        "model",
        "call_shape",
        "prompt_sha",
        "effort",
        "pass_rate",
        "cost_usd",
    )
    out["latest"] = {k: latest.get(k) for k in keep}
    out["latest"]["scored"] = (latest.get("rows") or {}).get("scored")
    out["latest"]["age_days"] = _age_days(latest, now)
    if previous is None:
        return out, []
    out["previous"] = {k: previous.get(k) for k in keep}
    change = _change(latest.get("pass_rate"), previous.get("pass_rate"))
    out["change_points"] = change
    warnings = _fell(
        "extraction pass rate",
        change,
        {"value": latest.get("pass_rate"), "date": latest.get("date")},
        {"value": previous.get("pass_rate"), "date": previous.get("date")},
        drop_points,
    )
    return out, warnings


def _recall10(record: dict | None, mode: str):
    return (((record or {}).get("modes") or {}).get(mode) or {}).get("recall@10")


def _qa(records: list[dict], drop_points: float, now: datetime) -> tuple[dict, list[str]]:
    latest, previous = _latest_and_previous(records, ("set_sha",))
    out: dict = {"latest": None, "previous": None, "change_points": {}}
    if latest is None:
        return out, []
    modes = list((latest.get("modes") or {}).keys())
    statuses = [
        (status, n)
        for group in (latest.get("semantic_status") or {}).values()
        for status, n in (group or {}).items()
        if isinstance(n, int)
    ]
    not_ok = sum(n for status, n in statuses if status != "ok")
    out["latest"] = {
        "date": latest.get("date"),
        "age_days": _age_days(latest, now),
        "recall@10": {mode: _recall10(latest, mode) for mode in modes},
        "semantic_not_ok": not_ok,
    }
    warnings = []
    if previous is not None:
        out["previous"] = {
            "date": previous.get("date"),
            "recall@10": {mode: _recall10(previous, mode) for mode in modes},
        }
        for mode in modes:
            change = _change(_recall10(latest, mode), _recall10(previous, mode))
            out["change_points"][mode] = change
            warnings += _fell(
                f"qa recall@10 ({mode})",
                change,
                {"value": _recall10(latest, mode), "date": latest.get("date")},
                {"value": _recall10(previous, mode), "date": previous.get("date")},
                drop_points,
            )
    if not_ok:
        worst = sorted((s for s in statuses if s[0] != "ok"), key=lambda s: -s[1])[0][0]
        total = sum(n for _, n in statuses)
        warnings.insert(
            0,
            f"qa run of {latest.get('date')}: semantic search was not ok in {not_ok} of "
            f"{total} calls (mostly {worst})",
        )
    return out, warnings


def _known_item(records: list[dict], drop_points: float, now: datetime) -> tuple[dict, list[str]]:
    latest, previous = _latest_and_previous(records, ("set_sha",))
    out: dict = {"latest": None, "previous": None, "change_points": None}
    if latest is None:
        return out, []

    def hit10(record):
        return (record.get("thread") or {}).get("hit@10")

    out["latest"] = {
        "date": latest.get("date"),
        "age_days": _age_days(latest, now),
        "thread_hit@10": hit10(latest),
    }
    if previous is None:
        return out, []
    out["previous"] = {"date": previous.get("date"), "thread_hit@10": hit10(previous)}
    change = _change(hit10(latest), hit10(previous))
    out["change_points"] = change
    warnings = _fell(
        "known-item thread hit@10",
        change,
        {"value": hit10(latest), "date": latest.get("date")},
        {"value": hit10(previous), "date": previous.get("date")},
        drop_points,
    )
    return out, warnings


def latest_eval_summary(
    data_dir: Path | str | None = None,
    *,
    drop_points: float = DROP_POINTS,
    now: datetime | None = None,
) -> dict:
    """The newest extraction, qa and known-item results, each against the run before it.

    `data_dir` is the directory that holds `evals/` (default ${BRAIN_DATA_DIR:-~/.second-brain}).
    Returns `status` ('WARN' when there are warnings, 'OK' when anything was recorded,
    'NONE' when nothing was), `warnings` (one sentence each), and `extraction`, `qa` and
    `known_item`, each with `latest`, `previous` and `change_points` (latest minus
    previous, in percentage points; per mode for qa). Never raises on a missing or
    torn results file.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    home = eval_home(data_dir)
    extraction, warnings = _extraction(home / "extraction" / "results.jsonl", drop_points, now)
    retrieval = home / "retrieval" / "results.jsonl"
    qa, qa_warnings = _qa(_records(retrieval, "qa"), drop_points, now)
    known, known_warnings = _known_item(_records(retrieval, "known-item"), drop_points, now)
    warnings += qa_warnings + known_warnings
    recorded = any(part["latest"] for part in (extraction, qa, known))
    return {
        "status": "WARN" if warnings else ("OK" if recorded else "NONE"),
        "warnings": warnings,
        "extraction": extraction,
        "qa": qa,
        "known_item": known,
    }
