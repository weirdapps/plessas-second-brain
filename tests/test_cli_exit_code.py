"""cli.main exits with a command's integer return code.

main() called args.func(args) and discarded the result, so no command could
report failure except by raising or calling sys.exit itself. calendar-sync
counted failed chunks, failed bodies and failed extractions and still exited 0,
and the wrappers' dead-man switches pinged green over it.
"""

import sys

import pytest

from src import cli


def _run_stats_returning(monkeypatch, value):
    monkeypatch.setattr(cli, "cmd_stats", lambda args: value)
    monkeypatch.setattr(sys, "argv", ["brain", "stats"])
    cli.main()


def test_main_exits_with_the_command_return_code(monkeypatch):
    with pytest.raises(SystemExit) as exc:
        _run_stats_returning(monkeypatch, 3)
    assert exc.value.code == 3


def test_main_returns_normally_when_the_command_returns_nothing(monkeypatch):
    _run_stats_returning(monkeypatch, None)


def test_zero_and_booleans_are_not_failures(monkeypatch):
    """Only a non-zero int is a status. Every command returns None today, and a
    stray True must not turn into exit code 1."""
    for value in (0, True, False, "text"):
        _run_stats_returning(monkeypatch, value)
