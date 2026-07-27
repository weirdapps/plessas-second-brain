"""Fix #3: inline_images.visioned_at — a real freshness signal for the vision
pass (classified_at is only written at Stage-1 INSERT and never updated by
vision, so it froze and misrepresented staleness)."""

import sqlite3

from PIL import Image

from src.store.schema import create_database, run_migrations


def test_fresh_db_inline_images_has_visioned_at():
    """A freshly created DB must carry the visioned_at column."""
    conn = create_database(":memory:")
    run_migrations(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(inline_images)")}
    conn.close()
    assert "visioned_at" in cols


def test_migrate_add_visioned_at_adds_and_is_idempotent():
    """The migration adds visioned_at to a pre-v15 table and is safe to re-run."""
    from src.store.schema import migrate_add_visioned_at

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE inline_images (sha256 TEXT PRIMARY KEY, classified_at TIMESTAMP)")
    migrate_add_visioned_at(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(inline_images)")}
    assert "visioned_at" in cols
    # Re-running must not raise (idempotent).
    migrate_add_visioned_at(conn)
    conn.close()


def test_classify_with_vision_sets_visioned_at(tmp_path, monkeypatch):
    """The Stage-3 vision pass records when it ran in visioned_at."""
    from unittest.mock import MagicMock

    from src.extract import image_vision
    from src.extract.image_classifier import sha256_of_file

    p = tmp_path / "img.png"
    Image.new("RGB", (200, 200), (5, 5, 5)).save(p, "PNG")
    sha = sha256_of_file(p)

    conn = create_database(":memory:")
    run_migrations(conn)
    # Stage-1 row exists but unclassified (as it is before vision runs).
    conn.execute(
        "INSERT INTO inline_images "
        "(sha256, width, height, bytes, classification, classification_method, classified_at) "
        "VALUES (?, 200, 200, ?, 'unclassified', 'stage1', '2026-06-09T00:00:00')",
        (sha, p.stat().st_size),
    )
    conn.commit()

    fake_resp = MagicMock()
    fake_resp.content = [MagicMock(text="CONTENT: a bar chart of quarterly revenue")]
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_resp
    monkeypatch.setattr(
        "src.extract.claude_extract._get_client_and_model",
        lambda: (fake_client, "test-model"),
    )

    label, _desc = image_vision.classify_with_vision(p, conn)

    row = conn.execute(
        "SELECT classification, visioned_at FROM inline_images WHERE sha256 = ?", (sha,)
    ).fetchone()
    conn.close()
    assert row[0] == "content"  # vision reclassified it
    assert row[1] is not None  # visioned_at populated by the vision pass
