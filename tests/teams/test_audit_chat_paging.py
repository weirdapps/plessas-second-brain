"""A chat read pages back to the newest message already stored.

Each read took the newest page only, 200 messages a chat, with no cursor. The busiest
chat fills 200 in about 37 hours, and the outage of 2026-10-20 lasted 23 hours, so an
outage of about a day and a half lost messages for good (audit S2-4). teams-cli now
pages back with --until and --max-pages; the pull passes the chat's newest stored
time, so a normal hour still reads one page, and says so when the page cap stops it
short. A CLI that predates the flags is read the old way for the rest of the run.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from src.export.teams_cli import TeamsCliError
from src.export.teams_export import CHAT_MAX_PAGES, CHAT_PAGE_SIZE, pull_messages

CHAT = "19:busy@thread.v2"
T0 = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}0Z"


def _message(n, at):
    return {
        "id": str(n),
        "messagetype": "Text",
        "content": f"message {n}",
        "imdisplayname": "Alpha, Al",
        "from": "https://emea.ng.msg.teams.microsoft.com/v1/users/ME/contacts/8:orgid:aa",
        "composetime": _iso(at),
    }


class FakeTeamsCli:
    """teams-cli list-messages over a chat's history, honouring --page-size, --until
    and --max-pages as the CLI does; `knows_paging=False` is a CLI from before them."""

    def __init__(self, history, knows_paging=True):
        self.history = sorted(history, key=lambda m: m["composetime"], reverse=True)
        self.knows_paging = knows_paging
        self.calls = []

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        if not self.knows_paging and ("--until" in args or "--max-pages" in args):
            raise TeamsCliError(1, "error: unknown option '--until'", retryable=False)

        def value(flag, default=None):
            return args[args.index(flag) + 1] if flag in args else default

        size = int(value("--page-size", 50))
        until = value("--until")
        max_pages = int(value("--max-pages", 1))
        out, pages = [], 0
        for start in range(0, max(len(self.history), 1), size):
            page = self.history[start : start + size]
            out += page
            pages += 1
            reached = until is None or (page and page[-1]["composetime"] <= until)
            if reached or start + size >= len(self.history):
                return {"messages": out, "pagesRead": pages, "complete": True}
            if pages >= max_pages:
                return {"messages": out, "pagesRead": pages, "complete": False}
        return {"messages": out, "pagesRead": pages, "complete": True}


def _seed_chat(db):
    db.execute(
        "INSERT INTO teams_chats (teams_chat_id, chat_kind, topic, first_seen_at)"
        " VALUES (?, 'group', 'Busy', '2026-08-01T00:00:00')",
        (CHAT,),
    )
    db.commit()


def _history(n_old, n_new, gap, every=timedelta(minutes=11)):
    """n_old messages up to T0, then a gap, then n_new messages `every` apart."""
    old = [_message(i, T0 - i * every) for i in range(n_old)]
    start = T0 + gap
    new = [_message(10_000 + i, start + i * every) for i in range(n_new)]
    return old, new


def _stored(db):
    return db.execute("SELECT COUNT(*) FROM teams_messages").fetchone()[0]


def test_a_three_day_gap_is_recovered_in_full(db):
    _seed_chat(db)
    old, new = _history(150, 393, gap=timedelta(minutes=11))  # 72 hours at one per 11 min
    with patch("src.export.teams_export.run_teams_cli", FakeTeamsCli(old)):
        pull_messages(db, concurrency=1)
    assert _stored(db) == 150

    cli = FakeTeamsCli(old + new)
    with patch("src.export.teams_export.run_teams_cli", cli):
        result = pull_messages(db, concurrency=1)

    assert result["messages_inserted"] == 393
    assert _stored(db) == 543
    (args,) = cli.calls
    # The newest stored message, as the store holds it, and the chat id still last.
    assert args[args.index("--until") + 1] == old[0]["composetime"]
    assert args[args.index("--max-pages") + 1] == str(CHAT_MAX_PAGES)
    assert args[-1] == CHAT


def test_a_normal_hour_costs_one_page_of_one_call_per_chat(db):
    _seed_chat(db)
    old, new = _history(150, 5, gap=timedelta(minutes=11))
    with patch("src.export.teams_export.run_teams_cli", FakeTeamsCli(old)):
        pull_messages(db, concurrency=1)

    cli = FakeTeamsCli(old + new)
    with patch("src.export.teams_export.run_teams_cli", cli):
        result = pull_messages(db, concurrency=1)

    assert len(cli.calls) == 1
    assert result["messages_inserted"] == 5


def test_a_chat_never_read_asks_for_one_page_as_before(db):
    _seed_chat(db)
    old, _ = _history(500, 0, gap=timedelta(0))
    cli = FakeTeamsCli(old)

    with patch("src.export.teams_export.run_teams_cli", cli):
        pull_messages(db, concurrency=1)

    (args,) = cli.calls
    assert "--until" not in args and "--max-pages" not in args
    assert _stored(db) == CHAT_PAGE_SIZE


def test_the_page_cap_is_said_out_loud(db, capsys):
    _seed_chat(db)
    old, new = _history(10, CHAT_PAGE_SIZE * (CHAT_MAX_PAGES + 2), gap=timedelta(days=20))
    with patch("src.export.teams_export.run_teams_cli", FakeTeamsCli(old)):
        pull_messages(db, concurrency=1)
    capsys.readouterr()

    with patch("src.export.teams_export.run_teams_cli", FakeTeamsCli(old + new)):
        result = pull_messages(db, concurrency=1)

    assert result["messages_inserted"] == CHAT_PAGE_SIZE * CHAT_MAX_PAGES
    err = capsys.readouterr().err
    assert CHAT in err
    assert "page cap" in err
    assert old[0]["composetime"] in err


def test_a_cli_without_paging_is_read_one_page_for_the_rest_of_the_run(db, capsys):
    _seed_chat(db)
    db.execute(
        "INSERT INTO teams_chats (teams_chat_id, chat_kind, topic, first_seen_at)"
        " VALUES ('19:second@thread.v2', 'group', 'Second', '2026-08-01T00:00:00')"
    )
    db.commit()
    old, new = _history(150, 30, gap=timedelta(hours=1))
    with patch("src.export.teams_export.run_teams_cli", FakeTeamsCli(old)):
        pull_messages(db, concurrency=1)
    capsys.readouterr()

    cli = FakeTeamsCli(old + new, knows_paging=False)
    with patch("src.export.teams_export.run_teams_cli", cli):
        result = pull_messages(db, concurrency=1)

    assert result["errors"] == 0
    assert result["chats_pulled"] == 2
    # One refused paged call, then every read the old way: no second refusal.
    paged = [c for c in cli.calls if "--until" in c]
    assert len(paged) == 1
    assert len(cli.calls) == 3
    assert capsys.readouterr().err.count("does not page back") == 1
