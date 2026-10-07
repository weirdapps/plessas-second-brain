"""scripts/relabel_attachment_status.py gives old rows the verdicts Phase 1 now reaches unread.

Rows written before the classifier carry old labels: an RMS-protected workbook "failed" with
BadZipFile, a protected message "skipped" by a parser that never worked, a media file skipped
with no reason, a SharePoint page shell skipped as an unsupported type. The script relabels
each failed or skipped row from its file's name, declared type and first bytes, reads no text,
leaves what a reader would now read to reextract, and never touches a 'vision' row, an extracted
row, or a row whose file is gone. Dry run unless --apply; --apply is refused on a replica.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

from src.extract.attachment_extractors import extract_text_from_file, verdict_without_reading
from src.store.schema import create_database
from tests.ole_fixtures import PASSWORD_NAMES, RMS_NAMES, STREAM, ole_file

MSIPC = b"\x76\xe8\x04\x60\xc4\x11\xe3\x86" + b"\x00" * 120
WORDS = "The plan for the regional network, in enough words to pass the filter. "
# The skip the old .xlsb reader wrote, dash and all, as the producer rows carry it.
OLD_EXPORT_SKIP = (
    "OLE compound document, no Excel workbook stream "
    + chr(0x2014)
    + " likely custom export format, not standard Excel"
)


def _script():
    path = Path(__file__).parent.parent / "scripts" / "relabel_attachment_status.py"
    spec = importlib.util.spec_from_file_location("relabel_attachment_status", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    conn.execute(
        "INSERT INTO emails (message_id, date_received, subject) VALUES (7, '2026-09-01', 'mail')"
    )
    conn.commit()
    yield path, conn, tmp_path
    conn.close()


def _row(conn, root, name, body, *, status, error=None, method=None, text=None, mime="", fp=None):
    n = conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] + 1
    if fp is None:
        f = root / "att" / f"dir{n}" / name
        if body is not None:
            f.parent.mkdir(parents=True)
            f.write_bytes(body)
        fp = str(f)
    conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size,"
        " file_path, exported_at) VALUES (1, 7, ?, ?, 1, ?, '2026-09-01')",
        (name, mime, fp),
    )
    att = conn.execute("SELECT MAX(id) FROM attachments").fetchone()[0]
    conn.execute(
        "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method,"
        " extraction_status, extraction_error, extracted_at, llm_status)"
        " VALUES (?, ?, ?, ?, ?, '2026-09-01', 'pending')",
        (att, text, method, status, error),
    )
    conn.commit()
    return att


def _label(conn, att):
    return conn.execute(
        "SELECT extraction_status, extraction_method, extraction_error, extracted_at, llm_status"
        " FROM attachment_content WHERE attachment_id = ?",
        (att,),
    ).fetchone()


def _main(path, root, *args):
    return _script().main(["--db", str(path), "--root", str(root / "att"), *args])


def _encrypted_pdf() -> bytes:
    import fitz

    doc = fitz.open()
    doc.new_page().insert_text((72, 72), WORDS)
    return doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")


@pytest.fixture
def rows(store):
    path, conn, root = store
    made = {
        "rms_xlsx": _row(
            conn,
            root,
            "Daily.xlsx",
            ole_file(RMS_NAMES),
            status="failed",
            error="BadZipFile: File is not a zip file",
        ),
        "rms_xlsb": _row(
            conn,
            root,
            "Plan.xlsb",
            ole_file(RMS_NAMES),
            status="skipped",
            method="pyxlsb",
            error=OLD_EXPORT_SKIP,
        ),
        "message": _row(
            conn,
            root,
            "message.rpmsg",
            MSIPC,
            status="skipped",
            method="compoundfiles",
            error=OLD_EXPORT_SKIP,
        ),
        "password_docx": _row(
            conn,
            root,
            "Budget.docx",
            ole_file(PASSWORD_NAMES),
            status="failed",
            error="PackageNotFoundError: Package not found",
        ),
        "password_pdf": _row(
            conn,
            root,
            "statement.pdf",
            _encrypted_pdf(),
            status="failed",
            error="ValueError: document closed or encrypted",
        ),
        "password_xls": _row(
            conn,
            root,
            "Old.xls",
            ole_file([("Workbook", STREAM)]),
            status="failed",
            method="xlrd",
            error="xlrd open failed: Workbook is encrypted",
        ),
        "video": _row(conn, root, "clip.mp4", b"\x00\x00\x00\x18ftypmp42", status="skipped"),
        "shell": _row(
            conn,
            root,
            "Page.aspx",
            None,
            status="skipped",
            error="Unsupported type: application/octet-stream (.aspx)",
            fp="text:sharepoint:0a1b2c",
        ),
        "drawing": _row(conn, root, "image001.wmz", b"\x1f\x8b\x08\x00", status="skipped"),
        "vision": _row(
            conn,
            root,
            "chart.png",
            ole_file(RMS_NAMES),
            status="skipped",
            method="vision",
            error="OCR returned insufficient text",
        ),
        "extracted": _row(
            conn,
            root,
            "Fine.xlsx",
            ole_file(RMS_NAMES),
            status="extracted",
            method="openpyxl",
            text=WORDS,
        ),
        "gone": _row(
            conn,
            root,
            "Lost.xlsx",
            None,
            status="failed",
            error="BadZipFile: File is not a zip file",
            fp="/nowhere/dir9/Lost.xlsx",
        ),
    }
    return path, conn, root, made


def test_a_dry_run_reports_and_changes_nothing(rows, capsys):
    path, conn, root, made = rows
    before = {k: _label(conn, v) for k, v in made.items()}

    assert _main(path, root) == 0

    assert {k: _label(conn, v) for k, v in made.items()} == before
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "  to change     : 8" in out
    assert "failed -> encrypted/rms" in out
    assert "  files missing : 1" in out


def test_apply_relabels_encrypted_files_by_kind(rows):
    path, conn, root, made = rows

    assert _main(path, root, "--apply") == 0

    assert _label(conn, made["rms_xlsx"])[:2] == ("encrypted", "rms")
    assert _label(conn, made["rms_xlsb"])[:2] == ("encrypted", "rms")
    assert _label(conn, made["message"])[:2] == ("encrypted", "rms-message")
    assert _label(conn, made["password_docx"])[:2] == ("encrypted", "password")
    assert _label(conn, made["password_pdf"])[:2] == ("encrypted", "password")
    assert _label(conn, made["password_xls"])[:2] == ("encrypted", "password")
    assert "Password-protected .xls" in _label(conn, made["password_xls"])[2]


def test_apply_gives_skips_their_reasons(rows):
    path, conn, root, made = rows

    _main(path, root, "--apply")

    assert _label(conn, made["video"])[:3] == ("skipped", None, "video: no text to read")
    status, _method, error, *_ = _label(conn, made["shell"])
    assert status == "skipped" and error.startswith("SharePoint page shell:")


def test_apply_leaves_alone_what_it_must(rows):
    path, conn, root, made = rows
    before = {k: _label(conn, made[k]) for k in ("drawing", "vision", "extracted", "gone")}

    _main(path, root, "--apply")

    assert {k: _label(conn, made[k]) for k in before} == before


def test_apply_changes_only_the_three_label_columns(rows):
    path, conn, root, made = rows

    _main(path, root, "--apply")

    _status, _method, _error, extracted_at, llm = _label(conn, made["rms_xlsx"])
    assert (extracted_at, llm) == ("2026-09-01", "pending")


def test_a_second_apply_finds_nothing_to_change(rows, capsys):
    path, conn, root, made = rows
    _main(path, root, "--apply")
    capsys.readouterr()

    _main(path, root, "--apply")

    assert "  to change     : 0" in capsys.readouterr().out


def test_apply_is_refused_on_a_replica_and_the_dry_run_is_not(rows, monkeypatch):
    path, conn, root, made = rows
    monkeypatch.setenv("BRAIN_ROLE", "replica")

    assert _main(path, root, "--apply") == 2
    assert _label(conn, made["rms_xlsx"])[0] == "failed"
    assert _main(path, root) == 0


def test_the_verdict_matches_what_phase_1_records(tmp_path):
    """Where the unread verdict exists, it is the one Phase 1 writes: the two cannot disagree."""
    files = {
        "a.xlsx": ole_file(RMS_NAMES),
        "b.docx": ole_file(PASSWORD_NAMES),
        "c.rpmsg": MSIPC,
        "d.pdf": _encrypted_pdf(),
        "e.mp4": b"\x00\x00\x00\x18ftypmp42",
        "f.7z": b"7z\xbc\xaf\x27\x1c",
        "g.txt": b"",
    }
    for name, data in files.items():
        p = tmp_path / name
        p.write_bytes(data)
        verdict = verdict_without_reading(str(p), "application/octet-stream")
        assert verdict is not None, name
        assert verdict == extract_text_from_file(str(p), "application/octet-stream"), name


def test_a_file_that_must_be_read_has_no_unread_verdict(tmp_path):
    for name, data, mime in (
        ("a.wmz", b"\x1f\x8b\x08\x00", "application/gzip"),
        ("b.txt", WORDS.encode(), "text/plain"),
    ):
        p = tmp_path / name
        p.write_bytes(data)
        assert verdict_without_reading(str(p), mime) is None, name


def test_the_script_runs_from_the_command_line(rows, monkeypatch):
    path, conn, root, made = rows
    module = _script()
    monkeypatch.setattr(sys, "argv", ["relabel", "--db", str(path), "--root", str(root / "att")])

    assert module.main() == 0
