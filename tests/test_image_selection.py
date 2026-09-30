"""The image pass finishes every image.

Each image of a message gets its own turn (the old skip keyed on the message, so once one image
of a message was processed the others never were). An image whose email row is gone is
processed with a blank sender. A failed description counts an attempt; after
VISION_ATTEMPTS_LIMIT the image counts as done, and the sweep may delete it. A known image in a
new message is still taken once, so its occurrence is recorded for image search and signature
counts.
"""

import random

import pytest
from PIL import Image

from src.extract.image_classifier import classify_stage1, sha256_of_file
from src.extract.image_pipeline import run_backfill
from src.store.file_sweep import VISION_ATTEMPTS_LIMIT
from src.store.schema import create_database


def _png(path, seed):
    path.parent.mkdir(parents=True, exist_ok=True)
    pixels = random.Random(seed).randbytes(200 * 200 * 3)
    Image.frombytes("RGB", (200, 200), pixels).save(path)
    return path


@pytest.fixture
def conn(tmp_path):
    c = create_database(str(tmp_path / "brain.db"))
    yield c
    c.close()


def _email(conn, message_id="AAMk-1"):
    conn.execute(
        "INSERT INTO emails (message_id, date_received, sender_address, subject, content)"
        " VALUES (?, '2026-09-01', 'a@example.com', 's', 'body')",
        (message_id,),
    )
    return conn.execute("SELECT id FROM emails WHERE message_id = ?", (message_id,)).fetchone()[0]


def _image(conn, email_id, message_id, path):
    conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size,"
        " file_path, exported_at, sha256) VALUES (?, ?, ?, 'image/png', ?, ?, '2026-09-01', ?)",
        (email_id, message_id, path.name, path.stat().st_size, str(path), sha256_of_file(path)),
    )
    conn.commit()


def _vision(monkeypatch, fail=False):
    seen = []

    def fake(img_path, conn):
        seen.append(img_path.name)
        if fail:
            raise RuntimeError("model unavailable")
        conn.execute(
            "UPDATE inline_images SET classification = 'content', vision_description = 'a chart',"
            " visioned_at = '2026-09-01' WHERE sha256 = ?",
            (sha256_of_file(img_path),),
        )
        return "content", "a chart"

    monkeypatch.setattr("src.extract.image_vision.classify_with_vision", fake)
    return seen


def test_every_image_of_a_message_is_processed(conn, tmp_path, monkeypatch):
    e = _email(conn)
    _image(conn, e, "AAMk-1", _png(tmp_path / "a" / "one.png", 1))
    _image(conn, e, "AAMk-1", _png(tmp_path / "a" / "two.png", 2))
    seen = _vision(monkeypatch)

    run_backfill(conn, limit=1, unprocessed_only=True)
    run_backfill(conn, limit=1, unprocessed_only=True)

    assert sorted(seen) == ["one.png", "two.png"]


def test_an_image_whose_email_is_gone_is_processed(conn, tmp_path, monkeypatch):
    conn.execute("PRAGMA foreign_keys = OFF")
    _image(conn, 999, "AAMk-gone", _png(tmp_path / "g" / "orphan.png", 3))
    seen = _vision(monkeypatch)

    stats = run_backfill(conn, unprocessed_only=True)

    assert seen == ["orphan.png"]
    assert stats["classified"] == 1


def test_a_failed_description_counts_and_stops_after_the_limit(conn, tmp_path, monkeypatch):
    _image(conn, _email(conn), "AAMk-1", _png(tmp_path / "a" / "stubborn.png", 4))
    seen = _vision(monkeypatch, fail=True)

    for _ in range(VISION_ATTEMPTS_LIMIT + 1):
        run_backfill(conn, unprocessed_only=True)

    assert len(seen) == VISION_ATTEMPTS_LIMIT
    assert conn.execute("SELECT vision_attempts FROM inline_images").fetchone()[0] == (
        VISION_ATTEMPTS_LIMIT
    )


def test_stage1_again_keeps_the_attempts_and_the_occurrences(conn, tmp_path, monkeypatch):
    img = _png(tmp_path / "a" / "again.png", 5)
    _image(conn, _email(conn), "AAMk-1", img)
    _vision(monkeypatch, fail=True)
    run_backfill(conn, unprocessed_only=True)

    classify_stage1(img, "a@example.com", 0.5, conn)

    assert conn.execute("SELECT vision_attempts FROM inline_images").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM inline_image_occurrences").fetchone()[0] == 1


def test_a_known_image_in_a_new_message_is_recorded_without_vision(conn, tmp_path, monkeypatch):
    img = _png(tmp_path / "a" / "chart.png", 6)
    _image(conn, _email(conn, "AAMk-1"), "AAMk-1", img)
    seen = _vision(monkeypatch)
    run_backfill(conn, unprocessed_only=True)

    _image(conn, _email(conn, "AAMk-2"), "AAMk-2", img)
    run_backfill(conn, unprocessed_only=True)

    assert seen == ["chart.png"], "described once"
    messages = {r[0] for r in conn.execute("SELECT message_id FROM inline_image_occurrences")}
    assert messages == {"AAMk-1", "AAMk-2"}


def test_an_image_without_a_hash_gets_one_when_processed(conn, tmp_path, monkeypatch):
    """The owed rule keys on the hash. A row registered before hashes were recorded is taken
    once, hashed on the way, and then known to be done."""
    img = _png(tmp_path / "a" / "nohash.png", 7)
    conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size,"
        " file_path, exported_at) VALUES (?, 'AAMk-1', 'nohash.png', 'image/png', 1, ?,"
        " '2026-09-01')",
        (_email(conn), str(img)),
    )
    conn.commit()
    _vision(monkeypatch)

    run_backfill(conn, unprocessed_only=True)

    assert conn.execute("SELECT sha256 FROM attachments").fetchone()[0] == sha256_of_file(img)
    assert run_backfill(conn, unprocessed_only=True)["scanned"] == 0
