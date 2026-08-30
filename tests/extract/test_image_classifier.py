import sqlite3
from pathlib import Path

from PIL import Image

from src.extract.image_classifier import (
    MIN_SIGNATURE_OCCURRENCES,
    Classification,
    classify_stage1,
    is_known_signature,
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


def _seed_index_row(
    conn: sqlite3.Connection, sender: str, sha: str, occurrence_count: int, sender_total: int
) -> None:
    """Write a sender_signature_index row directly, as refresh_signature_index would."""
    conn.execute(
        "INSERT INTO sender_signature_index VALUES (?, ?, ?, ?, ?)",
        (sender, sha, occurrence_count, sender_total, occurrence_count / sender_total),
    )
    conn.commit()


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


def test_one_off_image_from_a_thin_sender_is_not_a_signature(tmp_path):
    # A sender with only 19 inline images in the whole corpus makes a single
    # one-off image 1/19 = 5.3% of their history, clearing the 5% threshold.
    # 271 of 675 frequency-flagged images were exactly this: seen ONCE. They
    # are not signatures, and both caches return early for anything that isn't
    # 'unclassified', so a mislabel here is permanent.
    img = _make_image(tmp_path, "oneoff.png", 400, 300, color="blue")
    with open(img, "ab") as f:
        f.write(b"\x00" * 10000)
    sha = sha256_of_file(img)
    db = _setup_db(tmp_path)

    db.execute(
        """INSERT INTO inline_images
           (sha256, width, height, bytes, classification, classification_method, classified_at)
           VALUES (?, 400, 300, 10000, 'unclassified', 'seed', '2026-08-30T00:00:00Z')""",
        (sha,),
    )
    db.execute(
        "INSERT INTO inline_image_occurrences VALUES (?, ?, ?, ?)",
        (sha, "msg0", "thin@example.com", 0.5),
    )
    for i in range(1, 19):
        other = f"otherhash{i:03d}".ljust(64, "0")
        db.execute(
            """INSERT INTO inline_images
               (sha256, width, height, bytes, classification, classification_method, classified_at)
               VALUES (?, 400, 300, 10000, 'unclassified', 'seed', '2026-08-30T00:00:00Z')""",
            (other,),
        )
        db.execute(
            "INSERT INTO inline_image_occurrences VALUES (?, ?, ?, ?)",
            (other, f"msg{i}", "thin@example.com", 0.5),
        )
    db.commit()

    from src.extract.image_classifier import refresh_signature_index

    refresh_signature_index(db, sender="thin@example.com")
    indexed = db.execute(
        "SELECT occurrence_count, frequency FROM sender_signature_index WHERE sha256 = ?",
        (sha,),
    ).fetchone()
    assert indexed[0] == 1
    assert indexed[1] >= 0.05  # the frequency test on its own says "signature"

    result = classify_stage1(img, sender="thin@example.com", position=0.5, conn=db)
    assert result == Classification.UNCLASSIFIED


def test_repeated_image_above_threshold_is_still_a_signature(tmp_path):
    db = _setup_db(tmp_path)
    sha = "a" * 64
    _seed_index_row(db, "boss@example.com", sha, occurrence_count=6, sender_total=100)
    assert is_known_signature(db, "boss@example.com", sha) is True


def test_many_occurrences_below_threshold_are_not_a_signature(tmp_path):
    # A prolific sender: 50 copies is plenty of repetition, but 1% of their
    # traffic is not a signature. The occurrence floor must not weaken this.
    db = _setup_db(tmp_path)
    sha = "b" * 64
    _seed_index_row(db, "newsletter@example.com", sha, occurrence_count=50, sender_total=5000)
    assert is_known_signature(db, "newsletter@example.com", sha) is False


def test_occurrence_floor_boundary(tmp_path):
    db = _setup_db(tmp_path)
    below = "c" * 64
    at = "d" * 64
    # Both are far above the frequency threshold; only the count separates them.
    _seed_index_row(
        db,
        "thin@example.com",
        below,
        occurrence_count=MIN_SIGNATURE_OCCURRENCES - 1,
        sender_total=10,
    )
    _seed_index_row(
        db, "thin@example.com", at, occurrence_count=MIN_SIGNATURE_OCCURRENCES, sender_total=10
    )
    assert is_known_signature(db, "thin@example.com", below) is False
    assert is_known_signature(db, "thin@example.com", at) is True
