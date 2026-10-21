"""Thread participants, readable titles, the MRI cache and empty summaries (audit 2026-10-20).

- teams_threads.participants was never written: '[]' or NULL on all 10,190 threads.
- 7,109 titles began '? / ': the label was 'team / topic', and only a channel has a
  team, while a DM or an unnamed group has no topic either.
- 2,311 of 2,709 'permanent_fail' rows of the MRI cache were chat IDs, and more were
  apps and visitors: nobody a person lookup can name.
- 76 threads extracted on 2026-05-04 have an empty summary, so no vector, and 127 key
  facts hang off them.

bound_threads and the extraction now write the first two right; teams_repair mends
what is stored. Synthetic people only.
"""

import json
import sqlite3
from unittest.mock import patch

import pytest

from src.extract import teams_repair
from src.extract.teams_pipeline import chat_label
from src.extract.teams_threads import bound_threads
from tests.brain_cli import brain

A = "8:orgid:00000000-0000-0000-0000-00000000000a"
B = "8:orgid:00000000-0000-0000-0000-00000000000b"
LONG = "We agreed to move the launch to the second week and to tell the branches today."


def _chat(db, kind, topic=None, team=None):
    db.execute(
        "INSERT INTO teams_chats (teams_chat_id, chat_kind, topic, team_name, first_seen_at)"
        " VALUES (?, ?, ?, ?, '2026-08-01T00:00:00')",
        (f"19:{kind}-{topic}@thread.v2", kind, topic, team),
    )
    return db.execute("SELECT max(id) FROM teams_chats").fetchone()[0]


def _msg(db, chat_id, n, mri, name, text=LONG, at=None, system=0):
    db.execute(
        "INSERT INTO teams_messages (teams_message_id, chat_id, sender_mri, sender_display_name,"
        " composed_at, content_text, is_system) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (f"{chat_id}::{n}", chat_id, mri, name, at or f"2026-10-01T08:0{n}:00Z", text, system),
    )


def test_bound_threads_writes_who_wrote_in_order(db):
    chat = _chat(db, "group", "Launch")
    _msg(db, chat, 1, B, "Beta, Bea")
    _msg(db, chat, 2, A, "Alpha, Al")
    _msg(db, chat, 3, B, "Beta, Bea")
    _msg(db, chat, 4, "19:group-Launch@thread.v2", None, "added a member", system=1)
    db.commit()

    bound_threads(db)

    row = db.execute("SELECT participants FROM teams_threads").fetchone()
    assert json.loads(row[0]) == [B, A]


@pytest.mark.parametrize(
    ("kind", "topic", "team", "names", "label"),
    [
        ("channel", "Ops", "Team", [], "Team / Ops"),
        ("group", "Launch", None, ["Alpha, Al"], "Launch"),
        ("meeting", "Weekly review", None, [], "Weekly review"),
        ("oneOnOne", None, None, ["Owner, Olga", "Alpha, Al"], "DM with Alpha, Al"),
        ("oneOnOne", None, None, ["Owner, Olga"], "DM"),
        (
            "group",
            None,
            None,
            ["Alpha, Al", "Beta, Bea", "Gamma, Gus", "Delta, Dee", "Owner, Olga"],
            "Group chat with Alpha, Al, Beta, Bea, Gamma, Gus +1",
        ),
        ("meeting", None, None, [], "Meeting chat"),
    ],
)
def test_a_chat_label_never_reads_question_marks(monkeypatch, kind, topic, team, names, label):
    from src import config

    monkeypatch.setattr(config, "USER_NAME", "Olga Owner")
    thread = {
        "chat_kind": kind,
        "chat_topic": topic,
        "team_name": team,
        "participant_display_names": json.dumps(names),
    }
    assert chat_label(thread) == label


def _thread(db, chat, title, status="extracted", names=("Alpha, Al",), summary="s"):
    db.execute(
        "INSERT INTO teams_threads (chat_id, thread_kind, anchor_message_id, started_at, ended_at,"
        " message_count, participants, participant_display_names, title, summary,"
        " extraction_status) VALUES (?, 'chat_session', ?, '2026-10-01T08:00:00Z',"
        " '2026-10-01T09:00:00Z', 1, '[]', ?, ?, ?, ?)",
        (chat, f"a{title}", json.dumps(list(names)), title, summary, status),
    )
    return db.execute("SELECT max(id) FROM teams_threads").fetchone()[0]


def _in_thread(db, thread_id):
    db.execute(
        "UPDATE teams_messages SET thread_id = ? WHERE thread_id IS NULL AND chat_id ="
        " (SELECT chat_id FROM teams_threads WHERE id = ?)",
        (thread_id, thread_id),
    )


def _seed_store(db):
    """A DM and a named group, both with the old '? / ...' titles and no participants, a
    channel post stored with no sender, and the MRI cache's rows."""
    dm = _chat(db, "oneOnOne")
    _msg(db, dm, 1, A, "Alpha, Al", "ok")
    _msg(db, dm, 2, A, "Alpha, Al")
    dm_thread = _thread(db, dm, "? / ?: <p>ok</p>")
    _in_thread(db, dm_thread)
    group = _chat(db, "group", "Launch")
    _msg(db, group, 1, B, "Beta, Bea")
    group_thread = _thread(db, group, "? / Launch: something", names=("Beta, Bea",))
    _in_thread(db, group_thread)
    for mri, status in (
        ("19:00ab@thread.v2", "permanent_fail"),
        ("19:meeting_xyz@thread.v2", "permanent_fail"),
        ("28:00000000-0000-0000-0000-00000000b0b0", "permanent_fail"),
        ("8:teamsvisitor:0000", "permanent_fail"),
        ("8:orgid:00000000-0000-0000-0000-0000000000dd", "permanent_fail"),
        (A, "resolved"),
    ):
        db.execute("INSERT INTO teams_mri_resolution (mri, status) VALUES (?, ?)", (mri, status))
    db.commit()
    return dm_thread, group_thread


def test_repair_fills_participants_retitles_and_cleans_the_cache(db):
    dm_thread, group_thread = _seed_store(db)

    counts = teams_repair.repair(db)

    rows = {
        r["id"]: r
        for r in db.execute("SELECT id, participants, title, extraction_status FROM teams_threads")
    }
    assert json.loads(rows[dm_thread]["participants"]) == [A]
    assert json.loads(rows[group_thread]["participants"]) == [B]
    # The first message extraction reads, as the extraction titles it.
    assert rows[dm_thread]["title"] == f"DM with Alpha, Al: {LONG[:50]}…"
    assert rows[group_thread]["title"] == f"Launch: {LONG[:50]}…"
    # Nothing goes back to extraction.
    assert {r["extraction_status"] for r in rows.values()} == {"extracted"}
    left = [r[0] for r in db.execute("SELECT mri FROM teams_mri_resolution ORDER BY mri")]
    assert left == [A, "8:orgid:00000000-0000-0000-0000-0000000000dd"]
    assert counts["participants_set"] == 2
    assert counts["titles_fixed"] == 2
    assert counts["mri_rows_deleted"] == 4


def test_repair_twice_changes_nothing_and_a_dry_run_writes_nothing(db):
    _seed_store(db)
    before = db.execute("SELECT title, participants FROM teams_threads ORDER BY id").fetchall()

    dry = teams_repair.repair(db, dry_run=True)

    assert dry["titles_fixed"] == 2
    after = db.execute("SELECT title, participants FROM teams_threads ORDER BY id").fetchall()
    assert [tuple(r) for r in after] == [tuple(r) for r in before]
    assert db.execute("SELECT COUNT(*) FROM teams_mri_resolution").fetchone()[0] == 6

    teams_repair.repair(db)
    again = teams_repair.repair(db)
    assert {k: v for k, v in again.items() if v} == {}


def test_reset_empty_summaries_requeues_them_and_drops_their_facts(db):
    chat = _chat(db, "group", "Launch")
    empty = _thread(db, chat, "t1", summary="")
    kept = _thread(db, chat, "t2", summary="A summary.")
    for tid in (empty, empty, kept):
        db.execute("INSERT INTO key_facts (fact, teams_thread_id) VALUES ('a fact', ?)", (tid,))
    db.commit()

    dry = teams_repair.reset_empty_summaries(db, dry_run=True)
    assert dry == {"threads": 1, "key_facts": 2, "decisions": 0, "action_items": 0}
    assert db.execute("SELECT COUNT(*) FROM key_facts").fetchone()[0] == 3

    done = teams_repair.reset_empty_summaries(db)

    assert done == dry
    status = dict(db.execute("SELECT id, extraction_status FROM teams_threads"))
    assert status == {empty: "pending", kept: "extracted"}
    assert [r[0] for r in db.execute("SELECT teams_thread_id FROM key_facts")] == [kept]
    assert teams_repair.reset_empty_summaries(db) == {
        "threads": 0,
        "key_facts": 0,
        "decisions": 0,
        "action_items": 0,
    }


@pytest.mark.parametrize("name", ["teams-repair", "teams-empty-summaries"])
@pytest.mark.parametrize("dry", [[], ["--dry-run"]])
def test_the_repair_commands_refuse_on_a_replica(tmp_path, monkeypatch, capsys, name, dry):
    db_path = tmp_path / "brain.db"
    sqlite3.connect(db_path).close()
    monkeypatch.setenv("BRAIN_ROLE", "replica")
    with (
        patch("src.extract.teams_repair.repair") as repair,
        patch("src.extract.teams_repair.reset_empty_summaries") as reset,
    ):
        code, _, err = brain(monkeypatch, capsys, "--db", str(db_path), "maint", name, *dry)

    assert code == 2
    assert "replica" in err
    repair.assert_not_called()
    reset.assert_not_called()


def test_the_repair_command_prints_its_counts(db, tmp_path, monkeypatch, capsys):
    _seed_store(db)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setenv("BRAIN_ROLE", "producer")

    code, out, _ = brain(monkeypatch, capsys, "--db", path, "maint", "teams-repair", "--dry-run")

    assert code == 0
    assert "dry run" in out
    assert "titles" in out and "2" in out
