"""The reset returns wrongly filed images to 'unclassified', and the image pass then describes them.

Both caches return early for any image that is not 'unclassified', so a wrong Stage 1 verdict
was permanent: a one-off image filed as a signature by sender frequency before the occurrence
floor, and a large screenshot filed noise/decode_failed before Stage 1 shared vision's pixel
limit. The reset takes only images whose file is still on disk, keeps a user's override, and
leaves alone a signature the current rule still files.
"""

import importlib.util
import random
from pathlib import Path

import pytest
from PIL import Image

from src.extract.image_classifier import refresh_signature_index, sha256_of_file
from src.extract.image_pipeline import run_backfill
from src.extract.image_reset import SIGNATURE_FLOOR_LANDED, apply_reset, find_misfiled
from src.store.schema import create_database

_SPEC = importlib.util.spec_from_file_location(
    "reset_misfiled_images",
    Path(__file__).resolve().parent.parent / "scripts" / "reset_misfiled_images.py",
)
assert _SPEC and _SPEC.loader
_SCRIPT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_SCRIPT)

BEFORE_FLOOR = "2026-07-01T10:00:00+00:00"
AFTER_FLOOR = "2026-09-15T10:00:00+00:00"


@pytest.fixture
def conn(tmp_path):
    c = create_database(str(tmp_path / "brain.db"))
    yield c
    c.close()


def _png(path: Path, seed: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pixels = random.Random(seed).randbytes(200 * 200 * 3)
    Image.frombytes("RGB", (200, 200), pixels).save(path)
    return path


_message_seq = iter(range(1, 10_000))


def _attach(conn, path: Path, sender: str) -> str:
    """An email from `sender` carrying `path` as an image attachment; returns its message id."""
    n = next(_message_seq)
    message_id = f"AAMk-{n}"
    email_id = conn.execute(
        "INSERT INTO emails (message_id, date_received, sender_address, subject, content)"
        " VALUES (?, '2026-07-01', ?, 's', 'body')",
        (message_id, sender),
    ).lastrowid
    conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size,"
        " file_path, exported_at, sha256) VALUES (?, ?, ?, 'image/png', 1, ?, '2026-07-01', ?)",
        (email_id, message_id, path.name, str(path), sha256_of_file(path)),
    )
    conn.commit()
    return message_id


def _filed(conn, sha, classification, method, classified_at=BEFORE_FLOOR, overridden=0, w=200):
    conn.execute(
        "INSERT INTO inline_images (sha256, width, height, bytes, classification,"
        " classification_method, classified_at, user_overridden)"
        " VALUES (?, ?, ?, 120000, ?, ?, ?, ?)",
        (sha, w, w, classification, method, classified_at, overridden),
    )
    conn.commit()


def _classification(conn, sha):
    return conn.execute(
        "SELECT classification FROM inline_images WHERE sha256 = ?", (sha,)
    ).fetchone()[0]


def _seen(conn, sha, message_id, sender):
    conn.execute(
        "INSERT INTO inline_image_occurrences VALUES (?, ?, ?, 0.5)", (sha, message_id, sender)
    )
    conn.commit()


def _one_off_signature(conn, tmp_path, seed=1, sender="thin@example.com", **filed):
    """An image seen once, filed as a signature by frequency: 1 of the sender's 10 images."""
    img = _png(tmp_path / f"m{seed}" / "image001.png", seed)
    sha = sha256_of_file(img)
    _filed(conn, sha, "signature", "frequency", **filed)
    _seen(conn, sha, _attach(conn, img, sender), sender)
    for i in range(9):
        other = f"{seed:02d}other{i:02d}".ljust(64, "0")
        _filed(conn, other, "content", "vision_llm")
        _seen(conn, other, f"AAMk-other-{seed}-{i}", sender)
    refresh_signature_index(conn, sender)
    return img, sha


def test_a_one_off_filed_as_a_signature_before_the_floor_is_reset(conn, tmp_path):
    _img, sha = _one_off_signature(conn, tmp_path)

    plan = find_misfiled(conn)

    assert plan.frequency == [sha]
    assert plan.seen_under_floor == 1
    assert apply_reset(conn, plan) == 1
    row = conn.execute(
        "SELECT classification, classification_method FROM inline_images WHERE sha256 = ?",
        (sha,),
    ).fetchone()
    assert tuple(row) == ("unclassified", "reset_frequency")


def test_the_image_pass_describes_a_reset_image(conn, tmp_path, monkeypatch):
    _img, sha = _one_off_signature(conn, tmp_path)
    described = []

    def fake_vision(img_path, c):
        described.append(sha256_of_file(img_path))
        c.execute(
            "UPDATE inline_images SET classification = 'content', classification_method ="
            " 'vision_llm', vision_description = 'a bar chart', visioned_at = '2026-10-07'"
            " WHERE sha256 = ?",
            (sha256_of_file(img_path),),
        )
        c.commit()
        return "content", "a bar chart"

    monkeypatch.setattr("src.extract.image_vision.classify_with_vision", fake_vision)
    run_backfill(conn, limit=None, unprocessed_only=True)
    assert described == []  # filed as a signature, the pass had nothing to do with it

    apply_reset(conn, find_misfiled(conn))
    run_backfill(conn, limit=None, unprocessed_only=True)

    assert described == [sha]
    assert _classification(conn, sha) == "content"


def test_a_signature_the_current_rule_still_files_is_kept(conn, tmp_path):
    img = _png(tmp_path / "logo" / "logo.png", 7)
    sha = sha256_of_file(img)
    _filed(conn, sha, "signature", "frequency")
    for _ in range(3):  # 3 of the sender's 4 images: over both the floor and the ratio
        _seen(conn, sha, _attach(conn, img, "boss@example.com"), "boss@example.com")
    _filed(conn, "f" * 64, "content", "vision_llm")
    _seen(conn, "f" * 64, "AAMk-x", "boss@example.com")
    refresh_signature_index(conn, "boss@example.com")

    assert find_misfiled(conn).frequency == []


def test_an_image_recurring_across_senders_is_reset(conn, tmp_path):
    # A chart forwarded around a thread: three senders, once each. No sender's row
    # qualifies, so Stage 1 today would pass it on to vision.
    img = _png(tmp_path / "fwd" / "chart.png", 8)
    sha = sha256_of_file(img)
    _filed(conn, sha, "signature", "frequency")
    for sender in ("a@example.com", "b@example.com", "c@example.com"):
        _seen(conn, sha, _attach(conn, img, sender), sender)
        refresh_signature_index(conn, sender)

    plan = find_misfiled(conn)

    assert plan.frequency == [sha]
    assert plan.seen_under_floor == 0
    assert plan.attachment_rows == 3


def test_an_image_whose_file_is_gone_is_left_alone(conn, tmp_path):
    img, sha = _one_off_signature(conn, tmp_path)
    img.unlink()

    plan = find_misfiled(conn)

    assert plan.frequency == []
    assert plan.frequency_no_file == 1


def test_a_user_override_is_kept(conn, tmp_path):
    _one_off_signature(conn, tmp_path, overridden=1)

    assert find_misfiled(conn).frequency == []


def test_a_frequency_verdict_filed_after_the_floor_is_kept(conn, tmp_path):
    assert AFTER_FLOOR > SIGNATURE_FLOOR_LANDED
    _one_off_signature(conn, tmp_path, classified_at=AFTER_FLOOR)

    assert find_misfiled(conn).frequency == []


def test_an_image_the_pass_reached_in_the_meantime_is_not_reset(conn, tmp_path):
    _img, sha = _one_off_signature(conn, tmp_path)
    plan = find_misfiled(conn)
    conn.execute(
        "UPDATE inline_images SET classification = 'content', classification_method ="
        " 'vision_llm', vision_description = 'x' WHERE sha256 = ?",
        (sha,),
    )
    conn.commit()

    assert apply_reset(conn, plan) == 0
    assert _classification(conn, sha) == "content"


def test_a_decode_failure_whose_file_opens_now_is_reset(conn, tmp_path):
    giant = tmp_path / "giant" / "report.png"
    giant.parent.mkdir()
    # 200 M px: refused by Pillow's default, admitted by the shared limit.
    Image.new("1", (20_000, 10_000)).save(giant)
    broken = tmp_path / "broken" / "image002.png"
    broken.parent.mkdir()
    broken.write_bytes(b"not an image at all" * 500)
    for path in (giant, broken):
        sha = sha256_of_file(path)
        _filed(conn, sha, "noise", "decode_failed", w=0)
        _attach(conn, path, "a@example.com")

    plan = find_misfiled(conn)

    assert plan.decode_failed == [sha256_of_file(giant)]
    assert plan.undecodable == 1
    assert apply_reset(conn, plan) == 1
    method = conn.execute(
        "SELECT classification_method FROM inline_images WHERE sha256 = ?",
        (sha256_of_file(giant),),
    ).fetchone()[0]
    assert method == "reset_decode_failed"


def test_the_script_reports_a_dry_run_and_changes_nothing(conn, tmp_path, capsys):
    _img, sha = _one_off_signature(conn, tmp_path)
    db = conn.execute("PRAGMA database_list").fetchone()[2]

    assert _SCRIPT.main(["--db", db]) == 0

    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "to reset (a file on disk)" in out
    assert _classification(conn, sha) == "signature"


def test_the_script_applies_and_refuses_a_replica(conn, tmp_path, monkeypatch, capsys):
    _img, sha = _one_off_signature(conn, tmp_path)
    db = conn.execute("PRAGMA database_list").fetchone()[2]

    monkeypatch.setenv("BRAIN_ROLE", "replica")
    assert _SCRIPT.main(["--db", db, "--apply"]) == 2
    monkeypatch.setenv("BRAIN_ROLE", "producer")
    assert _SCRIPT.main(["--db", db, "--apply"]) == 0

    assert "Reset 1 images" in capsys.readouterr().out
    assert _classification(conn, sha) == "unclassified"
