"""Content images are transcribed, and their text is searchable and embedded as attachment text.

A content image is filed with a one-sentence description, which lost the figures, labels and
table cells of a chart or a screenshot. The transcription asks the model for that text, bounded,
and stores it beside the description. Both reach search through the image's attachment rows:
the rows Phase 1 left without text (OCR found too little or failed) take extraction_method
'vision', the description as summary (what build_index embeds) and the description plus the
transcription as text (what attachment search reads). A row holding text read from the file is
never overwritten, and no row is inserted.
"""

import random
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image

from src.extract.image_classifier import sha256_of_file
from src.extract.image_pipeline import project_vision_text, run_backfill, run_transcription
from src.extract.image_vision import (
    NO_TEXT,
    TRANSCRIBE_MAX_TOKENS,
    VisionDecodeTooLarge,
    transcribe_image,
)
from src.store.file_sweep import VISION_ATTEMPTS_LIMIT
from src.store.schema import create_database, migrate_add_image_transcription

OCR_INSUFFICIENT = "OCR returned insufficient text"
DESCRIPTION = "Bar chart of quarterly card spend by customer segment"
TRANSCRIPTION = "Πωλήσεις καρτών 2026\nSegment | Q1 | Q2\nPremium | 1,250 | 1,410"


@pytest.fixture
def conn(tmp_path):
    c = create_database(str(tmp_path / "brain.db"))
    yield c
    c.close()


class _Reply:
    stop_reason = "end_turn"

    def __init__(self, text):
        self.content = [type("Block", (), {"text": text})()]


def _model(monkeypatch, *answers):
    """complete() answering each call with the next answer: a text, or an exception to raise."""
    calls = []
    queue = list(answers)

    def fake(**kwargs):
        calls.append(kwargs)
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, BaseException):
            raise answer
        return _Reply(answer)

    monkeypatch.setattr("src.extract.claude_extract.complete", fake)
    return calls


_seq = iter(range(1, 100_000))


def _image(conn, tmp_path, *, content=True, text=None, row=True, file=True, seed=None):
    """An emailed image, its inline_images row and, unless row=False, its attachment_content row.

    text=None gives the row OCR left without text; a string gives a row OCR read.
    """
    n = next(_seq)
    path = tmp_path / f"m{n}" / "image001.png"
    path.parent.mkdir(parents=True)
    pixels = random.Random(seed if seed is not None else n).randbytes(200 * 200 * 3)
    Image.frombytes("RGB", (200, 200), pixels).save(path)
    sha = sha256_of_file(path)
    email_id = conn.execute(
        "INSERT INTO emails (message_id, date_received, sender_address, subject, content)"
        " VALUES (?, '2026-09-01', 'a@example.com', 'Q3 figures', 'see below')",
        (f"AAMk-{n}",),
    ).lastrowid
    att_id = conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size,"
        " file_path, exported_at, sha256) VALUES (?, ?, 'image001.png', 'image/png', 1, ?,"
        " '2026-09-01', ?)",
        (email_id, f"AAMk-{n}", str(path), sha),
    ).lastrowid
    if row:
        conn.execute(
            "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method,"
            " extraction_status, extraction_error, extracted_at, llm_status)"
            " VALUES (?, ?, 'ocr', ?, ?, '2026-09-01T10:00:00', 'pending')",
            (att_id, text, "extracted" if text else "skipped", None if text else OCR_INSUFFICIENT),
        )
    conn.execute(
        "INSERT OR IGNORE INTO inline_images (sha256, width, height, bytes, classification,"
        " classification_method, classified_at, vision_description, visioned_at)"
        " VALUES (?, 200, 200, 120000, ?, 'vision_llm', '2026-09-01', ?, ?)",
        (
            sha,
            "content" if content else "signature",
            DESCRIPTION if content else "a logo",
            f"2026-09-{n % 28 + 1:02d}T10:00:00+00:00",
        ),
    )
    conn.commit()
    if not file:
        path.unlink()
    return sha, att_id


def _content_row(conn, att_id):
    return conn.execute(
        "SELECT extracted_text, extraction_method, extraction_status, extraction_error, summary,"
        " llm_status FROM attachment_content WHERE attachment_id = ?",
        (att_id,),
    ).fetchone()


def _image_row(conn, sha):
    return conn.execute(
        "SELECT vision_transcription, transcribed_at, transcription_attempts"
        " FROM inline_images WHERE sha256 = ?",
        (sha,),
    ).fetchone()


# --- schema v31 ------------------------------------------------------------------------------


def _cols(conn):
    return {r[1] for r in conn.execute("PRAGMA table_info(inline_images)")}


TRANSCRIPTION_COLUMNS = {"vision_transcription", "transcribed_at", "transcription_attempts"}


def test_v31_gives_images_a_transcription_beside_the_description(conn):
    assert TRANSCRIPTION_COLUMNS <= _cols(conn)
    migrate_add_image_transcription(conn)  # idempotent
    for column in TRANSCRIPTION_COLUMNS:
        conn.execute(f"ALTER TABLE inline_images DROP COLUMN {column}")
    conn.commit()

    migrate_add_image_transcription(conn)

    assert TRANSCRIPTION_COLUMNS <= _cols(conn)


def test_v31_loses_a_race_cleanly(tmp_path):
    """Units start together after a deploy: the loser reads the columns as missing, then
    finds them there when it ALTERs. That must be a no-op, not a failed run."""
    import sqlite3

    class StaleRead(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.startswith("PRAGMA table_info(inline_images)"):
                return super().execute("SELECT 0, 'sha256' UNION ALL SELECT 1, 'width'")
            return super().execute(sql, *args)

    path = tmp_path / "raced.db"
    create_database(str(path)).close()
    racer = sqlite3.connect(path, factory=StaleRead)

    migrate_add_image_transcription(racer)  # must not raise

    racer.close()


# --- the transcription call ------------------------------------------------------------------


def test_the_transcription_asks_for_the_visible_text_within_a_bounded_budget(
    conn, tmp_path, monkeypatch
):
    calls = _model(monkeypatch, TRANSCRIPTION)
    img = tmp_path / "chart.png"
    Image.new("RGB", (300, 200), "white").save(img)

    assert transcribe_image(img) == TRANSCRIPTION
    assert 1_000 <= TRANSCRIBE_MAX_TOKENS <= 1_500
    assert calls[0]["max_tokens"] == TRANSCRIBE_MAX_TOKENS
    image_block, prompt = calls[0]["messages"][0]["content"]
    assert image_block["type"] == "image"
    for asked in ("labels", "table", "number"):
        assert asked in prompt["text"].lower()


def test_an_image_without_text_transcribes_to_nothing(tmp_path, monkeypatch):
    _model(monkeypatch, f" {NO_TEXT} ")
    img = tmp_path / "photo.png"
    Image.new("RGB", (300, 200), "green").save(img)

    assert transcribe_image(img) == ""


def test_a_credential_shown_in_a_screenshot_is_redacted(tmp_path, monkeypatch):
    key = "sk-ant-api03-" + "aB-c_D1e2F" * 6
    _model(monkeypatch, f"export ANTHROPIC_API_KEY={key}")
    img = tmp_path / "terminal.png"
    Image.new("RGB", (300, 200), "black").save(img)

    text = transcribe_image(img)

    assert key not in text
    assert "[REDACTED:anthropic-key]" in text


# --- the transcription pass ------------------------------------------------------------------


def test_a_content_image_without_text_is_transcribed_and_written_to_its_rows(
    conn, tmp_path, monkeypatch
):
    sha, att_id = _image(conn, tmp_path)
    calls = _model(monkeypatch, TRANSCRIPTION)

    stats = run_transcription(conn)
    again = run_transcription(conn)

    assert (stats["transcribed"], stats["failed"], stats["projected"]) == (1, 0, 1)
    assert len(calls) == 1 and again["candidates"] == 0
    transcription, transcribed_at, attempts = _image_row(conn, sha)
    assert (transcription, attempts) == (TRANSCRIPTION, 0)
    assert transcribed_at
    text, method, status, error, summary, llm_status = _content_row(conn, att_id)
    assert text == f"{DESCRIPTION}\n\n{TRANSCRIPTION}"
    assert (method, status, error, summary, llm_status) == (
        "vision",
        "extracted",
        None,
        DESCRIPTION,
        "extracted",
    )


def test_an_image_its_ocr_already_read_is_left_to_the_ocr(conn, tmp_path, monkeypatch):
    _sha, att_id = _image(conn, tmp_path, text="Premium 1,250 1,410 Q1 Q2 card spend")
    calls = _model(monkeypatch, TRANSCRIPTION)

    stats = run_transcription(conn)

    assert calls == [] and stats["candidates"] == 0
    assert _content_row(conn, att_id)[:2] == ("Premium 1,250 1,410 Q1 Q2 card spend", "ocr")


def test_only_content_images_are_transcribed(conn, tmp_path, monkeypatch):
    _sha, att_id = _image(conn, tmp_path, content=False)
    calls = _model(monkeypatch, TRANSCRIPTION)

    run_transcription(conn)

    assert calls == []
    assert _content_row(conn, att_id)[1] == "ocr"


def test_an_image_whose_file_is_gone_does_not_use_up_the_limit(conn, tmp_path, monkeypatch):
    _image(conn, tmp_path, file=False)
    sha, _ = _image(conn, tmp_path)
    _model(monkeypatch, TRANSCRIPTION)

    stats = run_transcription(conn, limit=1)

    assert (stats["missing"], stats["transcribed"]) == (1, 1)
    assert _image_row(conn, sha)[0] == TRANSCRIPTION


def test_a_failing_image_is_given_up_after_the_attempts_limit(conn, tmp_path, monkeypatch):
    sha, _ = _image(conn, tmp_path)
    calls = _model(monkeypatch, ValueError("vision response carried no text block"))

    for _ in range(VISION_ATTEMPTS_LIMIT + 1):
        run_transcription(conn)

    assert len(calls) == VISION_ATTEMPTS_LIMIT
    assert _image_row(conn, sha)[2] == VISION_ATTEMPTS_LIMIT


@pytest.mark.parametrize("failure", ["APIConnectionError", "RateLimitError", "too large"])
def test_a_failure_of_the_service_or_the_host_costs_the_image_no_attempt(
    conn, tmp_path, monkeypatch, failure
):
    from src.extract import policy_bridge

    if failure == "too large":
        error: BaseException = VisionDecodeTooLarge("deferring until the host has room")
    else:
        cls = getattr(policy_bridge.anthropic, failure)
        error = cls.__new__(cls)
    sha, _ = _image(conn, tmp_path)
    _model(monkeypatch, error)

    stats = run_transcription(conn)

    assert stats["failed"] == 1
    assert _image_row(conn, sha)[2] == 0


def test_a_dry_run_counts_and_calls_nothing(conn, tmp_path, monkeypatch):
    _image(conn, tmp_path)
    _image(conn, tmp_path, file=False)
    calls = _model(monkeypatch, TRANSCRIPTION)

    stats = run_transcription(conn, dry_run=True)

    assert (stats["candidates"], stats["missing"], stats["transcribed"]) == (1, 1, 0)
    assert calls == []


def test_workers_transcribe_on_their_own_connections(conn, tmp_path, monkeypatch):
    shas = [_image(conn, tmp_path)[0] for _ in range(3)]
    _model(monkeypatch, TRANSCRIPTION)

    stats = run_transcription(conn, workers=2)

    assert stats["transcribed"] == 3
    assert all(_image_row(conn, sha)[0] == TRANSCRIPTION for sha in shas)


# --- writing vision text into attachment rows ------------------------------------------------


def test_the_projection_never_inserts_a_row_or_overwrites_text_read_from_the_file(conn, tmp_path):
    seed = 4242
    _sha, no_row = _image(conn, tmp_path, row=False, seed=seed)
    _sha2, read = _image(conn, tmp_path, text="text OCR read", seed=seed)  # the same bytes

    assert project_vision_text(conn) == 0
    assert _content_row(conn, no_row) is None
    assert _content_row(conn, read)[:2] == ("text OCR read", "ocr")


def test_the_projection_writes_each_row_once(conn, tmp_path):
    _image(conn, tmp_path)

    assert project_vision_text(conn) == 1
    assert project_vision_text(conn) == 0


def test_an_image_described_with_nothing_keeps_just_its_text(conn, tmp_path):
    # parse_vision_response files "CONTENT:" with nothing after it as content, described "".
    sha, att_id = _image(conn, tmp_path)
    conn.execute(
        "UPDATE inline_images SET vision_description = '', vision_transcription = ?"
        " WHERE sha256 = ?",
        (TRANSCRIPTION, sha),
    )
    conn.commit()

    project_vision_text(conn)

    assert _content_row(conn, att_id)[0] == TRANSCRIPTION


def test_the_image_pass_makes_a_new_description_searchable(conn, tmp_path, monkeypatch):
    # The hourly Step 8: classification, then the description into the image's text-free row.
    sha, att_id = _image(conn, tmp_path)
    conn.execute(
        "UPDATE inline_images SET classification = 'unclassified', vision_description = NULL"
        " WHERE sha256 = ?",
        (sha,),
    )
    conn.commit()

    def fake_vision(img_path, c):
        c.execute(
            "UPDATE inline_images SET classification = 'content', vision_description = ?,"
            " visioned_at = '2026-10-07' WHERE sha256 = ?",
            (DESCRIPTION, sha256_of_file(img_path)),
        )
        c.commit()
        return "content", DESCRIPTION

    monkeypatch.setattr("src.extract.image_vision.classify_with_vision", fake_vision)

    stats = run_backfill(conn, limit=None, unprocessed_only=True)

    assert stats["projected"] == 1
    assert _content_row(conn, att_id)[:2] == (DESCRIPTION, "vision")


# --- search and embeddings -------------------------------------------------------------------


def test_attachment_search_finds_an_image_by_its_description_and_its_text(
    conn, tmp_path, monkeypatch
):
    from src import mcp_server
    from src.store.schema import get_connection

    _sha, att_id = _image(conn, tmp_path)
    _model(monkeypatch, TRANSCRIPTION)
    run_transcription(conn)

    # The tool closes its connection after every call, as it does in production.
    with patch("src.mcp_server._get_conn", side_effect=lambda: get_connection(str(_db(conn)))):
        by_description = mcp_server.search_attachments("card spend segment")
        # Greek from the transcription, typed without accents.
        by_text = mcp_server.search_attachments("πωλησεις καρτων")

    for hits in (by_description, by_text):
        assert [h["attachment_id"] for h in hits] == [att_id]
        assert hits[0]["summary"] == DESCRIPTION
        assert "partial_match" not in hits[0]


def test_build_index_embeds_the_image_description(conn, tmp_path, monkeypatch):
    import src.store.embeddings as emb

    _sha, att_id = _image(conn, tmp_path)
    project_vision_text(conn)
    ac_id = conn.execute(
        "SELECT id FROM attachment_content WHERE attachment_id = ?", (att_id,)
    ).fetchone()[0]
    embedded = []

    def fake_embed(texts, client=None):
        embedded.extend(texts)
        return np.zeros((len(texts), 3), dtype=np.float32)

    monkeypatch.setattr(emb, "EMBEDDINGS_FILE", tmp_path / "embeddings.npz")
    monkeypatch.setattr(emb, "generate_embeddings", fake_embed)
    monkeypatch.setattr(emb, "_get_client", lambda: None)

    assert emb.build_index(conn) == 1

    assert embedded == [f"[Attachment: image001.png] {DESCRIPTION}"]
    assert np.load(tmp_path / "embeddings.npz")["ids"].tolist() == [-ac_id]


# --- the commands ----------------------------------------------------------------------------


def _db(conn) -> Path:
    return Path(conn.execute("PRAGMA database_list").fetchone()[2])


def test_transcribe_images_counts_on_a_dry_run_then_transcribes(
    conn, tmp_path, monkeypatch, capsys
):
    from src.cli import cmd_transcribe_images

    sha, _ = _image(conn, tmp_path)
    calls = _model(monkeypatch, TRANSCRIPTION)
    args = {"db": _db(conn), "limit": 0, "workers": 1, "deadline_s": None}

    assert cmd_transcribe_images(Namespace(**args, dry_run=True)) == 0
    assert calls == [] and "Would transcribe: 1" in capsys.readouterr().out

    assert cmd_transcribe_images(Namespace(**args, dry_run=False)) == 0
    assert _image_row(conn, sha)[0] == TRANSCRIPTION


def test_transcribe_images_fails_when_every_call_failed(conn, tmp_path, monkeypatch):
    from src.cli import cmd_transcribe_images

    _image(conn, tmp_path)
    _model(monkeypatch, ValueError("no text block"))

    rc = cmd_transcribe_images(
        Namespace(db=_db(conn), limit=0, workers=1, deadline_s=None, dry_run=False)
    )

    assert rc == 1


def test_the_nightly_image_pass_transcribes_new_content_images(conn, tmp_path, monkeypatch):
    from src.cli import cmd_process_images

    sha, att_id = _image(conn, tmp_path)
    _model(monkeypatch, TRANSCRIPTION)

    cmd_process_images(
        Namespace(
            db=_db(conn),
            since=None,
            limit=500,
            no_vision=False,
            dry_run=False,
            reprocess=False,
            workers=1,
        )
    )

    assert _image_row(conn, sha)[0] == TRANSCRIPTION
    assert _content_row(conn, att_id)[0] == f"{DESCRIPTION}\n\n{TRANSCRIPTION}"
