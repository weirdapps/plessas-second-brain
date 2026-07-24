"""Tests for teams_threads.bound_threads — channel scope only in Phase 1."""

from src.extract.teams_threads import (
    _CHAT_THREAD_GAP_HOURS,
    bound_threads,
)


def _insert_chat(db, kind="channel"):
    db.execute(
        """INSERT INTO teams_chats(teams_chat_id, chat_kind, first_seen_at)
           VALUES (?, ?, '2026-04-01T00:00:00')""",
        (f"19:test::{kind}", kind),
    )
    db.commit()
    return db.execute("SELECT id FROM teams_chats").fetchone()["id"]


def _insert_msg(db, chat_id, *, mid, composed, parent=None, content="hi"):
    db.execute(
        """INSERT INTO teams_messages(
             teams_message_id, chat_id, composed_at, content_text,
             parent_message_id, is_system
           ) VALUES (?, ?, ?, ?, ?, 0)""",
        (mid, chat_id, composed, content, parent),
    )


def test_channel_post_with_replies_becomes_one_thread(db):
    chat_id = _insert_chat(db, kind="channel")
    _insert_msg(
        db,
        chat_id,
        mid="P1",
        composed="2026-04-29T08:00:00",
        parent=None,
        content="Q2 roadmap, please review",
    )
    _insert_msg(
        db,
        chat_id,
        mid="R1",
        composed="2026-04-29T08:30:00",
        parent="P1",
        content="LGTM",
    )
    _insert_msg(
        db,
        chat_id,
        mid="R2",
        composed="2026-04-29T08:45:00",
        parent="P1",
        content="ship it",
    )
    db.commit()

    result = bound_threads(db)
    assert result["threads_created"] == 1

    thread = db.execute("SELECT * FROM teams_threads").fetchone()
    assert thread["thread_kind"] == "channel_post"
    assert thread["anchor_message_id"] == "P1"
    assert thread["message_count"] == 3
    assert thread["extraction_status"] == "pending"

    msg_thread_ids = [
        r["thread_id"]
        for r in db.execute("SELECT thread_id FROM teams_messages ORDER BY composed_at").fetchall()
    ]
    assert all(t == thread["id"] for t in msg_thread_ids)


def test_two_separate_posts_become_two_threads(db):
    chat_id = _insert_chat(db)
    _insert_msg(db, chat_id, mid="P1", composed="2026-04-29T08:00:00", content="topic A")
    _insert_msg(db, chat_id, mid="P2", composed="2026-04-29T10:00:00", content="topic B")
    db.commit()

    result = bound_threads(db)
    assert result["threads_created"] == 2


def test_new_reply_to_existing_thread_marks_pending_again(db):
    chat_id = _insert_chat(db)
    _insert_msg(db, chat_id, mid="P1", composed="2026-04-29T08:00:00", content="post")
    db.commit()
    bound_threads(db)

    # Mark thread as already extracted.
    db.execute("UPDATE teams_threads SET extraction_status='extracted', message_count=1")
    db.commit()

    # New reply lands.
    _insert_msg(
        db,
        chat_id,
        mid="R1",
        composed="2026-04-29T09:00:00",
        parent="P1",
        content="reply",
    )
    db.commit()
    bound_threads(db)

    thread = db.execute("SELECT extraction_status, message_count FROM teams_threads").fetchone()
    assert thread["extraction_status"] == "pending"
    assert thread["message_count"] == 2


def test_idempotent_when_no_new_messages(db):
    chat_id = _insert_chat(db)
    _insert_msg(db, chat_id, mid="P1", composed="2026-04-29T08:00:00", content="post")
    db.commit()
    bound_threads(db)
    result_2 = bound_threads(db)
    assert result_2["threads_created"] == 0


def test_comma_in_display_name_does_not_fragment_participants(db):
    """ACME display names like 'Novak, Maria' must survive participant aggregation."""
    import json as json_mod

    chat_id = _insert_chat(db)
    db.execute(
        """INSERT INTO teams_messages(
             teams_message_id, chat_id, composed_at, content_text,
             parent_message_id, is_system, sender_display_name
           ) VALUES (?, ?, ?, ?, ?, 0, ?)""",
        (
            f"{chat_id}::P1",
            chat_id,
            "2026-04-29T08:00:00",
            "post",
            None,
            "Novak, Maria",
        ),
    )
    db.execute(
        """INSERT INTO teams_messages(
             teams_message_id, chat_id, composed_at, content_text,
             parent_message_id, is_system, sender_display_name
           ) VALUES (?, ?, ?, ?, ?, 0, ?)""",
        (
            f"{chat_id}::R1",
            chat_id,
            "2026-04-29T08:30:00",
            "reply",
            "P1",
            "Papadopoulos, Nikos",
        ),
    )
    db.commit()
    bound_threads(db)

    thread = db.execute("SELECT participant_display_names FROM teams_threads").fetchone()
    participants = json_mod.loads(thread["participant_display_names"])
    assert sorted(participants) == ["Novak, Maria", "Papadopoulos, Nikos"]


def test_system_messages_do_not_create_threads(db):
    """ThreadActivity/* messages are excluded from thread bounding."""
    chat_id = _insert_chat(db)
    db.execute(
        """INSERT INTO teams_messages(
             teams_message_id, chat_id, composed_at, content_text,
             parent_message_id, is_system, sender_display_name
           ) VALUES (?, ?, ?, ?, ?, 1, ?)""",
        (
            f"{chat_id}::SYS1",
            chat_id,
            "2026-04-29T08:00:00",
            "X joined the channel",
            None,
            None,
        ),
    )
    db.commit()
    result = bound_threads(db)
    assert result["threads_created"] == 0
    # System message stays unbound.
    msg = db.execute("SELECT thread_id FROM teams_messages").fetchone()
    assert msg["thread_id"] is None


def _seed_chat_with_messages(
    db, kind: str, topic: str | None, messages: list[tuple[str, str]]
) -> int:
    """Insert a non-channel chat + N messages. messages = [(composed_at_iso, content), ...].
    Returns chat_id (PK).
    """
    db.execute(
        """
        INSERT INTO teams_chats(
            teams_chat_id, chat_kind, topic, team_uuid, team_name,
            channel_id, first_seen_at
        ) VALUES (?, ?, ?, NULL, NULL, NULL, ?)
        """,
        (
            f"19:test-{kind}@thread.v2",
            kind,
            topic,
            "2026-04-01T00:00:00Z",
        ),
    )
    chat_id = db.execute(
        f"SELECT id FROM teams_chats WHERE teams_chat_id = '19:test-{kind}@thread.v2'"
    ).fetchone()["id"]
    for i, (ts, content) in enumerate(messages):
        db.execute(
            """
            INSERT INTO teams_messages(
                teams_message_id, chat_id, sender_mri, sender_display_name,
                composed_at, message_type, content_text, content_html,
                parent_message_id, is_system, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, '{}')
            """,
            (
                f"{chat_id}::msg-{i}",
                chat_id,
                "8:orgid:aaa",
                "Test User",
                ts,
                "Text",
                content,
                content,
            ),
        )
    db.commit()
    return chat_id


def test_chat_thread_gap_constant_is_8_hours():
    assert _CHAT_THREAD_GAP_HOURS == 8


def test_bound_threads_groups_messages_within_gap_into_one_thread(db):
    chat_id = _seed_chat_with_messages(
        db,
        "oneOnOne",
        None,
        [
            ("2026-05-01T08:00:00Z", "morning A"),
            ("2026-05-01T08:30:00Z", "morning B"),  # 30 min gap → same thread
            ("2026-05-01T15:00:00Z", "afternoon"),  # 6.5h gap → still same thread
        ],
    )
    bound_threads(db)
    thread_ids = [
        r[0]
        for r in db.execute(
            "SELECT thread_id FROM teams_messages WHERE chat_id = ? ORDER BY composed_at",
            (chat_id,),
        ).fetchall()
    ]
    assert len(set(thread_ids)) == 1  # all same thread


def test_bound_threads_splits_on_8h_gap(db):
    chat_id = _seed_chat_with_messages(
        db,
        "oneOnOne",
        None,
        [
            ("2026-05-01T08:00:00Z", "morning"),
            ("2026-05-01T16:30:00Z", "evening"),  # 8.5h gap → new thread
        ],
    )
    bound_threads(db)
    thread_ids = [
        r[0]
        for r in db.execute(
            "SELECT thread_id FROM teams_messages WHERE chat_id = ? ORDER BY composed_at",
            (chat_id,),
        ).fetchall()
    ]
    assert len(set(thread_ids)) == 2


def test_bound_threads_uses_chat_session_thread_kind_for_oneOnOne(db):
    _seed_chat_with_messages(
        db,
        "oneOnOne",
        None,
        [("2026-05-01T08:00:00Z", "hi")],
    )
    bound_threads(db)
    thread = db.execute("SELECT thread_kind, title FROM teams_threads").fetchone()
    assert thread["thread_kind"] == "chat_session"


def test_bound_threads_group_chat_title_uses_topic(db):
    _seed_chat_with_messages(
        db,
        "group",
        "Cards strategy",
        [("2026-05-01T08:00:00Z", "agenda")],
    )
    bound_threads(db)
    title = db.execute("SELECT title FROM teams_threads").fetchone()["title"]
    assert "Cards strategy" in title
