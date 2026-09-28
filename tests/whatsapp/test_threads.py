"""brain whatsapp-sync, step 2: messages into gap-bounded sessions per chat."""

from src.export.whatsapp_export import ingest_snapshot
from src.extract.whatsapp_threads import bound_threads
from tests.whatsapp.conftest import ALICE, DIRECT_JID, OWNER, build_snapshot


def _msg(mid, content, ts, sender=ALICE, from_me=0):
    return (mid, DIRECT_JID, sender, content, ts, from_me, "", "")


def test_a_long_silence_starts_a_new_session(db, tmp_path):
    rows = [
        _msg("m1", "morning", "2026-09-01 09:00:00+03:00"),
        _msg("m2", "reply", "2026-09-01 09:30:00+03:00", OWNER, 1),
        _msg("m3", "evening, next day", "2026-09-02 20:00:00+03:00"),
    ]
    ingest_snapshot(db, build_snapshot(tmp_path, rows))
    out = bound_threads(db)
    assert out["threads_created"] == 2
    counts = [r[0] for r in db.execute("SELECT message_count FROM whatsapp_threads ORDER BY id")]
    assert counts == [2, 1]


def test_a_session_continues_across_runs(db, tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = [_msg("m1", "hi", "2026-09-01 09:00:00+03:00")]
    ingest_snapshot(db, build_snapshot(tmp_path / "a", first))
    bound_threads(db)
    second = first + [_msg("m2", "still here", "2026-09-01 10:00:00+03:00")]
    ingest_snapshot(db, build_snapshot(tmp_path / "b", second))
    out = bound_threads(db)
    assert out["threads_created"] == 0
    assert db.execute("SELECT COUNT(*) FROM whatsapp_threads").fetchone()[0] == 1
    assert db.execute("SELECT message_count FROM whatsapp_threads").fetchone()[0] == 2


def test_a_touched_thread_goes_back_to_pending(db, tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = [_msg("m1", "hi", "2026-09-01 09:00:00+03:00")]
    ingest_snapshot(db, build_snapshot(tmp_path / "a", first))
    bound_threads(db)
    db.execute("UPDATE whatsapp_threads SET extraction_status = 'extracted'")
    db.commit()
    ingest_snapshot(
        db,
        build_snapshot(tmp_path / "b", first + [_msg("m2", "more", "2026-09-01 09:10:00+03:00")]),
    )
    bound_threads(db)
    assert db.execute("SELECT extraction_status FROM whatsapp_threads").fetchone()[0] == "pending"


def test_participants_and_title_name_the_chat(db, tmp_path):
    rows = [_msg("m1", "hi", "2026-09-01 09:00:00+03:00")]
    ingest_snapshot(db, build_snapshot(tmp_path, rows))
    bound_threads(db)
    title, names = db.execute("SELECT title, participant_names FROM whatsapp_threads").fetchone()
    assert "Alice Example" in title and "2026-09-01" in title
    assert "Alice Example" in names
