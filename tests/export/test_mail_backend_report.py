"""brain mail-backend-report reads the gate figures off the events file in one command."""

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from src.export.mail_backend_report import render, summarise

ROOT = Path(__file__).resolve().parents[2]


def _write(path, events):
    path.write_text("".join(json.dumps(e) + "\n" for e in events) + "not json\n")


EVENTS = [
    {
        "at": "2026-11-07T00:13:00+00:00",
        "backend": "shadow",
        "verb": "list-mail",
        "outcome": "match",
    },
    {
        "at": "2026-11-09T10:13:12+00:00",
        "backend": "shadow",
        "verb": "list-mail",
        "outcome": "graph_error",
    },
    {"at": "2026-11-13T01:00:00+00:00", "backend": "auto", "verb": "get-mail", "outcome": "ok"},
    {"at": "2026-11-13T01:00:01+00:00", "backend": "auto", "verb": "get-mail", "outcome": "ok"},
    {"at": "2026-11-13T01:00:02+00:00", "backend": "auto", "verb": "get-mail", "outcome": "ok"},
    {
        "at": "2026-11-13T01:30:00+00:00",
        "backend": "adapter",
        "verb": "list-mail",
        "outcome": "vanished",
        "mode": "auto",
        "count": 2,
        "ids": ["a", "b"],
    },
    {
        "at": "2026-11-13T02:00:00+00:00",
        "backend": "auto",
        "verb": "list-mail",
        "outcome": "fallback",
        "reason": "auth",
        "exit_code": 2,
    },
]


def test_counts_non_matches_and_the_fallback_rate(tmp_path):
    p = tmp_path / "ev.jsonl"
    _write(p, EVENTS)
    s = summarise(p)
    assert s["shadow"] == {"events": 2, "non_match": 1}
    assert s["auto"] == {"calls": 4, "fallbacks": 1, "fallback_rate": 0.25, "reasons": {"auth": 1}}
    assert s["unreadable_lines"] == 1
    assert {"backend": "shadow", "verb": "list-mail", "outcome": "graph_error", "n": 1} in s[
        "counts"
    ]
    assert s["vanished"] == 2
    text = render(s)
    assert "vanished: 2" in text
    assert "1 not matching" in text and "(25.00%)" in text and "auth 1" in text


def test_since_drops_older_events(tmp_path):
    p = tmp_path / "ev.jsonl"
    _write(p, EVENTS)
    s = summarise(p, since=datetime(2026, 11, 10, tzinfo=UTC))
    assert s["shadow"]["events"] == 0 and s["auto"]["calls"] == 4


def test_a_missing_file_is_an_empty_report(tmp_path):
    s = summarise(tmp_path / "none.jsonl")
    assert s["auto"]["fallback_rate"] is None and s["counts"] == []
    assert "n/a" in render(s)


def test_the_cli_prints_it_as_json(tmp_path):
    p = tmp_path / "ev.jsonl"
    _write(p, EVENTS)
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.cli",
            "mail-backend-report",
            "--path",
            str(p),
            "--json",
            "--since",
            "2026-11-13T00:00:00Z",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(out.stdout)["auto"]["fallbacks"] == 1
