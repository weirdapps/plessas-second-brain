"""The window calendar-sync lists: --since is honoured without --backfill."""

import types
from datetime import datetime, timedelta

from src.store.schema import create_database


def _db(tmp_path) -> str:
    path = tmp_path / "brain.db"
    if not path.exists():
        create_database(str(path)).close()
    return str(path)


def _since_listed(monkeypatch, tmp_path, **flags) -> datetime:
    """The `since` cmd_calendar_sync hands list_events for the given flags."""
    from src import cli
    from src.export import calendar_export

    seen: list[datetime] = []

    def record(since, until, failures=None, **kwargs):
        seen.append(since)
        return []

    monkeypatch.setattr(calendar_export, "list_events", record)
    args = types.SimpleNamespace(
        db=_db(tmp_path), backfill=False, since=None, until=None, skip_extraction=False
    )
    for name, value in flags.items():
        setattr(args, name, value)
    cli.cmd_calendar_sync(args)
    return seen[0]


def test_since_is_honoured_without_backfill(monkeypatch, tmp_path):
    """cli-6: `calendar-sync --since 2026-08-01`, the README's own example, listed
    the last seven days and exited 0, so the gap it was run to fill stayed open."""
    assert _since_listed(monkeypatch, tmp_path, since="2026-08-01") == datetime(2026, 8, 1)


def test_since_wins_over_backfill_too(monkeypatch, tmp_path):
    assert _since_listed(monkeypatch, tmp_path, since="2026-08-01", backfill=True) == datetime(
        2026, 8, 1
    )


def test_backfill_alone_lists_a_year_and_a_bare_run_a_week(monkeypatch, tmp_path):
    year = datetime.now() - _since_listed(monkeypatch, tmp_path, backfill=True)
    week = datetime.now() - _since_listed(monkeypatch, tmp_path)

    assert timedelta(days=364) < year <= timedelta(days=366)
    assert timedelta(days=6) < week <= timedelta(days=8)
