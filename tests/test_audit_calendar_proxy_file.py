"""An unreadable canonical_people.json is reported, and does not rewrite the self flags."""

import json
import logging
import sqlite3
import types

from src.store.calendar_loader import load_proxy_emails
from src.store.schema import create_database

_BROKEN = '[{"email": "pa@example.com", "is_proxy_for_self": true},\n<<<<<<< HEAD\n'


def test_an_unreadable_proxy_file_is_reported_not_silent(tmp_path, caplog):
    """dedup-actions-2: the missing file warned and a malformed one did not, so a
    merge conflict in the private repo read exactly like a file with no proxies."""
    path = tmp_path / "canonical_people.json"
    path.write_text(_BROKEN)

    with caplog.at_level(logging.WARNING):
        result = load_proxy_emails(str(path))

    assert result is None, "unreadable must be told apart from absent"
    assert "canonical_people.json" in caplog.text


def test_a_missing_proxy_file_is_still_an_empty_set(tmp_path):
    assert load_proxy_emails(str(tmp_path / "canonical_people.json")) == set()


def test_calendar_sync_leaves_the_self_flags_alone_when_the_proxy_file_is_unreadable(
    monkeypatch, tmp_path, capsys
):
    """refresh_self_flags recomputes every stored row. Run over an empty proxy set
    it rewrote every event the PA booked as not the owner's, on every run."""
    import src.config
    from src import cli
    from src.export import calendar_export

    db_path = str(tmp_path / "brain.db")
    conn = create_database(db_path)
    conn.execute(
        "INSERT INTO calendar_events (outlook_event_id, organizer_email, start_at, end_at, "
        "is_self_organized, ingested_at) VALUES ('e1', 'pa@example.com', "
        "'2026-08-12T09:00:00.0000000Z', '2026-08-12T10:00:00.0000000Z', 1, '2026-08-12')"
    )
    conn.commit()
    conn.close()

    (tmp_path / "canonical_people.json").write_text(_BROKEN)
    monkeypatch.setattr(cli, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(src.config, "USER_EMAIL_PATTERN", "owner@example.com")
    monkeypatch.setattr(calendar_export, "list_events", lambda since, until, failures=None: [])

    cli.cmd_calendar_sync(
        types.SimpleNamespace(
            db=db_path, backfill=False, since=None, until=None, skip_extraction=False
        )
    )

    flag = sqlite3.connect(db_path).execute("SELECT is_self_organized FROM calendar_events")
    assert flag.fetchone()[0] == 1
    assert "leaving the stored self flags alone" in capsys.readouterr().err


def test_a_readable_proxy_file_still_refreshes_the_flags(monkeypatch, tmp_path):
    import src.config
    from src import cli
    from src.export import calendar_export

    db_path = str(tmp_path / "brain.db")
    conn = create_database(db_path)
    conn.execute(
        "INSERT INTO calendar_events (outlook_event_id, organizer_email, start_at, end_at, "
        "is_self_organized, ingested_at) VALUES ('e1', 'pa@example.com', "
        "'2026-08-12T09:00:00.0000000Z', '2026-08-12T10:00:00.0000000Z', 0, '2026-08-12')"
    )
    conn.commit()
    conn.close()

    (tmp_path / "canonical_people.json").write_text(
        json.dumps([{"email": "pa@example.com", "is_proxy_for_self": True}])
    )
    monkeypatch.setattr(cli, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(src.config, "USER_EMAIL_PATTERN", "owner@example.com")
    monkeypatch.setattr(calendar_export, "list_events", lambda since, until, failures=None: [])

    cli.cmd_calendar_sync(
        types.SimpleNamespace(
            db=db_path, backfill=False, since=None, until=None, skip_extraction=False
        )
    )

    flag = sqlite3.connect(db_path).execute("SELECT is_self_organized FROM calendar_events")
    assert flag.fetchone()[0] == 1
