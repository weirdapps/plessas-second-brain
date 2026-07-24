import sqlite3
from pathlib import Path

from PIL import Image

from src.extract.image_classifier import (
    Classification,
    classify_stage1,
    sha256_of_file,
)
from src.store.schema import create_database, run_migrations


def _setup_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "test.db"
    conn = create_database(str(db_path))
    run_migrations(conn)
    return conn


def _make_image(tmp_path: Path, name: str, w: int, h: int, color: str = "red") -> Path:
    p = tmp_path / name
    img = Image.new("RGB", (w, h), color)
    img.save(p, "PNG")
    return p


def test_tiny_image_is_noise(tmp_path):
    img = _make_image(tmp_path, "tiny.png", 50, 50)
    db = _setup_db(tmp_path)
    result = classify_stage1(img, sender="x@y.com", position=0.1, conn=db)
    assert result == Classification.NOISE


def test_normal_image_passes_stage1(tmp_path):
    img = _make_image(tmp_path, "norm.png", 800, 600)
    # padding to ensure > 5KB
    with open(img, "ab") as f:
        f.write(b"\x00" * 6000)
    db = _setup_db(tmp_path)
    result = classify_stage1(img, sender="x@y.com", position=0.3, conn=db)
    assert result == Classification.UNCLASSIFIED  # passes stage 1, awaits stage 3


def test_recurring_image_marked_as_signature(tmp_path):
    # Logo-sized image but ≥ 100px on both axes so it passes Stage 1a.
    img = _make_image(tmp_path, "logo.png", 200, 120)
    with open(img, "ab") as f:
        f.write(b"\x00" * 10000)
    sha = sha256_of_file(img)
    db = _setup_db(tmp_path)

    # Simulate this image appearing in 6 of 100 messages from the sender.
    # inline_image_occurrences has FK to inline_images(sha256), so seed parent rows first.
    db.execute(
        """INSERT INTO inline_images
           (sha256, width, height, bytes, classification, classification_method, classified_at)
           VALUES (?, 200, 120, 10000, 'unclassified', 'seed', '2026-04-22T00:00:00Z')""",
        (sha,),
    )
    for i in range(100):
        if i >= 6:
            other = f"otherhash{i:03d}".ljust(64, "0")
            db.execute(
                """INSERT INTO inline_images
                   (sha256, width, height, bytes, classification, classification_method, classified_at)
                   VALUES (?, 200, 120, 10000, 'unclassified', 'seed', '2026-04-22T00:00:00Z')""",
                (other,),
            )
            db.execute(
                "INSERT INTO inline_image_occurrences VALUES (?, ?, ?, ?)",
                (other, f"msg{i}", "boss@example.com", 0.9),
            )
        else:
            db.execute(
                "INSERT INTO inline_image_occurrences VALUES (?, ?, ?, ?)",
                (sha, f"msg{i}", "boss@example.com", 0.9),
            )
    db.commit()

    # Refresh signature index
    from src.extract.image_classifier import refresh_signature_index

    refresh_signature_index(db, sender="boss@example.com")

    result = classify_stage1(img, sender="boss@example.com", position=0.5, conn=db)
    assert result == Classification.SIGNATURE
