"""A chat session continues across bound_threads runs.

Only unassigned messages were considered, and prev_dt started at None for every
run, so the first new message of each run opened a new thread even when the
chat's latest session ended minutes before. A conversation that ran across an
hourly sync was extracted in pieces (audit teams-calendar-3: 862 of 5,133 chat
sessions should have been merged).
"""

from src.extract.teams_threads import bound_threads


def _chat(db, kind="group"):
    db.execute(
        "INSERT INTO teams_chats(teams_chat_id, chat_kind, topic, first_seen_at) "
        "VALUES ('19:session@thread.v2', ?, 'Example group', '2026-09-01T00:00:00')",
        (kind,),
    )
    return db.execute("SELECT id FROM teams_chats").fetchone()["id"]


def _msg(db, chat_id, mid, composed, *, is_system=0):
    db.execute(
        "INSERT INTO teams_messages(teams_message_id, chat_id, composed_at, content_text, "
        "is_system) VALUES (?, ?, ?, 'text', ?)",
        (f"{chat_id}::{mid}", chat_id, composed, is_system),
    )
    db.commit()


def _threads(db):
    return db.execute(
        "SELECT id, started_at, ended_at, message_count, extraction_status "
        "FROM teams_threads ORDER BY id"
    ).fetchall()


def test_a_message_within_the_gap_joins_the_previous_runs_session(db):
    chat_id = _chat(db)
    _msg(db, chat_id, "m1", "2026-09-25T12:23:08Z")
    _msg(db, chat_id, "m2", "2026-09-25T12:31:53Z")
    bound_threads(db)
    db.execute("UPDATE teams_threads SET extraction_status = 'done'")
    db.commit()

    _msg(db, chat_id, "m3", "2026-09-25T12:34:07Z")
    result = bound_threads(db)

    threads = _threads(db)
    assert len(threads) == 1
    assert result["threads_created"] == 0
    assert threads[0]["message_count"] == 3
    assert threads[0]["ended_at"] == "2026-09-25T12:34:07Z"
    # Grown, so it is extracted again with the whole conversation.
    assert threads[0]["extraction_status"] == "pending"


def test_a_message_past_the_gap_still_opens_a_new_session(db):
    chat_id = _chat(db)
    _msg(db, chat_id, "m1", "2026-09-25T08:00:00Z")
    bound_threads(db)

    _msg(db, chat_id, "m2", "2026-09-25T16:00:01Z")
    bound_threads(db)

    assert len(_threads(db)) == 2


def test_the_newest_session_is_the_one_continued(db):
    chat_id = _chat(db, kind="oneOnOne")
    _msg(db, chat_id, "m1", "2026-09-24T08:00:00Z")
    _msg(db, chat_id, "m2", "2026-09-25T08:00:00Z")
    bound_threads(db)

    _msg(db, chat_id, "m3", "2026-09-25T09:00:00Z")
    bound_threads(db)

    threads = _threads(db)
    assert [t["message_count"] for t in threads] == [1, 2]


def test_a_late_message_far_older_than_the_session_does_not_join_it(db):
    chat_id = _chat(db)
    _msg(db, chat_id, "m1", "2026-09-25T12:00:00Z")
    bound_threads(db)

    _msg(db, chat_id, "m0", "2026-09-20T12:00:00Z")
    bound_threads(db)

    threads = _threads(db)
    assert [t["started_at"] for t in threads] == ["2026-09-25T12:00:00Z", "2026-09-20T12:00:00Z"]


def test_a_session_opened_by_a_late_message_is_bounded_from_that_message(db):
    chat_id = _chat(db)
    _msg(db, chat_id, "m1", "2026-09-25T12:00:00Z")
    bound_threads(db)

    _msg(db, chat_id, "m0", "2026-09-20T12:00:00Z")
    _msg(db, chat_id, "m0b", "2026-09-20T21:00:00Z")
    bound_threads(db)

    assert [t["message_count"] for t in _threads(db)] == [1, 1, 1]
