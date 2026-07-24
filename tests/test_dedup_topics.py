"""Tests for topic consolidation (src/store/dedup_topics.py + normalize_topic)."""

from src.store.dedup_topics import run_topic_dedup
from src.store.normalizer import find_or_create_topic
from src.store.schema import create_database, get_connection, migrate_add_conversations


def _legacy_topic(conn, tid, name):
    """Insert a topic row raw (bypassing normalization) to simulate legacy data."""
    conn.execute("INSERT INTO topics (id, name, display_name) VALUES (?, ?, ?)", (tid, name, name))


class TestNormalizeTopicConsolidatesOnInsert:
    def test_find_or_create_merges_separator_variants(self):
        conn = create_database(":memory:")
        t1 = find_or_create_topic(conn, "Cards-Migration")
        t2 = find_or_create_topic(conn, "cards migration")
        t3 = find_or_create_topic(conn, "cards_migration")
        assert t1 == t2 == t3  # all map to one canonical topic now
        conn.close()


class TestTopicDedupPass:
    def test_merges_separator_variants_and_reassigns_links(self, tmp_path):
        db = str(tmp_path / "b.db")
        conn = create_database(db)
        _legacy_topic(conn, 1, "cards-migration")
        _legacy_topic(conn, 2, "cards migration")
        _legacy_topic(conn, 3, "cards_migration")
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received, summary) VALUES (1, 1, '2026-01-01', 's')"
        )
        for tid in (1, 2, 3):
            conn.execute("INSERT INTO email_topics (email_id, topic_id) VALUES (1, ?)", (tid,))
        conn.commit()
        conn.close()

        res = run_topic_dedup(db)
        assert res["merged"] == 2

        conn = get_connection(db)
        names = [r[0] for r in conn.execute("SELECT name FROM topics")]
        assert names == ["cards migration"]  # single canonical survivor
        # The email keeps exactly one (deduped) link to the survivor.
        assert (
            conn.execute("SELECT COUNT(*) FROM email_topics WHERE email_id = 1").fetchone()[0] == 1
        )
        conn.close()

    def test_rekeys_lone_noncanonical_topic(self, tmp_path):
        db = str(tmp_path / "b.db")
        conn = create_database(db)
        _legacy_topic(conn, 1, "cards-migration")
        conn.commit()
        conn.close()

        res = run_topic_dedup(db)
        assert res["rekeyed"] == 1
        conn = get_connection(db)
        assert (
            conn.execute("SELECT name FROM topics WHERE id = 1").fetchone()[0] == "cards migration"
        )
        conn.close()

    def test_reassigns_conversation_topics_no_fk_crash(self, tmp_path):
        # Regression: conversation_topics also references topics(id); a merge must
        # reassign it or the DELETE trips the FK (caught on the live DB, not in-memory).
        db = str(tmp_path / "b.db")
        conn = create_database(db)
        migrate_add_conversations(conn)
        _legacy_topic(conn, 1, "cards-migration")
        _legacy_topic(conn, 2, "cards migration")
        conn.execute(
            "INSERT INTO conversations (id, session_id, started_at, workspace, project_name, "
            "turn_count, summary, topics_summary, created_at) "
            "VALUES (1, 'sess', '2026-01-01T00:00:00', '/w', 'w', 1, 'sum', 'top', '2026-01-01T00:00:00')"
        )
        conn.execute("INSERT INTO conversation_topics (conversation_id, topic_id) VALUES (1, 1)")
        conn.commit()
        conn.close()

        res = run_topic_dedup(db)
        assert res["merged"] == 1
        conn = get_connection(db)
        surviving = conn.execute("SELECT id FROM topics").fetchone()[0]
        row = conn.execute(
            "SELECT topic_id FROM conversation_topics WHERE conversation_id = 1"
        ).fetchone()
        assert row[0] == surviving  # link followed the merge, no FK crash
        conn.close()

    def test_dry_run_rolls_back(self, tmp_path):
        db = str(tmp_path / "b.db")
        conn = create_database(db)
        _legacy_topic(conn, 1, "a-b")
        _legacy_topic(conn, 2, "a b")
        conn.commit()
        conn.close()

        run_topic_dedup(db, dry_run=True)
        conn = get_connection(db)
        assert conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 2  # unchanged
        conn.close()
