"""Read data/mail_backend_events.jsonl into the numbers the backend gates are judged on.

`run_outlook_cli` writes one line per shadow comparison (`match`, `mismatch`,
`graph_error`) and, under SB_MAIL_BACKEND=auto, one per call the Graph CLI was asked
to serve (`ok`, or `fallback` with its `reason`), plus `vanished` lines (backend
`adapter`) for messages Exchange no longer had when the Graph CLI read them. This counts them per backend, verb
and outcome, and gives the two figures the plan's gates name: the shadow's
non-matches and auto's fallback rate. Reads only; a line it cannot parse is counted,
never fatal.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _parse_at(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def summarise(path: Path, since: datetime | None = None) -> dict:
    counts: Counter[tuple[str, str, str]] = Counter()
    reasons: Counter[str] = Counter()
    unreadable = 0
    vanished_events: list[dict] = []
    first = last = None
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                unreadable += 1
                continue
            at = _parse_at(e.get("at")) if isinstance(e, dict) else None
            if at is None:
                unreadable += 1
                continue
            if since and at < since:
                continue
            first = at if first is None or at < first else first
            last = at if last is None or at > last else last
            counts[(str(e.get("backend")), str(e.get("verb")), str(e.get("outcome")))] += 1
            if e.get("outcome") == "vanished":
                vanished_events.append(e)
            if e.get("outcome") == "fallback":
                reasons[str(e.get("reason") or "unknown")] += 1

    def total(backend: str, outcome: str | None = None) -> int:
        return sum(n for (b, _, o), n in counts.items() if b == backend and outcome in (None, o))

    shadow = total("shadow")
    auto = total("auto", "ok") + total("auto", "fallback")
    fallbacks = total("auto", "fallback")
    return {
        "path": str(path),
        "since": since.isoformat() if since else None,
        "first": first.isoformat() if first else None,
        "last": last.isoformat() if last else None,
        "unreadable_lines": unreadable,
        "counts": [
            {"backend": b, "verb": v, "outcome": o, "n": n}
            for (b, v, o), n in sorted(counts.items())
        ],
        "vanished": sum(int(n.get("count") or 1) for n in vanished_events),
        "shadow": {"events": shadow, "non_match": shadow - total("shadow", "match")},
        "auto": {
            "calls": auto,
            "fallbacks": fallbacks,
            "fallback_rate": round(fallbacks / auto, 4) if auto else None,
            "reasons": dict(sorted(reasons.items())),
        },
    }


def render(summary: dict) -> str:
    lines = [
        f"{summary['path']}",
        f"window: {summary['first'] or '-'} .. {summary['last'] or '-'}"
        + (f" (since {summary['since']})" if summary["since"] else ""),
        "",
        f"{'backend':<8} {'verb':<22} {'outcome':<12} {'n':>6}",
    ]
    for c in summary["counts"]:
        lines.append(f"{c['backend']:<8} {c['verb']:<22} {c['outcome']:<12} {c['n']:>6}")
    s, a = summary["shadow"], summary["auto"]
    lines.append("")
    lines.append(f"shadow: {s['events']} compared, {s['non_match']} not matching")
    rate = "n/a" if a["fallback_rate"] is None else f"{a['fallback_rate'] * 100:.2f}%"
    why = ", ".join(f"{k} {v}" for k, v in a["reasons"].items()) or "none"
    lines.append(f"auto:   {a['calls']} calls, {a['fallbacks']} fallbacks ({rate}); {why}")
    lines.append(
        f"vanished: {summary['vanished']} messages gone between a Graph listing and its read"
        " (not counted as mismatches or fallbacks)"
    )
    if summary["unreadable_lines"]:
        lines.append(f"unreadable lines: {summary['unreadable_lines']}")
    return "\n".join(lines)
