"""A chat that answers 404 for a week is disabled, through the same breaker as a 403.

Two meeting chats answered "Location lookup failed" (404) on every run from
2026-09-29, two errors an hour for three weeks (audit 2026-10-20). Only a 403 was
read as permanent. A 404 is now too, once the chat has had no good read for
PERSISTENT_404_DAYS: a chat just created can 404 for a while, and a chat read last
week is evidence of nothing yet. It still goes through the share breaker, so a run
where 404s are everywhere disables nothing, and a token lapse never reads as a 404.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from src.export.teams_cli import TeamsCliAuthRequired
from src.export.teams_export import PERSISTENT_404_DAYS, pull_messages

_NOT_FOUND = (
    'teams-cli exit 5: {"code":"upstream","message":"Graph 404 HTTP_404: {\\"errorCode\\":404,'
    '\\"message\\":\\"{\\\\\\"subCode\\\\\\":\\\\\\"LocationLookupFailed\\\\\\"}\\"}","status":404}'
)


def _ago(days):
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _seed(db, n, first_seen_days=30, last_pulled_days=None, prefix="chat"):
    for i in range(n):
        db.execute(
            "INSERT INTO teams_chats (teams_chat_id, chat_kind, topic, first_seen_at,"
            " last_pulled_at) VALUES (?, 'meeting', 'm', ?, ?)",
            (
                f"19:meeting_{prefix}{i}@thread.v2",
                _ago(first_seen_days),
                None if last_pulled_days is None else _ago(last_pulled_days),
            ),
        )
    db.commit()


def _pull(db, bad):
    def cli(args, **kw):
        if args[-1] in bad:
            raise RuntimeError(_NOT_FOUND)
        return {"messages": []}

    with patch("src.export.teams_export.run_teams_cli", side_effect=cli):
        return pull_messages(db, concurrency=1)


def _disabled(db):
    return [
        r[0]
        for r in db.execute(
            "SELECT teams_chat_id FROM teams_chats WHERE ingest_disabled = 1 ORDER BY id"
        )
    ]


def test_a_chat_never_read_that_404s_for_weeks_is_disabled(db):
    _seed(db, 10)
    _seed(db, 1, first_seen_days=22, prefix="gone")
    bad = "19:meeting_gone0@thread.v2"

    result = _pull(db, {bad})

    assert result["errors"] == 1
    assert _disabled(db) == [bad]
    assert db.execute(
        "SELECT ingest_disabled_at FROM teams_chats WHERE teams_chat_id = ?", (bad,)
    ).fetchone()[0]


def test_a_chat_read_within_the_window_is_kept(db):
    _seed(db, 10)
    _seed(db, 1, first_seen_days=60, last_pulled_days=PERSISTENT_404_DAYS - 1, prefix="flaky")

    _pull(db, {"19:meeting_flaky0@thread.v2"})

    assert _disabled(db) == []


def test_a_new_chat_that_404s_is_kept(db):
    _seed(db, 10)
    _seed(db, 1, first_seen_days=0, prefix="new")

    _pull(db, {"19:meeting_new0@thread.v2"})

    assert _disabled(db) == []


def test_404s_everywhere_disable_nothing(db):
    _seed(db, 12)

    result = _pull(db, {f"19:meeting_chat{i}@thread.v2" for i in range(12)})

    assert result["errors"] == 12
    assert _disabled(db) == []


def test_an_expired_session_still_stops_the_run(db):
    _seed(db, 3)

    with (
        patch(
            "src.export.teams_export.run_teams_cli",
            side_effect=TeamsCliAuthRequired('{"code":"auth_required"}'),
        ),
        pytest.raises(TeamsCliAuthRequired),
    ):
        pull_messages(db, concurrency=1)

    assert _disabled(db) == []
