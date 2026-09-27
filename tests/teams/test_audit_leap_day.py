"""pull_messages runs on 29 February.

The 12-month activity cutoff was now.replace(year=now.year - 1), which raises
ValueError on 29 February and would have failed every teams-sync run that day
before a single chat was pulled (audit teams-calendar-10).
"""

from datetime import UTC, datetime
from unittest.mock import patch

from src.export.teams_export import pull_messages


class _LeapDay(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2028, 2, 29, 10, 0, tzinfo=UTC)


def test_pull_messages_runs_on_a_leap_day(db):
    db.execute(
        "INSERT INTO teams_chats (teams_chat_id, chat_kind, topic, first_seen_at) "
        "VALUES ('19:leap@thread.v2', 'group', 'x', '2028-01-01T00:00:00')"
    )
    db.commit()

    with (
        patch("src.export.teams_export.datetime", _LeapDay),
        patch("src.export.teams_export.run_teams_cli", return_value={"messages": []}),
    ):
        result = pull_messages(db, concurrency=1)

    assert result["chats_pulled"] == 1
