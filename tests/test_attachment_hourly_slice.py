"""The hourly sync's attachment slice (issue #151).

Phase 1 ran oldest first in a 90 s slice while one file can take 120 s (OCR, an archive), and
Phase 2 was scoped to the run's own registrations, which oldest-first Phase 1 had seldom reached.
A document mailed in the morning had no text and no summary until the 02:00 pass: 15-63% of a
day's attachments got their text more than six hours after download. The hourly slice now reads
the newest mail first and leaves images and zip archives to the nightly pass while one runs, and
Phase 2 starts with what Phase 1 has just read. The nightly pass keeps its order and reads every
kind. Nothing here calls a model or reads a real file: Phase 1's reader and Phase 2's model call
are fakes that move a fake clock.
"""

import sqlite3
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from src.extract import attachment_pipeline
from src.extract.attachment_pipeline import run_phase1, run_phase2

PDF = "application/pdf"


class _Clock:
    """time.monotonic for the pipeline: it moves only when a fake reader or model says so."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(
        attachment_pipeline,
        "time",
        types.SimpleNamespace(monotonic=c.monotonic, sleep=lambda _seconds: None),
    )
    return c


def _store(tmp_path, *attachments):
    """A store with one email per attachment, registered in the order given, so the ids follow
    the arguments. Each attachment is (file name, its email's date_received, mime type)."""
    from src.store.schema import create_database

    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    for n, (name, received, mime) in enumerate(attachments):
        email_id = conn.execute(
            "INSERT INTO emails (message_id, date_received, subject) VALUES (?, ?, ?)",
            (f"m{n}", received, f"mail {n}"),
        ).lastrowid
        f = tmp_path / "attachments" / f"m{n}" / name
        f.parent.mkdir(parents=True)
        f.write_text(f"The text of {name}, long enough to clear the extraction threshold.")
        conn.execute(
            "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size,"
            " file_path, exported_at) VALUES (?, ?, ?, ?, 1, ?, '2026-10-11T09:00:00')",
            (email_id, f"m{n}", name, mime, str(f)),
        )
    conn.commit()
    conn.close()
    return str(path)


def _reader(monkeypatch, clock, seconds=None, default=1.0):
    """Phase 1's reader replaced: it records each file it is given and takes `seconds[name]`
    (else `default`) of the fake clock."""
    read: list[str] = []

    def extract(file_path, mime_type, *args, **kwargs):
        name = Path(file_path).name
        read.append(name)
        clock.now += (seconds or {}).get(name, default)
        return {
            "text": f"text of {name} " * 10,
            "method": "fake",
            "status": "extracted",
            "error": None,
        }

    monkeypatch.setattr(attachment_pipeline, "extract_text_from_file", extract)
    return read


def _model(monkeypatch, clock, seconds=0.0):
    """Phase 2's model call replaced: it records each file it is asked about and takes
    `seconds` of the fake clock. The summary names the file and carries a marker word."""
    asked: list[str] = []

    def summarise(row, *args, **kwargs):
        asked.append(row[3])
        clock.now += seconds
        return (row[0], row[5], {"summary": f"okapi summary of {row[3]}"}, None, False)

    monkeypatch.setattr(attachment_pipeline, "_extract_one_attachment", summarise)
    return asked


def _extracted(db, *names):
    """Give each named attachment extracted text still awaiting its summary."""
    conn = sqlite3.connect(db)
    for name in names:
        (att_id,) = conn.execute(
            "SELECT id FROM attachments WHERE filename = ?", (name,)
        ).fetchone()
        conn.execute(
            "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method,"
            " extraction_status, extracted_at, llm_status)"
            " VALUES (?, ?, 'fake', 'extracted', '2026-10-11T09:00:00', 'pending')",
            (att_id, f"text of {name}"),
        )
    conn.commit()
    conn.close()


def _llm_status(db, name):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT ac.llm_status FROM attachment_content ac"
            " JOIN attachments a ON a.id = ac.attachment_id WHERE a.filename = ?",
            (name,),
        ).fetchone()[0]
    finally:
        conn.close()


# --- Newest mail first -------------------------------------------------------------------------

# Registration order is not mail order: the nightly pass registers a backlog of old mail after the
# day's, so neither id order picks the newest. tuesday registered first, last-month last.
_THREE = (
    ("tuesday.pdf", "2026-10-06T09:00:00Z", PDF),
    ("today.pdf", "2026-10-11T08:30:00Z", PDF),
    ("last-month.pdf", "2026-09-02T09:00:00Z", PDF),
)


def test_the_hourly_slice_reads_the_newest_mail_first(tmp_path, monkeypatch, clock):
    db = _store(tmp_path, *_THREE)
    read = _reader(monkeypatch, clock, default=60.0)

    stats = run_phase1(db, deadline_s=50, newest_first=True)

    assert read == ["today.pdf"]
    assert (stats["processed"], stats["deferred"]) == (1, 2)


def test_received_times_are_compared_as_instants_not_as_text(tmp_path, monkeypatch, clock):
    """The store holds received times with a Z, with an offset and with neither. As text,
    11:30+03:00 sorts after 09:00Z, though it is half an hour earlier."""
    db = _store(
        tmp_path,
        ("athens.pdf", "2026-10-11T11:30:00+03:00", PDF),
        ("utc.pdf", "2026-10-11T09:00:00Z", PDF),
    )
    read = _reader(monkeypatch, clock)

    run_phase1(db, newest_first=True)

    assert read == ["utc.pdf", "athens.pdf"]


def test_the_nightly_pass_keeps_its_order(tmp_path, monkeypatch, clock):
    db = _store(tmp_path, *_THREE)
    read = _reader(monkeypatch, clock, default=60.0)

    run_phase1(db, deadline_s=50)

    assert read == ["tuesday.pdf"]


# --- Images and archives wait for the nightly pass ---------------------------------------------


def test_the_hourly_slice_leaves_images_and_archives_to_the_nightly_pass(
    tmp_path, monkeypatch, clock
):
    db = _store(
        tmp_path,
        ("scan.png", "2026-10-11T08:00:00Z", "image/png"),
        ("photo.jfif", "2026-10-11T08:01:00Z", "application/octet-stream"),
        ("bundle.zip", "2026-10-11T08:02:00Z", "application/x-zip-compressed"),
        ("minutes.pdf", "2026-10-11T07:00:00Z", PDF),
    )
    read = _reader(monkeypatch, clock)

    hourly = run_phase1(db, deadline_s=90, newest_first=True, defer_heavy=True)

    assert read == ["minutes.pdf"]
    assert hourly["heavy"] == 3

    nightly = run_phase1(db, deadline_s=900)

    assert sorted(read[1:]) == ["bundle.zip", "photo.jfif", "scan.png"]
    assert nightly["heavy"] == 0


@pytest.mark.parametrize(
    "name, mime, heavy",
    [
        ("scan.png", "image/png", True),
        ("img-0f3a", "image/jpeg", True),  # an extensionless Outlook part
        ("photo.heic", "application/octet-stream", True),  # known by its name
        ("bundle.zip", "application/octet-stream", True),
        ("bundle", "application/zip", True),
        ("report.pdf", PDF, False),
        ("notes.docx", "application/octet-stream", False),
        ("old.rar", "application/x-rar-compressed", False),  # no reader: skipped at once
        ("part", None, False),
    ],
)
def test_heavy_kinds_are_images_and_zip_archives(name, mime, heavy):
    assert attachment_pipeline.is_heavy_kind(f"m1/{name}", mime) is heavy


def test_an_image_whose_bytes_are_held_is_copied_not_deferred(tmp_path, monkeypatch, clock):
    """Copying another attachment's finished row reads no file and calls no model."""
    db = _store(
        tmp_path,
        ("logo.png", "2026-10-01T09:00:00Z", "image/png"),
        ("logo.png", "2026-10-11T09:00:00Z", "image/png"),
    )
    conn = sqlite3.connect(db)
    conn.execute("UPDATE attachments SET sha256 = 'aa'")
    conn.execute(
        "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method,"
        " extraction_status, extracted_at, summary, llm_status)"
        " VALUES (1, 'held text', 'ocr', 'extracted', '2026-10-01T10:00:00', 'a logo', 'extracted')"
    )
    conn.commit()
    conn.close()
    read = _reader(monkeypatch, clock)

    stats = run_phase1(db, deadline_s=90, newest_first=True, defer_heavy=True)

    assert read == []
    assert (stats["reused"], stats["heavy"]) == (1, 0)


# --- One file longer than the slice ------------------------------------------------------------


def test_a_file_that_outlasts_the_slice_costs_one_run_and_is_not_read_again(
    tmp_path, monkeypatch, clock
):
    """A scan can take OCR_MAX_SECONDS against the 90 s slice. The deadline is checked before a
    file starts, so the scan runs to its own cap and its row is written, whatever it read; the
    rest wait, and the next run moves on, because a file with a row is never offered again."""
    from src.extract.attachment_extractors import OCR_MAX_SECONDS
    from src.pipeline.sync import PHASE1_SYNC_DEADLINE_S

    db = _store(
        tmp_path,
        ("tuesday.pdf", "2026-10-06T09:00:00Z", PDF),
        ("scan.pdf", "2026-10-11T08:30:00Z", PDF),
        ("monday.pdf", "2026-10-05T09:00:00Z", PDF),
    )
    read = _reader(monkeypatch, clock, seconds={"scan.pdf": OCR_MAX_SECONDS})

    first = run_phase1(db, deadline_s=PHASE1_SYNC_DEADLINE_S, newest_first=True, defer_heavy=True)
    second = run_phase1(db, deadline_s=PHASE1_SYNC_DEADLINE_S, newest_first=True, defer_heavy=True)

    assert read == ["scan.pdf", "tuesday.pdf", "monday.pdf"]
    assert (first["processed"], first["deferred"]) == (1, 2)
    assert (second["processed"], second["deferred"]) == (2, 0)


# --- Phase 2 starts with what Phase 1 has just read --------------------------------------------


def test_phase2_starts_with_what_phase1_just_read_then_the_newest(tmp_path, monkeypatch, clock):
    db = _store(
        tmp_path,
        ("older-waiting.pdf", "2026-10-08T09:00:00Z", PDF),
        ("just-read.pdf", "2026-10-10T09:00:00Z", PDF),
        ("newer-waiting.pdf", "2026-10-11T07:00:00Z", PDF),
    )
    _extracted(db, "older-waiting.pdf", "newer-waiting.pdf")
    _reader(monkeypatch, clock)
    phase1 = run_phase1(db, newest_first=True)
    asked = _model(monkeypatch, clock, seconds=40.0)

    phase2 = run_phase2(db, first_ids=phase1["ids"], newest_first=True, deadline_s=60)

    assert asked == ["just-read.pdf", "newer-waiting.pdf"]
    assert phase2["deferred"] == 1
    assert _llm_status(db, "older-waiting.pdf") == "pending"


# --- The sync's Step 6 -------------------------------------------------------------------------


def _sync(tmp_path, monkeypatch, *, phase1=None, stamp=None, loaded=0, registered=()):
    """Run cmd_sync over `tmp_path`'s store with every step but 6 stubbed. `phase1` replaces
    run_phase1's result (None runs the real one); run_phase2 is real unless phase1 is given.
    `stamp` is the nightly text pass's sync_metadata stamp, if any. Returns the two phases' mocks
    (None for a real one)."""
    from src import cli
    from src.store.schema import create_database

    db_path = tmp_path / "brain.db"
    if not db_path.exists():
        create_database(str(db_path)).close()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO sync_metadata (key, value)"
        " VALUES ('last_sync_date', '2026-01-01T00:00:00')"
    )
    if stamp is not None:
        from src.extract.attachment_pipeline import TEXT_PASS_KEY

        conn.execute(
            "INSERT OR REPLACE INTO sync_metadata (key, value) VALUES (?, ?)",
            (TEXT_PASS_KEY, stamp),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr("src.pipeline.sync.DATA_ROOT", tmp_path)
    args = types.SimpleNamespace(
        db=db_path, limit=None, engine="claude", workers=1, skip_export=True
    )
    stubs = [
        patch(
            "src.extract.local.run_extraction",
            return_value={"extracted": 0, "failed": 0, "quota_paused": False},
        ),
        patch("src.store.loader.load_extractions", return_value=loaded),
        patch(
            "src.export.outlook_attachments.register_downloaded_attachments",
            return_value={
                "ids": list(registered),
                "registered": len(registered),
                "scanned": 1,
                "deferred": 0,
            },
        ),
        patch("src.store.dedup_people.run_dedup", return_value={"total_reduced": 0}),
        patch("src.store.embeddings.build_index"),
        patch("src.export.conversation_export.export_conversations", return_value={"exported": 0}),
        patch("src.extract.image_pipeline.run_backfill", return_value={}),
    ]
    if phase1 is not None:
        stubs += [
            patch("src.extract.attachment_pipeline.run_phase1", return_value=phase1),
            patch(
                "src.extract.attachment_pipeline.run_phase2",
                return_value={"processed": 0, "extracted": 0, "failed": 0, "deferred": 0},
            ),
        ]
    mocks = [s.start() for s in stubs]
    try:
        cli.cmd_sync(args)
    finally:
        for s in stubs:
            s.stop()
    return (mocks[-2], mocks[-1]) if phase1 is not None else (None, None)


def _hours_ago(hours):
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


def test_the_hourly_sync_reads_newest_first_and_leaves_heavy_kinds_to_the_nightly_pass(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BRAIN_SYNC_HEAVY_ATTACHMENTS", "")
    phase1, _ = _sync(tmp_path, monkeypatch, phase1={"processed": 0}, stamp=_hours_ago(8))

    kwargs = phase1.call_args.kwargs
    assert kwargs["newest_first"] is True
    assert kwargs["defer_heavy"] is True


@pytest.mark.parametrize(
    "stamp",
    [
        pytest.param(None, id="never"),
        pytest.param(_hours_ago(60), id="two-nights-ago"),
        pytest.param("not a time", id="unreadable"),
    ],
)
def test_without_a_recent_nightly_pass_the_hourly_slice_reads_every_kind(
    tmp_path, monkeypatch, stamp
):
    """A host that runs `sync` alone, as the documented cron schedule does, has no pass to leave
    them to; nor has one whose pass stopped two nights ago."""
    monkeypatch.setenv("BRAIN_SYNC_HEAVY_ATTACHMENTS", "")
    phase1, _ = _sync(tmp_path, monkeypatch, phase1={"processed": 0}, stamp=stamp)

    assert phase1.call_args.kwargs["defer_heavy"] is False


def test_the_switch_reads_them_hourly_whatever_the_nightly_pass_does(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_SYNC_HEAVY_ATTACHMENTS", "1")
    phase1, _ = _sync(tmp_path, monkeypatch, phase1={"processed": 0}, stamp=_hours_ago(8))

    assert phase1.call_args.kwargs["defer_heavy"] is False


def test_phase2_is_handed_what_phase1_just_read(tmp_path, monkeypatch):
    read = {"processed": 2, "extracted": 2, "failed": 0, "skipped": 0, "ids": [5, 9]}
    _, phase2 = _sync(tmp_path, monkeypatch, phase1=read)

    kwargs = phase2.call_args.kwargs
    assert kwargs["first_ids"] == [5, 9]
    assert kwargs["newest_first"] is True


def test_phase2_runs_when_phase1_found_nothing_new(tmp_path, monkeypatch):
    """Text read by an earlier run and still waiting for its summary is drained hourly too, not
    only after Phase 1 has read something."""
    _, phase2 = _sync(tmp_path, monkeypatch, phase1={"processed": 0})

    phase2.assert_called_once()


def test_a_document_mailed_this_morning_is_searchable_by_summary_after_one_hourly_run(
    tmp_path, monkeypatch, clock
):
    """Two old documents wait without text, each a minute's read against the 90 s slice, and
    this run registers this morning's minutes. Oldest first, the slice spent itself on the old
    two and Phase 2, scoped to this run's registrations, found nothing to summarise."""
    from src.store.query import search_attachments
    from src.store.schema import get_connection

    db = _store(
        tmp_path,
        ("old-1.pdf", "2026-09-01T09:00:00Z", PDF),
        ("old-2.pdf", "2026-09-02T09:00:00Z", PDF),
        ("minutes.pdf", "2026-10-11T07:30:00Z", PDF),
    )
    _reader(monkeypatch, clock, seconds={"old-1.pdf": 60.0, "old-2.pdf": 60.0})
    _model(monkeypatch, clock)

    _sync(tmp_path, monkeypatch, loaded=1, registered=[3])

    conn = get_connection(db)
    try:
        found = [r["filename"] for r in search_attachments(conn, "okapi")]
    finally:
        conn.close()
    assert "minutes.pdf" in found
    assert _llm_status(db, "minutes.pdf") == "extracted"


# --- The nightly pass leaves a stamp -----------------------------------------------------------


def _process_attachments(tmp_path, **overrides):
    from src import cli
    from src.store.schema import create_database

    db_path = tmp_path / "brain.db"
    create_database(str(db_path)).close()
    args = types.SimpleNamespace(
        db=db_path, phase=1, type=None, limit=0, estimate=False, deadline_s=900, workers=1
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    stats = {"processed": 0, "extracted": 0, "failed": 0, "skipped": 0}
    with patch("src.extract.attachment_pipeline.run_phase1", return_value=stats):
        assert cli.cmd_process_attachments(args) == 0
    conn = sqlite3.connect(db_path)
    try:
        from src.extract.attachment_pipeline import TEXT_PASS_KEY

        row = conn.execute(
            "SELECT value FROM sync_metadata WHERE key = ?", (TEXT_PASS_KEY,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def test_the_nightly_text_pass_leaves_its_stamp(tmp_path):
    stamp = _process_attachments(tmp_path)

    assert stamp is not None
    assert datetime.now(UTC) - datetime.fromisoformat(stamp) < timedelta(minutes=1)


def test_a_pass_over_one_kind_leaves_none(tmp_path):
    """Only a pass that reads every kind will take the images and archives the hourly slice
    leaves it."""
    assert _process_attachments(tmp_path, type="pdf") is None
