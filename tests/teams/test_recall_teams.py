"""Tests for the recall() unified MCP tool — teams kind."""

from src.store.recall import recall


def _seed_extracted_thread(db, *, title, summary):
    db.execute(
        """INSERT INTO teams_chats(teams_chat_id, chat_kind, topic, team_name, first_seen_at)
           VALUES ('19:t', 'channel', 'Cards Strategy', 'Cards & Digital', '2026-04-01')"""
    )
    chat_id = db.execute("SELECT id FROM teams_chats").fetchone()["id"]
    db.execute(
        """INSERT INTO teams_threads(
             chat_id, thread_kind, anchor_message_id, started_at, ended_at,
             message_count, extraction_status, title, summary, participant_display_names
           ) VALUES (?, 'channel_post', 'P1', '2026-04-29T08:00', '2026-04-29T09:00',
                     2, 'extracted', ?, ?, '[\"Alice\"]')""",
        (chat_id, title, summary),
    )
    db.commit()


def test_recall_includes_teams_kind_when_thread_summary_matches(db):
    _seed_extracted_thread(
        db,
        title="Cards & Digital: Q2 roadmap",
        summary="Novak shared the Q2 cards issuance roadmap; Papadopoulos approved.",
    )
    out = recall(db, "roadmap", limit_per_kind=5)
    assert "teams" in out
    assert len(out["teams"]) == 1
    assert "roadmap" in out["teams"][0]["summary"].lower()


def test_recall_teams_empty_when_no_match(db):
    _seed_extracted_thread(db, title="X", summary="some unrelated content")
    out = recall(db, "blockchain")
    assert out["teams"] == []
