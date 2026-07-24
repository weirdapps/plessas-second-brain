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
