"""Tests for teams_mri.resolve_mris — MRI cache and people back-link."""

from unittest.mock import patch

import pytest

from src.export.teams_cli import TeamsCliAuthRequired, TeamsCliError
from src.extract.teams_mri import resolve_mris


def _seed_chat_with_message(db, mri="8:orgid:aaaa-1111", display="Novak, Maria"):
    db.execute(
        """INSERT INTO teams_chats(teams_chat_id, chat_kind, first_seen_at)
           VALUES ('19:test', 'channel', '2026-04-01T00:00:00')"""
    )
    chat_id = db.execute("SELECT id FROM teams_chats").fetchone()["id"]
    db.execute(
        """INSERT INTO teams_messages(
             teams_message_id, chat_id, composed_at, content_text,
             sender_mri, sender_display_name
           ) VALUES (?, ?, '2026-04-29T08:00:00', 'hi', ?, ?)""",
        (f"{chat_id}::M1", chat_id, mri, display),
    )
    db.commit()


def _seed_person(db, email="novak@example.com", name="Novak, Maria"):
    db.execute("INSERT INTO people(name, email) VALUES (?, ?)", (name, email))
    db.commit()
    return db.execute("SELECT id FROM people WHERE email = ?", (email,)).fetchone()["id"]


def _add_message(db, suffix, mri="8:orgid:aaaa-1111", display="Novak, Maria"):
    """Append another message to the seeded chat, as a later sync would."""
    chat_id = db.execute("SELECT id FROM teams_chats").fetchone()["id"]
    db.execute(
        """INSERT INTO teams_messages(
             teams_message_id, chat_id, composed_at, content_text,
             sender_mri, sender_display_name
           ) VALUES (?, ?, '2026-04-30T08:00:00', 'later', ?, ?)""",
        (f"{chat_id}::{suffix}", chat_id, mri, display),
    )
    db.commit()
    return f"{chat_id}::{suffix}"


def _seed_resolution(db, mri, person_id, email="novak@example.com", name="Novak, Maria"):
    """Write a already-resolved cache row directly, as an earlier run would have."""
    db.execute(
        """INSERT INTO teams_mri_resolution(
             mri, email, display_name, person_id, status, resolved_at, last_attempt_at
           ) VALUES (?, ?, ?, ?, 'resolved', '2026-04-29T08:00:00', '2026-04-29T08:00:00')""",
        (mri, email, name, person_id),
    )
    db.commit()


def test_resolve_mris_caches_and_back_links_person(db, fixture_loader):
    _seed_chat_with_message(db)
    person_id = _seed_person(db)
    payload = fixture_loader("resolve-mri.json")

    with patch("src.extract.teams_mri.run_teams_cli") as mock:
        mock.return_value = payload
        result = resolve_mris(db)

    assert result["resolved"] == 1
    cache = db.execute("SELECT email, person_id, status FROM teams_mri_resolution").fetchone()
    assert cache["email"] == "novak@example.com"
    assert cache["person_id"] == person_id
    assert cache["status"] == "resolved"

    # Back-link applied to the message.
    msg = db.execute("SELECT sender_person_id FROM teams_messages").fetchone()
    assert msg["sender_person_id"] == person_id


def test_resolve_mris_skips_already_cached(db, fixture_loader):
    _seed_chat_with_message(db)
    _seed_person(db)
    payload = fixture_loader("resolve-mri.json")

    with patch("src.extract.teams_mri.run_teams_cli") as mock:
        mock.return_value = payload
        resolve_mris(db)
        # Second call: nothing new to resolve.
        mock.reset_mock()
        result = resolve_mris(db)
        mock.assert_not_called()

    assert result["resolved"] == 0


def test_resolve_mris_marks_404_permanent_fail(db):
    _seed_chat_with_message(db, mri="8:orgid:zzzz-9999")

    err = TeamsCliError(exit_code=5, stderr='{"status":404}', retryable=True)
    with patch("src.extract.teams_mri.run_teams_cli", side_effect=err):
        result = resolve_mris(db)

    assert result["resolved"] == 0
    assert result["permanent_fail"] == 1
    cache = db.execute("SELECT status FROM teams_mri_resolution").fetchone()
    assert cache["status"] == "permanent_fail"


def test_backfill_attributes_messages_that_arrived_after_resolution(db, fixture_loader):
    # The one-shot UPDATE inside the loop only ever saw the messages that
    # existed at resolution time, and the candidate query skips MRIs already
    # 'resolved', so a sender resolved once was never revisited. Every message
    # arriving afterwards kept sender_person_id NULL forever (10,210 of them,
    # 42% of attributable Teams traffic).
    _seed_chat_with_message(db)
    person_id = _seed_person(db)
    payload = fixture_loader("resolve-mri.json")

    with patch("src.extract.teams_mri.run_teams_cli", return_value=payload):
        resolve_mris(db)

    later_id = _add_message(db, "M2")
    with patch("src.extract.teams_mri.run_teams_cli"):
        resolve_mris(db)

    later = db.execute(
        "SELECT sender_person_id FROM teams_messages WHERE teams_message_id = ?",
        (later_id,),
    ).fetchone()
    assert later["sender_person_id"] == person_id


def test_backfill_runs_when_there_is_nothing_new_to_resolve(db):
    # The common case: the cache is warm, the candidate list is empty, and the
    # loop body never executes. The back-fill has to run anyway.
    _seed_chat_with_message(db)
    person_id = _seed_person(db)
    _seed_resolution(db, "8:orgid:aaaa-1111", person_id)

    with patch("src.extract.teams_mri.run_teams_cli") as mock:
        result = resolve_mris(db)
        mock.assert_not_called()

    assert result["resolved"] == 0
    msg = db.execute("SELECT sender_person_id FROM teams_messages").fetchone()
    assert msg["sender_person_id"] == person_id


def test_backfill_is_idempotent(db):
    _seed_chat_with_message(db)
    person_id = _seed_person(db)
    _seed_resolution(db, "8:orgid:aaaa-1111", person_id)

    with patch("src.extract.teams_mri.run_teams_cli"):
        resolve_mris(db)
        before = db.execute(
            "SELECT teams_message_id, sender_person_id FROM teams_messages ORDER BY 1"
        ).fetchall()
        changes_before = db.total_changes

        resolve_mris(db)

    after = db.execute(
        "SELECT teams_message_id, sender_person_id FROM teams_messages ORDER BY 1"
    ).fetchall()
    assert [tuple(r) for r in after] == [tuple(r) for r in before]
    # Stronger than "same values written twice": the second pass rewrites no rows.
    assert db.total_changes == changes_before


def test_backfill_skips_resolution_whose_person_row_is_gone(db):
    # teams_mri_resolution.person_id can outlive the people row that dedup
    # merged away. teams_messages.sender_person_id REFERENCES people(id), so
    # writing that stale id back would plant a dangling foreign key.
    _seed_chat_with_message(db, mri="8:orgid:bbbb-2222")
    _seed_resolution(db, "8:orgid:bbbb-2222", person_id=4242, email="gone@example.com")

    with patch("src.extract.teams_mri.run_teams_cli") as mock:
        resolve_mris(db)
        mock.assert_not_called()

    msg = db.execute("SELECT sender_person_id FROM teams_messages").fetchone()
    assert msg["sender_person_id"] is None


def test_backfill_leaves_no_resolvable_message_unattributed(db, fixture_loader):
    _seed_chat_with_message(db)
    person_id = _seed_person(db)
    payload = fixture_loader("resolve-mri.json")
    with patch("src.extract.teams_mri.run_teams_cli", return_value=payload):
        resolve_mris(db)
    for i in range(2, 6):
        _add_message(db, f"M{i}")
    # A bot MRI never resolves to a person; it must stay NULL and must not be
    # counted against us.
    _add_message(db, "M9", mri="28:cccc-3333", display="Some Bot")

    with patch("src.extract.teams_mri.run_teams_cli") as mock:
        resolve_mris(db)
        mock.assert_not_called()

    leftover = db.execute(
        """SELECT COUNT(*) AS n
           FROM teams_messages m
           JOIN teams_mri_resolution r ON r.mri = m.sender_mri
           JOIN people p ON p.id = r.person_id
           WHERE m.sender_person_id IS NULL AND r.status = 'resolved'"""
    ).fetchone()["n"]
    assert leftover == 0
    attributed = db.execute(
        "SELECT COUNT(*) AS n FROM teams_messages WHERE sender_person_id = ?", (person_id,)
    ).fetchone()["n"]
    assert attributed == 5


def test_resolve_mris_respects_rate_limit(db, fixture_loader):
    # Seed 10 messages with distinct MRIs.
    db.execute(
        """INSERT INTO teams_chats(teams_chat_id, chat_kind, first_seen_at)
           VALUES ('19:test', 'channel', '2026-04-01T00:00:00')"""
    )
    chat_id = db.execute("SELECT id FROM teams_chats").fetchone()["id"]
    for i in range(10):
        db.execute(
            """INSERT INTO teams_messages(
                 teams_message_id, chat_id, composed_at, content_text,
                 sender_mri, sender_display_name
               ) VALUES (?, ?, '2026-04-29T08:00:00', 'hi', ?, ?)""",
            (f"{chat_id}::M{i}", chat_id, f"8:orgid:oid-{i}", f"User {i}"),
        )
    db.commit()

    payload = fixture_loader("resolve-mri.json")
    with patch("src.extract.teams_mri.run_teams_cli", return_value=payload) as mock:
        result = resolve_mris(db, max_per_run=3)

    assert result["resolved"] == 3
    assert mock.call_count == 3


def test_resolve_mris_retries_failed_status(db, fixture_loader):
    _seed_chat_with_message(db)
    _seed_person(db)
    err = TeamsCliError(exit_code=5, stderr="upstream 502", retryable=True)
    payload = fixture_loader("resolve-mri.json")

    with patch("src.extract.teams_mri.run_teams_cli") as mock:
        mock.side_effect = err
        first = resolve_mris(db)
        assert first["retryable_fail"] == 1

        # Cache row should be 'failed', not 'permanent_fail'
        cache = db.execute("SELECT status FROM teams_mri_resolution").fetchone()
        assert cache["status"] == "failed"

        # Subsequent call retries (because failed is in the WHERE clause)
        mock.side_effect = None
        mock.return_value = payload
        second = resolve_mris(db)

    assert second["resolved"] == 1
    cache = db.execute("SELECT status FROM teams_mri_resolution").fetchone()
    assert cache["status"] == "resolved"


def test_resolve_mris_propagates_auth_required(db):
    _seed_chat_with_message(db)
    with patch(
        "src.extract.teams_mri.run_teams_cli",
        side_effect=TeamsCliAuthRequired(stderr="auth required"),
    ):
        with pytest.raises(TeamsCliAuthRequired):
            resolve_mris(db)


def test_resolve_mris_skips_non_user_mri_without_graph_call(db):
    # Channel/thread IDs (19:...@thread.v2) and bot MRIs (28:) are not people —
    # resolve-mri rejects them. They must be marked permanent_fail WITHOUT a
    # Graph call so they aren't retried as 'failed' on every sync forever.
    _seed_chat_with_message(db, mri="19:eb01bc38049947f1@thread.v2", display="Some Channel")

    with patch("src.extract.teams_mri.run_teams_cli") as mock:
        result = resolve_mris(db)
        mock.assert_not_called()

    assert result["resolved"] == 0
    assert result["permanent_fail"] == 1
    cache = db.execute("SELECT status FROM teams_mri_resolution").fetchone()
    assert cache["status"] == "permanent_fail"
