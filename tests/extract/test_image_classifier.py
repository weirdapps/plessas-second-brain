import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from src.extract.image_classifier import (
    IMAGE_PIXEL_LIMIT,
    MIN_BYTES,
    MIN_SIGNATURE_OCCURRENCES,
    Classification,
    admit_large_images,
    classify_stage1,
    is_known_signature,
    sha256_of_file,
)
from src.store.schema import create_database, run_migrations

REPO = Path(__file__).resolve().parent.parent.parent


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


# Stage 1 alone, in a fresh interpreter, as the loader hook runs it: no image_vision import.
_STAGE1_ALONE = """
import sys
from pathlib import Path

from PIL import Image

default = Image.MAX_IMAGE_PIXELS
from src.extract.image_classifier import classify_stage1
from src.store.schema import create_database

assert "src.extract.image_vision" not in sys.modules
conn = create_database(sys.argv[2])
label = classify_stage1(Path(sys.argv[1]), "a@example.com", 0.5, conn)
row = conn.execute("SELECT width, height, classification_method FROM inline_images").fetchone()
print(label.value, row[0], row[1], row[2], Image.MAX_IMAGE_PIXELS == default)
"""


def test_a_large_screenshot_is_not_noise_where_vision_was_never_imported(tmp_path):
    # A full-page report screenshot runs to hundreds of megapixels. Pillow's default guard
    # refuses anything over 2 x 89,478,485 px, and Stage 1 filed five such screenshots as
    # noise/decode_failed, so vision never saw them, though vision admits up to 550 M px.
    big = tmp_path / "report.png"
    # 200 M px, between the two limits. Mode "1" keeps it ~25 MB in memory and ~25 KB on disk,
    # and Stage 1 reads only the header.
    Image.new("1", (20_000, 10_000)).save(big)
    assert big.stat().st_size >= MIN_BYTES

    out = subprocess.run(
        [sys.executable, "-c", _STAGE1_ALONE, str(big), str(tmp_path / "brain.db")],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )

    # Classified from its header, and the process-wide guard is Pillow's own afterwards.
    assert out.stdout.split() == ["unclassified", "20000", "10000", "stage1_passed", "True"]


# Importing the image stages, or Phase 1 (which reaches image_classifier through
# file_hashes), must leave Pillow's guard for the process as Pillow set it: OCR and every
# other caller keep the default, and only the pipeline's own opens are admitted higher.
_IMPORT_ONLY = """
from PIL import Image

default = Image.MAX_IMAGE_PIXELS
import src.extract.attachment_pipeline
import src.extract.image_pipeline
import src.extract.image_vision

print(Image.MAX_IMAGE_PIXELS == default)
"""


def test_importing_the_image_stages_leaves_the_process_guard_alone(tmp_path):
    out = subprocess.run(
        [sys.executable, "-c", _IMPORT_ONLY],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )

    assert out.stdout.strip() == "True"


def test_the_pipeline_limit_holds_only_inside_its_scope(monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1_000)

    with admit_large_images():
        assert Image.MAX_IMAGE_PIXELS == IMAGE_PIXEL_LIMIT
    assert Image.MAX_IMAGE_PIXELS == 1_000

    with pytest.raises(ValueError), admit_large_images():
        raise ValueError("the open failed")
    assert Image.MAX_IMAGE_PIXELS == 1_000


def test_the_scope_never_lowers_a_higher_or_absent_limit(monkeypatch):
    for value in (IMAGE_PIXEL_LIMIT * 4, None):
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", value)
        with admit_large_images():
            assert Image.MAX_IMAGE_PIXELS == value


def test_two_threads_cannot_interleave_their_scopes(monkeypatch):
    # Interleaved, the second scope would save the first one's raised value as "previous"
    # and put it back last, leaving the whole process raised.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1_000)
    inside, release, order = threading.Event(), threading.Event(), []

    def first():
        with admit_large_images():
            inside.set()
            release.wait(5)
            order.append("first out")

    def second():
        inside.wait(5)
        with admit_large_images():
            order.append("second in")

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for t in threads:
        t.start()
    time.sleep(0.05)  # the second thread would be inside by now if nothing held it out
    release.set()
    for t in threads:
        t.join(5)

    assert order == ["first out", "second in"]
    assert Image.MAX_IMAGE_PIXELS == 1_000
