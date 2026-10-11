"""`refetch-garbled` downloads again, from the mailbox, the swept originals of OCR texts marked as
Greek read as Latin look-alike letters, and leaves them for `reextract --garbled` and the image
pass.

`ocr-regarble` found about a thousand such texts on the producer, nearly all of whose files the
sweep had already deleted (data-quality-03), and a marked text is only read better from its file.
Mail older than about a year is no longer in the mailbox (code-ingest-03), so a message that is
gone is recorded as gone and never asked for again. Mail the retired Apple Mail exporter stored
carries no id the mailbox knows: it is looked up by folder, received time and subject, and a file
is put back only when its sha256 is the row's. outlook-cli is never called: a stub of the adapter
serves a mailbox each test fills.
"""

import hashlib
import json
import random
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.export import mail_reconcile as mr
from src.export import refetch_garbled as rg
from src.export.outlook_cli import OutlookCliAuthRequired, OutlookCliError
from src.extract import attachment_pipeline
from src.extract import reextract as rx
from src.extract.image_pipeline import run_transcription
from src.extract.ocr_garble import OCR_GARBLED_NOTE, is_garbled
from src.store.file_hashes import locate_file
from src.store.file_sweep import (
    DELETABLE,
    NOT_HELD,
    SweepPolicy,
    _registered,
    sweep_files,
)
from src.store.schema import create_database
from tests.greek_text import GREEK, garble

# Graph ids are long base64 strings; these are made up.
MESSAGE = "AAMkAGQ0TESTMESSAGE0000000000000001AAA="
OTHER = "AAMkAGQ0TESTMESSAGE0000000000000002AAA="
THIRD = "AAMkAGQ0TESTMESSAGE0000000000000003AAA="
ARCHIVED = "AAMkAGQ0TESTARCHIVECOPY00000000000001AAA="


def _bytes(seed: str, n: int = 2048) -> bytes:
    return random.Random(seed).randbytes(n)


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


SCAN = _bytes("scan")
LOGO = _bytes("logo")


def _not_found() -> OutlookCliError:
    """What outlook-cli says, exit 5, when the mailbox holds no message under an id."""
    payload = {
        "error": {
            "code": "UPSTREAM_HTTP_404",
            "message": "The specified object was not found in the store.",
            "httpStatus": 404,
        }
    }
    return OutlookCliError(exit_code=5, stderr=json.dumps(payload, indent=2), retryable=True)


def _unavailable() -> OutlookCliError:
    payload = {"error": {"code": "UPSTREAM_HTTP_503", "message": "busy", "httpStatus": 503}}
    return OutlookCliError(exit_code=5, stderr=json.dumps(payload), retryable=True)


class Mailbox:
    """A stub of the adapter. `download-attachments` writes a message's files into --out, and
    `list-mail` lists the messages a folder holds received in [--since, --until)."""

    def __init__(self):
        self.messages: dict[str, list[tuple[str, bytes]]] = {}
        self.folders: dict[str, list[dict]] = {}
        self.errors: dict[str, Exception] = {}
        self.list_errors: dict[str, Exception] = {}
        self.calls: list[list[str]] = []
        self.delay = 0.0
        self.running = 0
        self.peak = 0
        self._lock = threading.Lock()

    def holds(self, message_id: str, *files: tuple[str, bytes]) -> None:
        self.messages[message_id] = list(files)

    def files(
        self, folder: str, message_id: str, received: str, subject: str, *files: tuple[str, bytes]
    ) -> None:
        """A message in `folder`, received at `received` as the mailbox lists it (UTC)."""
        self.folders.setdefault(folder, []).append(
            {
                "Id": message_id,
                "InternetMessageId": None,
                "ReceivedDateTime": received,
                "Subject": subject,
            }
        )
        self.holds(message_id, *files)

    def asked(self) -> list[str]:
        """The message ids whose attachments were asked for."""
        return [args[1] for args in self.calls if args[0] == "download-attachments"]

    def listed(self) -> list[str]:
        """The folders listed, in order."""
        return [args[args.index("--folder") + 1] for args in self.calls if args[0] == "list-mail"]

    def __call__(self, args, timeout_sec=60):
        with self._lock:
            self.calls.append(list(args))
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            time.sleep(self.delay)
            if args[0] == "list-mail":
                return self._list(args)
            return self._download(args)
        finally:
            with self._lock:
                self.running -= 1

    def _list(self, args):
        folder = args[args.index("--folder") + 1]
        if folder in self.list_errors:
            raise self.list_errors[folder]
        since, until = args[args.index("--since") + 1], args[args.index("--until") + 1]
        return [m for m in self.folders.get(folder, []) if since <= m["ReceivedDateTime"] < until]

    def _download(self, args):
        assert args[0] == "download-attachments"
        message_id = args[1]
        if message_id in self.errors:
            raise self.errors[message_id]
        if message_id not in self.messages:
            raise _not_found()
        out = Path(args[args.index("--out") + 1])
        saved = []
        for name, data in self.messages[message_id]:
            (out / name).write_bytes(data)
            saved.append({"id": f"att-{name}", "name": name, "path": str(out / name)})
        return {"messageId": message_id, "outDir": str(out), "saved": saved, "skipped": []}


@pytest.fixture
def mailbox(monkeypatch):
    box = Mailbox()
    monkeypatch.setattr(rg, "run_outlook_cli", box)
    # The lookup lists a folder as mail-reconcile does, through that module's adapter.
    monkeypatch.setattr(mr, "run_outlook_cli", box)
    return box


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "brain.db"
    conn = create_database(str(path))
    yield path, conn, tmp_path / "att"
    conn.close()


def _email(
    conn,
    message_id,
    received="2026-01-15T09:00:00Z",
    aliases=(),
    *,
    subject="mail",
    folder="Archive",
) -> int:
    email_id = conn.execute(
        "INSERT INTO emails (message_id, date_received, subject, mailbox_name) VALUES (?, ?, ?, ?)",
        (message_id, received, subject, folder),
    ).lastrowid
    for alias in aliases:
        conn.execute(
            "INSERT INTO email_aliases (message_id, email_id, recorded_at) VALUES (?, ?, ?)",
            (alias, email_id, "2026-10-08T00:00:00Z"),
        )
    conn.commit()
    return email_id


def _garbled(
    conn,
    root: Path,
    email_id: int | None,
    directory: str,
    name: str = "scan.png",
    data: bytes = SCAN,
    *,
    mime: str = "image/png",
    method: str = "ocr",
    hashed: bool = True,
    recorded_root: Path | None = None,
) -> int:
    """A garbled OCR row whose file the sweep deleted: its path recorded, nothing on disk."""
    file_path = (recorded_root or root) / directory / name
    att = conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size,"
        " file_path, exported_at, sha256, file_removed_at)"
        " VALUES (?, ?, ?, ?, ?, ?, '2026-04-06T10:00:00', ?, '2026-09-01T00:00:00+00:00')",
        (
            email_id,
            directory,
            name,
            mime,
            len(data),
            str(file_path),
            sha256_of_bytes(data) if hashed else None,
        ),
    ).lastrowid
    conn.execute(
        "INSERT INTO attachment_content (attachment_id, extracted_text, extraction_method,"
        " extraction_status, extraction_error, extracted_at, llm_status)"
        " VALUES (?, ?, ?, 'extracted', ?, '2026-04-06T10:00:00', 'skipped')",
        (att, garble(GREEK), method, OCR_GARBLED_NOTE),
    )
    conn.commit()
    return att


def _attachment(conn, att):
    return dict(
        conn.execute(
            "SELECT file_path, file_removed_at, sha256 FROM attachments WHERE id = ?", (att,)
        ).fetchone()
    )


def _files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def _run(path, root, **kw):
    kw.setdefault("workers", 1)
    return rg.refetch_garbled(str(path), root=str(root), **kw)


# --- fetched, matched and put back -------------------------------------------------------------


def test_a_swept_original_is_put_back_where_its_row_records_it(store, mailbox):
    path, conn, root = store
    att = _garbled(conn, root, _email(conn, MESSAGE), MESSAGE)
    mailbox.holds(MESSAGE, ("scan.png", SCAN), ("logo.png", LOGO))

    stats = _run(path, root)

    restored = root / MESSAGE / "scan.png"
    assert restored.read_bytes() == SCAN
    assert (stats["messages_tried"], stats["fetched"]) == (1, 1)
    assert _attachment(conn, att) == {
        "file_path": str(restored),
        "file_removed_at": None,
        "sha256": sha256_of_bytes(SCAN),
    }
    # The message's other file was not asked for, and is not kept.
    assert _files(root) == [f"{MESSAGE}/scan.png"]
    args = mailbox.calls[0]
    assert args[:2] == ["download-attachments", MESSAGE]
    assert "--include-inline" in args
    assert not Path(args[args.index("--out") + 1]).is_relative_to(root)


def test_a_fetched_original_is_left_marked_for_reextract_garbled(store, mailbox):
    path, conn, root = store
    att = _garbled(
        conn,
        root,
        _email(conn, MESSAGE),
        MESSAGE,
        "letter.pdf",
        mime="application/pdf",
        method="pymupdf+tesseract",
    )
    mailbox.holds(MESSAGE, ("letter.pdf", SCAN))
    assert rx.reextract(str(path), {"garbled"}, dry_run=True, root=str(root))["missing"] == 1

    _run(path, root)

    error = conn.execute(
        "SELECT extraction_error FROM attachment_content WHERE attachment_id = ?", (att,)
    ).fetchone()[0]
    assert is_garbled(error)
    stats = rx.reextract(str(path), {"garbled"}, dry_run=True, root=str(root))
    assert (stats["reread"], stats["missing"]) == (1, 0)


def test_a_fetched_image_is_found_by_the_image_pass(store, mailbox):
    """The image pass finds a file by the path its row records, so a row recorded on another
    host is pointed at the file put back here."""
    path, conn, root = store
    att = _garbled(
        conn, root, _email(conn, MESSAGE), MESSAGE, recorded_root=Path("/elsewhere/attachments")
    )
    conn.execute(
        "INSERT INTO inline_images (sha256, width, height, bytes, classification,"
        " classification_method, classified_at, vision_description, visioned_at)"
        " VALUES (?, 120, 120, 2048, 'content', 'vision_llm', '2026-09-01', 'a scanned letter',"
        " '2026-09-01T10:00:00+00:00')",
        (sha256_of_bytes(SCAN),),
    )
    conn.commit()
    mailbox.holds(MESSAGE, ("scan.png", SCAN))
    assert run_transcription(conn, dry_run=True)["missing"] == 1

    _run(path, root)

    assert _attachment(conn, att)["file_path"] == str(root / MESSAGE / "scan.png")
    assert run_transcription(conn, dry_run=True)["candidates"] == 1


def test_a_relative_root_records_an_absolute_path(store, mailbox, monkeypatch):
    path, conn, root = store
    att = _garbled(
        conn, root, _email(conn, MESSAGE), MESSAGE, recorded_root=Path("/elsewhere/attachments")
    )
    mailbox.holds(MESSAGE, ("scan.png", SCAN))
    monkeypatch.chdir(root.parent)

    rg.refetch_garbled(str(path), root=root.name, workers=1)

    assert _attachment(conn, att)["file_path"] == str(root / MESSAGE / "scan.png")


def test_one_download_serves_every_row_of_a_message(store, mailbox):
    path, conn, root = store
    email = _email(conn, MESSAGE)
    first = _garbled(conn, root, email, MESSAGE, "scan.png", SCAN)
    second = _garbled(conn, root, email, MESSAGE, "logo.png", LOGO)
    mailbox.holds(MESSAGE, ("scan.png", SCAN), ("logo.png", LOGO))

    stats = _run(path, root, workers=2)

    assert mailbox.asked() == [MESSAGE]
    assert (stats["messages_tried"], stats["fetched"]) == (1, 2)
    assert _attachment(conn, first)["file_removed_at"] is None
    assert _attachment(conn, second)["file_removed_at"] is None


def test_a_file_is_matched_by_its_bytes_not_its_name(store, mailbox):
    path, conn, root = store
    att = _garbled(conn, root, _email(conn, MESSAGE), MESSAGE, "scan.png", SCAN)
    mailbox.holds(MESSAGE, ("scan.png", LOGO), ("scan (1).png", SCAN))

    _run(path, root)

    assert (root / MESSAGE / "scan.png").read_bytes() == SCAN
    assert _attachment(conn, att)["file_removed_at"] is None


def test_a_row_with_no_hash_is_matched_by_name_and_size(store, mailbox):
    path, conn, root = store
    att = _garbled(conn, root, _email(conn, MESSAGE), MESSAGE, "scan.png", SCAN, hashed=False)
    mailbox.holds(MESSAGE, ("other.png", SCAN[:100]), ("scan.png", SCAN))

    _run(path, root)

    assert (root / MESSAGE / "scan.png").read_bytes() == SCAN
    assert _attachment(conn, att)["sha256"] == sha256_of_bytes(SCAN)


def test_a_second_run_fetches_nothing(store, mailbox):
    path, conn, root = store
    _garbled(conn, root, _email(conn, MESSAGE), MESSAGE)
    mailbox.holds(MESSAGE, ("scan.png", SCAN))
    _run(path, root)

    again = _run(path, root)

    assert mailbox.asked() == [MESSAGE]
    assert (again["messages_tried"], again["fetched"], again["already_fetched"]) == (0, 0, 1)


def test_a_message_that_no_longer_holds_the_bytes_is_not_asked_again(store, mailbox):
    path, conn, root = store
    att = _garbled(conn, root, _email(conn, MESSAGE), MESSAGE)
    mailbox.holds(MESSAGE, ("scan.png", LOGO))

    stats = _run(path, root)
    again = _run(path, root)

    assert (stats["unmatched"], stats["fetched"]) == (1, 0)
    assert rg.fetch_states(conn)[att]["state"] == "unmatched"
    assert again["unmatched_before"] == 1
    assert mailbox.asked() == [MESSAGE]
    assert _files(root) == []


def test_the_registrar_does_not_register_a_fetched_original_again(store, mailbox):
    from src.export.outlook_attachments import register_downloaded_attachments

    path, conn, root = store
    _garbled(conn, root, _email(conn, MESSAGE), MESSAGE)
    mailbox.holds(MESSAGE, ("scan.png", SCAN))
    _run(path, root)

    assert register_downloaded_attachments(conn, root)["registered"] == 0


# --- gone, failed, and ids the mailbox cannot be asked for -------------------------------------


def test_a_message_the_mailbox_no_longer_holds_is_marked_gone_and_not_retried(store, mailbox):
    path, conn, root = store
    att = _garbled(conn, root, _email(conn, MESSAGE, "2025-06-01T09:00:00Z"), MESSAGE)

    stats = _run(path, root)
    again = _run(path, root)

    assert (stats["messages_tried"], stats["gone"], stats["gone_files"]) == (1, 1, 1)
    record = rg.fetch_states(conn)[att]
    assert record["state"] == "gone"
    assert record["gone_at"]
    assert (again["messages_tried"], again["gone_before"]) == (0, 1)
    assert mailbox.asked() == [MESSAGE]


def test_a_moved_message_is_asked_for_by_the_id_of_its_copy(store, mailbox):
    """A move to Archive gives a message a new id: the one its directory is named for answers
    404, and the alias the loader recorded finds it."""
    path, conn, root = store
    att = _garbled(conn, root, _email(conn, MESSAGE, aliases=[ARCHIVED]), MESSAGE)
    mailbox.holds(ARCHIVED, ("scan.png", SCAN))

    stats = _run(path, root)

    assert mailbox.asked() == [MESSAGE, ARCHIVED]
    assert (stats["fetched"], stats["gone"]) == (1, 0)
    assert rg.fetch_states(conn)[att]["state"] == "fetched"
    assert (root / MESSAGE / "scan.png").read_bytes() == SCAN


@pytest.mark.parametrize(
    "failure",
    [_unavailable(), subprocess.TimeoutExpired("outlook-cli", 300)],
    ids=["service-error", "timeout"],
)
def test_a_failure_is_retried_on_later_runs_up_to_three_attempts(store, mailbox, failure):
    path, conn, root = store
    att = _garbled(conn, root, _email(conn, MESSAGE), MESSAGE)
    mailbox.errors[MESSAGE] = failure

    runs = [_run(path, root) for _ in range(4)]

    assert [r["failed"] for r in runs] == [1, 1, 1, 0]
    assert runs[3]["given_up"] == 1
    assert len(mailbox.calls) == 3
    record = rg.fetch_states(conn)[att]
    assert (record["state"], record["attempts"]) == ("failed", 3)
    assert record["last_attempt_at"]


def test_a_failure_then_a_success_fetches_it(store, mailbox):
    path, conn, root = store
    att = _garbled(conn, root, _email(conn, MESSAGE), MESSAGE)
    mailbox.errors[MESSAGE] = _unavailable()
    _run(path, root)
    del mailbox.errors[MESSAGE]
    mailbox.holds(MESSAGE, ("scan.png", SCAN))

    stats = _run(path, root)

    assert stats["fetched"] == 1
    assert rg.fetch_states(conn)[att]["state"] == "fetched"


def test_an_apple_mail_number_is_never_sent_to_outlook(store, mailbox):
    """Mail the retired Apple Mail exporter stored carries Mail.app's numbers, not Graph ids, and
    such an email holds no copy id either: it is counted, and not asked for by id (the lookup,
    turned off here, finds it by folder, time and subject)."""
    path, conn, root = store
    att = _garbled(conn, root, _email(conn, 48213), "48213")

    stats = _run(path, root, lookup=False)

    assert mailbox.calls == []
    assert (stats["no_mailbox_id"], stats["messages_tried"]) == (1, 0)
    assert att not in rg.fetch_states(conn)


def test_an_apple_mail_email_with_a_recorded_copy_is_asked_for_by_the_copy_id(store, mailbox):
    path, conn, root = store
    att = _garbled(conn, root, _email(conn, 48213, aliases=[ARCHIVED]), "48213")
    mailbox.holds(ARCHIVED, ("scan.png", SCAN))

    stats = _run(path, root)

    assert mailbox.asked() == [ARCHIVED]
    assert (stats["no_mailbox_id"], stats["fetched"]) == (0, 1)
    assert (root / "48213" / "scan.png").read_bytes() == SCAN
    assert _attachment(conn, att)["file_removed_at"] is None


def test_reauthentication_stops_the_run_and_counts_no_attempt(store, mailbox):
    path, conn, root = store
    first = _garbled(conn, root, _email(conn, MESSAGE, "2026-01-01T00:00:00Z"), MESSAGE)
    _garbled(conn, root, _email(conn, OTHER, "2026-02-01T00:00:00Z"), OTHER)
    mailbox.errors[MESSAGE] = OutlookCliAuthRequired("session expired")
    mailbox.holds(OTHER, ("scan.png", SCAN))

    stats = _run(path, root)

    assert stats["stopped"] is True
    assert mailbox.asked() == [MESSAGE]
    # Neither message was answered: both are left for the next run.
    assert (stats["fetched"], stats["failed"], stats["left"]) == (0, 0, 2)
    assert first not in rg.fetch_states(conn)


def test_the_oldest_mail_goes_first_and_the_limit_bounds_the_messages(store, mailbox):
    """Mail crosses the mailbox's retention edge oldest first."""
    path, conn, root = store
    _garbled(conn, root, _email(conn, THIRD, "2026-03-01T00:00:00Z"), THIRD)
    _garbled(conn, root, _email(conn, MESSAGE, "2025-11-01T00:00:00Z"), MESSAGE)
    _garbled(conn, root, _email(conn, OTHER, "2026-01-01T00:00:00Z"), OTHER)
    for message_id in (MESSAGE, OTHER, THIRD):
        mailbox.holds(message_id, ("scan.png", SCAN))

    stats = _run(path, root, limit=2, workers=2)

    assert sorted(mailbox.asked()) == sorted([MESSAGE, OTHER])
    assert (stats["messages_to_try"], stats["messages_tried"], stats["left"]) == (3, 2, 1)


def test_a_file_on_disk_and_a_text_only_row_are_not_fetched(store, mailbox):
    path, conn, root = store
    email = _email(conn, MESSAGE)
    on_disk = _garbled(conn, root, email, MESSAGE, "scan.png", SCAN)
    (root / MESSAGE).mkdir(parents=True)
    (root / MESSAGE / "scan.png").write_bytes(SCAN)
    text_only = _garbled(conn, root, email, MESSAGE, "note.txt", LOGO)
    conn.execute(
        "UPDATE attachments SET file_path = 'text:sharepoint:key' WHERE id = ?", (text_only,)
    )
    conn.commit()

    stats = _run(path, root)

    assert mailbox.calls == []
    assert (stats["garbled"], stats["on_disk"], stats["to_try"]) == (1, 1, 0)
    assert locate_file(_attachment(conn, on_disk)["file_path"], root) is not None


# --- the dry run, and the selection's cost -----------------------------------------------------


def test_the_dry_run_counts_and_downloads_or_writes_nothing(store, mailbox):
    path, conn, root = store
    email = _email(conn, MESSAGE)
    _garbled(conn, root, email, MESSAGE, "scan.png", SCAN)
    _garbled(conn, root, email, MESSAGE, "logo.png", LOGO)
    _garbled(conn, root, _email(conn, 48213), "48213")
    mailbox.holds(MESSAGE, ("scan.png", SCAN), ("logo.png", LOGO))

    stats = _run(path, root, dry_run=True)

    assert mailbox.calls == []
    assert _files(root) == []
    assert rg.fetch_states(conn) == {}
    assert (stats["to_try"], stats["messages_to_try"], stats["no_mailbox_id"]) == (2, 1, 1)
    assert (stats["messages_tried"], stats["fetched"]) == (0, 0)


def _poison_the_stored_text(path: Path) -> int:
    """Overwrite every overflow page of attachment_content, so reading a long text fails.
    The same technique as tests/test_attachment_content_indexes.py."""
    import sqlite3

    conn = sqlite3.connect(str(path))
    try:
        pages = [
            r[0]
            for r in conn.execute(
                "SELECT pageno FROM dbstat WHERE name = 'attachment_content'"
                " AND pagetype = 'overflow'"
            )
        ]
    except sqlite3.OperationalError:
        pytest.skip("this SQLite has no dbstat table")
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    conn.close()
    with open(path, "r+b") as fh:
        for pageno in pages:
            fh.seek((pageno - 1) * page_size)
            fh.write(b"\xff" * page_size)
    return len(pages)


def test_choosing_the_rows_never_reads_the_stored_text(store):
    """The mark sits behind extracted_text, 3 GB on the producer: the choice is answered from the
    sweep index (schema v32), as the sweep's own query is."""
    path, conn, root = store
    att = _garbled(conn, root, _email(conn, MESSAGE), MESSAGE)
    conn.execute(
        "UPDATE attachment_content SET extracted_text = ? WHERE attachment_id = ?",
        (garble(GREEK) * 200, att),
    )
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    assert _poison_the_stored_text(path) > 0

    stats = rg.refetch_garbled(str(path), root=str(root), dry_run=True)

    assert stats["to_try"] == 1


# --- the sweep keeps a fetched original until it is read again ---------------------------------


def _letter(conn, root, mailbox):
    """A garbled scan whose original the mailbox still holds, on a store where
    `reextract --partial` has run (otherwise every pymupdf+tesseract row counts as read in part
    and is kept anyway)."""
    conn.execute(
        "INSERT INTO sync_metadata (key, value) VALUES ('reextract_partial_since', '2026-01-01')"
    )
    att = _garbled(
        conn,
        root,
        _email(conn, MESSAGE),
        MESSAGE,
        "letter.pdf",
        mime="application/pdf",
        method="pymupdf+tesseract",
    )
    mailbox.holds(MESSAGE, ("letter.pdf", SCAN))
    return att


def test_the_sweep_keeps_a_fetched_original_until_reextract_reads_it(store, mailbox, monkeypatch):
    path, conn, root = store
    att = _letter(conn, root, mailbox)
    _run(path, root)
    letter = root / MESSAGE / "letter.pdf"

    kept = sweep_files(conn, root, SweepPolicy(apply=True))

    assert letter.exists()
    assert (kept["not-held"], kept["deleted"]) == (1, 0)

    monkeypatch.setattr(
        rx,
        "extract_text_from_file",
        lambda *a, **k: {
            "text": GREEK,
            "method": "pymupdf+tesseract",
            "status": "extracted",
            "error": None,
        },
    )
    monkeypatch.setattr(
        attachment_pipeline,
        "_extract_one_attachment",
        lambda row, *a, **k: (
            row[0],
            row[5],
            {"summary": "a summary", "language": "el"},
            None,
            False,
        ),
    )
    monkeypatch.setattr("src.store.embeddings.remove_vectors", lambda ids: len(ids))
    assert rx.reextract(str(path), {"garbled"}, root=str(root))["reread"] == 1

    swept = sweep_files(conn, root, SweepPolicy(apply=True))

    assert not letter.exists()
    assert swept["deleted"] == 1
    assert _attachment(conn, att)["file_removed_at"] is not None


def test_a_fetched_original_stays_until_read_even_if_its_mark_is_lifted(store, mailbox):
    """The keep-rule is the fetch, not only the mark: an original fetched back stays until a
    reading of it is recorded after the fetch."""
    path, conn, root = store
    att = _letter(conn, root, mailbox)
    _run(path, root)
    key = (MESSAGE, "letter.pdf")
    conn.execute(
        "UPDATE attachment_content SET extraction_error = NULL WHERE attachment_id = ?", (att,)
    )
    conn.commit()

    assert _registered(conn)[key][1] == NOT_HELD

    conn.execute(
        "UPDATE attachment_content SET extracted_at = '9999-01-01T00:00:00'"
        " WHERE attachment_id = ?",
        (att,),
    )
    conn.commit()

    assert _registered(conn)[key][1] == DELETABLE


def test_a_malformed_fetch_record_counts_as_none_and_does_not_stop_the_sweep(store, mailbox):
    """The mark is lifted, so the sweep reaches the record: one that is not JSON is passed over,
    and the other rules decide."""
    path, conn, root = store
    att = _letter(conn, root, mailbox)
    _run(path, root)
    conn.execute(
        "UPDATE sync_metadata SET value = 'not json' WHERE key = ?", (f"refetch_garbled:{att}",)
    )
    conn.execute(
        "UPDATE attachment_content SET extraction_error = NULL WHERE attachment_id = ?", (att,)
    )
    conn.commit()

    assert _registered(conn)[(MESSAGE, "letter.pdf")][1] == DELETABLE


# --- the lookup: mail with no mailbox id, found by folder, time and subject --------------------

# Made-up ids of messages the lookup finds by listing a folder.
LISTED = "AAMkAGQ0TESTLISTEDMESSAGE00000000001AAA="
LISTED_2 = "AAMkAGQ0TESTLISTEDMESSAGE00000000002AAA="

ATHENS = ZoneInfo("Europe/Athens")
# A message received at 08:15:30 UTC in winter, which the retired Apple Mail exporter stored as
# Athens time with no zone, two hours ahead.
WHEN = datetime(2026, 2, 10, 8, 15, 30, tzinfo=UTC)
RECEIVED = "2026-02-10T08:15:30Z"
STORED = "2026-02-10T10:15:30"
# Mail.app's number for the email, which names its attachments' directory.
NUMBER = 48213


def _naive(when: datetime) -> str:
    """`when` as the Apple Mail exporter stored it: Athens time, with no zone."""
    return when.astimezone(ATHENS).replace(tzinfo=None).isoformat()


def _utc(when: datetime) -> str:
    """`when` as the mailbox lists a message's ReceivedDateTime."""
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _apple(
    conn, root, number=NUMBER, stored=STORED, subject="Scanned letter", folder="Archive", **kw
) -> int:
    """A garbled row of an email the Apple Mail exporter stored: Mail.app's number for its id, no
    copy id, and the attachments' directory named for the number."""
    email = _email(conn, number, stored, subject=subject, folder=folder)
    return _garbled(conn, root, email, str(number), **kw)


def test_mail_with_no_mailbox_id_is_found_by_folder_time_and_subject(store, mailbox):
    path, conn, root = store
    att = _apple(conn, root)
    mailbox.files(
        "Archive", LISTED, RECEIVED, "Scanned letter", ("scan.png", SCAN), ("logo.png", LOGO)
    )

    stats = _run(path, root)

    restored = root / str(NUMBER) / "scan.png"
    assert restored.read_bytes() == SCAN
    assert _files(root) == [f"{NUMBER}/scan.png"]
    assert _attachment(conn, att) == {
        "file_path": str(restored),
        "file_removed_at": None,
        "sha256": sha256_of_bytes(SCAN),
    }
    assert (mailbox.listed(), mailbox.asked()) == (["Archive"], [LISTED])
    lookup = stats["lookup"]
    assert (lookup["messages_tried"], lookup["found"], lookup["fetched"]) == (1, 1, 1)
    record = rg.fetch_states(conn)[att]
    assert (record["state"], record["by"], record["via"]) == ("fetched", "lookup", LISTED)
    # Mail.app's number is never sent to outlook-cli.
    assert not any(str(NUMBER) in args for args in mailbox.calls)


def test_a_found_message_without_the_rows_bytes_puts_nothing_back(store, mailbox):
    """The sha256 alone decides: a file of the same name and size is not the row's."""
    path, conn, root = store
    att = _apple(conn, root)
    mailbox.files("Archive", LISTED, RECEIVED, "Scanned letter", ("scan.png", _bytes("another")))

    stats = _run(path, root)
    again = _run(path, root)

    assert _files(root) == []
    lookup = stats["lookup"]
    assert (lookup["found"], lookup["unmatched"], lookup["fetched"]) == (1, 1, 0)
    assert rg.fetch_states(conn)[att]["state"] == "unmatched"
    assert _attachment(conn, att)["file_removed_at"] is not None
    assert again["unmatched_before"] == 1
    assert mailbox.asked() == [LISTED]


def test_several_matching_messages_are_ambiguous_and_none_is_guessed(store, mailbox):
    path, conn, root = store
    att = _apple(conn, root, subject="Scan")
    mailbox.files("Archive", LISTED, RECEIVED, "Scan", ("scan.png", SCAN))
    # A reply forty seconds later has the same subject once normalised.
    later = _utc(WHEN + timedelta(seconds=40))
    mailbox.files("Archive", LISTED_2, later, "RE: scan", ("scan.png", SCAN))

    stats = _run(path, root)
    again = _run(path, root)

    assert (mailbox.asked(), _files(root)) == ([], [])
    assert (stats["lookup"]["ambiguous"], stats["lookup"]["ambiguous_files"]) == (1, 1)
    record = rg.fetch_states(conn)[att]
    assert (record["state"], record["folder"], record["candidates"]) == ("ambiguous", "Archive", 2)
    assert (again["ambiguous_before"], again["lookup"]["messages_tried"]) == (1, 0)
    assert mailbox.listed() == ["Archive"]


def test_no_match_past_the_retention_edge_is_gone_and_not_looked_up_again(store, mailbox):
    path, conn, root = store
    att = _apple(conn, root, stored="2024-05-10T11:00:00")

    stats = _run(path, root)
    again = _run(path, root)

    assert mailbox.listed() == ["Archive", "Sent Items", "Inbox"]
    assert (stats["lookup"]["gone"], stats["lookup"]["gone_files"]) == (1, 1)
    record = rg.fetch_states(conn)[att]
    assert (record["state"], record["by"]) == ("gone", "lookup")
    assert record["gone_at"]
    assert again["gone_before"] == 1


def test_no_match_within_the_retention_edge_is_looked_up_again_three_times(store, mailbox):
    path, conn, root = store
    recent = datetime.now(UTC).replace(microsecond=0) - timedelta(days=30)
    att = _apple(conn, root, stored=_naive(recent))

    runs = [_run(path, root) for _ in range(4)]

    assert [r["lookup"]["not_found"] for r in runs] == [1, 1, 1, 0]
    assert runs[3]["given_up"] == 1
    assert len(mailbox.listed()) == 3 * 3
    record = rg.fetch_states(conn)[att]
    assert (record["state"], record["attempts"]) == ("not_found", 3)


def test_a_failed_lookup_is_looked_up_again_three_attempts_in_all(store, mailbox):
    path, conn, root = store
    att = _apple(conn, root)
    mailbox.list_errors["Archive"] = _unavailable()

    runs = [_run(path, root) for _ in range(4)]

    assert [r["lookup"]["failed"] for r in runs] == [1, 1, 1, 0]
    assert runs[3]["given_up"] == 1
    record = rg.fetch_states(conn)[att]
    assert (record["state"], record["attempts"], record["by"]) == ("failed", 3, "lookup")


def test_a_second_run_looks_up_nothing(store, mailbox):
    path, conn, root = store
    _apple(conn, root)
    mailbox.files("Archive", LISTED, RECEIVED, "Scanned letter", ("scan.png", SCAN))
    _run(path, root)
    calls = len(mailbox.calls)

    again = _run(path, root)

    assert len(mailbox.calls) == calls
    assert again["already_fetched"] == 1
    assert (again["lookup"]["to_try"], again["lookup"]["messages_tried"]) == (0, 0)


@pytest.mark.parametrize(
    ("stored", "received"),
    [
        ("2026-02-10T10:15:30", "2026-02-10T08:15:30Z"),
        ("2026-07-10T10:15:30", "2026-07-10T07:15:30Z"),
        # The hour the clocks go back happens twice: this is its second time round.
        ("2025-10-26T03:30:00", "2025-10-26T01:30:00Z"),
    ],
    ids=["winter", "summer", "clocks-back"],
)
def test_a_stored_time_with_no_zone_is_read_as_athens_time(store, mailbox, stored, received):
    path, conn, root = store
    _apple(conn, root, stored=stored, subject="Scan")
    mailbox.files("Archive", LISTED, received, "Scan", ("scan.png", SCAN))
    # The same subject at the stored time read as UTC, two or three hours off: another message.
    mailbox.files("Archive", LISTED_2, stored + "Z", "Scan", ("scan.png", LOGO))

    stats = _run(path, root)

    assert mailbox.asked() == [LISTED]
    assert stats["lookup"]["fetched"] == 1


def test_only_a_message_in_the_window_with_the_subject_is_a_candidate(store, mailbox):
    path, conn, root = store
    _apple(conn, root)
    inside = WHEN + rg.LOOKUP_WINDOW - timedelta(seconds=30)
    before = WHEN - rg.LOOKUP_WINDOW - timedelta(minutes=1)
    after = WHEN + rg.LOOKUP_WINDOW + timedelta(minutes=1)
    mailbox.files("Archive", LISTED, _utc(inside), "Scanned letter", ("scan.png", SCAN))
    mailbox.files("Archive", OTHER, _utc(before), "Scanned letter", ("scan.png", LOGO))
    mailbox.files("Archive", THIRD, _utc(after), "Scanned letter", ("scan.png", LOGO))
    mailbox.files("Archive", LISTED_2, RECEIVED, "Invoice", ("scan.png", LOGO))

    stats = _run(path, root)

    assert mailbox.asked() == [LISTED]
    assert stats["lookup"]["fetched"] == 1


def test_mail_filed_from_the_inbox_is_found_in_archive(store, mailbox):
    path, conn, root = store
    att = _apple(conn, root, folder="Inbox")
    mailbox.files("Archive", LISTED, RECEIVED, "Scanned letter", ("scan.png", SCAN))

    stats = _run(path, root)

    assert mailbox.listed() == ["Inbox", "Archive"]
    assert stats["lookup"]["fetched"] == 1
    assert rg.fetch_states(conn)[att]["folder"] == "Archive"


def test_the_folder_the_email_records_decides_first(store, mailbox):
    """Sent mail copied to its sender lies in Sent Items and, received, in Archive: the folder
    the email records is looked in first, and a match there is not weighed against the other."""
    path, conn, root = store
    _apple(conn, root, folder="Sent Items")
    mailbox.files("Sent Items", LISTED, RECEIVED, "Scanned letter", ("scan.png", SCAN))
    mailbox.files("Archive", LISTED_2, RECEIVED, "Scanned letter", ("scan.png", SCAN))

    stats = _run(path, root)

    assert (mailbox.listed(), mailbox.asked()) == (["Sent Items"], [LISTED])
    assert stats["lookup"]["fetched"] == 1


def test_a_folder_the_mailbox_lacks_is_looked_past(store, mailbox):
    path, conn, root = store
    _apple(conn, root)
    mailbox.list_errors["Archive"] = _not_found()
    mailbox.files("Sent Items", LISTED, RECEIVED, "Scanned letter", ("scan.png", SCAN))

    stats = _run(path, root)

    assert mailbox.listed() == ["Archive", "Sent Items"]
    assert (stats["lookup"]["fetched"], stats["lookup"]["failed"]) == (1, 0)


def test_a_row_with_nothing_to_look_it_up_by_is_counted_and_not_looked_up(store, mailbox):
    """A file is accepted by its sha256 alone, so a row with no hash is not looked up; nor is an
    email with no subject, or a file with no email."""
    path, conn, root = store
    _apple(conn, root, subject="")
    _apple(conn, root, number=48214, hashed=False)
    _garbled(conn, root, None, "48215")

    stats = _run(path, root)

    assert mailbox.calls == []
    lookup = stats["lookup"]
    assert (stats["no_mailbox_id"], lookup["no_key"], lookup["to_try"]) == (3, 3, 0)
    assert rg.fetch_states(conn) == {}


def test_no_lookup_leaves_mail_with_no_mailbox_id_alone(store, mailbox):
    path, conn, root = store
    att = _apple(conn, root)
    mailbox.files("Archive", LISTED, RECEIVED, "Scanned letter", ("scan.png", SCAN))

    stats = _run(path, root, lookup=False)

    assert mailbox.calls == []
    assert (stats["no_mailbox_id"], stats["lookup"]) == (1, None)
    assert att not in rg.fetch_states(conn)


def test_the_dry_run_counts_both_halves_and_asks_for_nothing(store, mailbox):
    """It counts what a run with the same limit would ask for, and leave: the id half goes first."""
    path, conn, root = store
    _garbled(conn, root, _email(conn, MESSAGE), MESSAGE)
    mailbox.holds(MESSAGE, ("scan.png", SCAN))
    _apple(conn, root)
    mailbox.files("Archive", LISTED, RECEIVED, "Scanned letter", ("scan.png", SCAN))

    stats = _run(path, root, dry_run=True, limit=1)

    assert mailbox.calls == []
    assert (_files(root), rg.fetch_states(conn)) == ([], {})
    assert (stats["to_try"], stats["messages_to_try"], stats["left"]) == (1, 1, 0)
    lookup = stats["lookup"]
    assert (lookup["to_try"], lookup["messages_to_try"], lookup["messages_tried"]) == (1, 1, 0)
    assert lookup["left"] == 1


def test_the_limit_bounds_both_halves_together(store, mailbox):
    path, conn, root = store
    for message_id, received in (
        (MESSAGE, "2026-01-01T00:00:00Z"),
        (OTHER, "2026-01-02T00:00:00Z"),
    ):
        _garbled(conn, root, _email(conn, message_id, received), message_id)
        mailbox.holds(message_id, ("scan.png", SCAN))
    _apple(conn, root)
    _apple(conn, root, number=48214, stored="2026-03-10T10:15:30", subject="Second scan")
    mailbox.files("Archive", LISTED, RECEIVED, "Scanned letter", ("scan.png", SCAN))
    mailbox.files("Archive", LISTED_2, "2026-03-10T08:15:30Z", "Second scan", ("scan.png", SCAN))

    stats = _run(path, root, limit=3)

    assert (stats["messages_tried"], stats["lookup"]["messages_tried"]) == (2, 1)
    assert (stats["left"], stats["lookup"]["left"]) == (0, 1)
    assert mailbox.asked() == [MESSAGE, OTHER, LISTED]


@pytest.mark.parametrize("call", ["list-mail", "download-attachments"])
def test_reauthentication_in_the_lookup_stops_the_run_and_counts_no_attempt(store, mailbox, call):
    path, conn, root = store
    first = _apple(conn, root)
    second = _apple(conn, root, number=48214, stored="2026-03-10T10:15:30", subject="Second scan")
    mailbox.files("Archive", LISTED, RECEIVED, "Scanned letter", ("scan.png", SCAN))
    mailbox.files("Archive", LISTED_2, "2026-03-10T08:15:30Z", "Second scan", ("scan.png", SCAN))
    if call == "list-mail":
        mailbox.list_errors["Archive"] = OutlookCliAuthRequired("session expired")
    else:
        mailbox.errors[LISTED] = OutlookCliAuthRequired("session expired")

    stats = _run(path, root)

    assert (stats["stopped"], stats["stop_exit_code"]) == (True, 4)
    assert mailbox.calls[-1][0] == call
    assert (stats["lookup"]["messages_tried"], stats["lookup"]["left"]) == (0, 2)
    states = rg.fetch_states(conn)
    assert first not in states and second not in states


def test_the_lookup_does_not_start_once_the_id_half_stopped(store, mailbox):
    path, conn, root = store
    _garbled(conn, root, _email(conn, MESSAGE), MESSAGE)
    mailbox.errors[MESSAGE] = OutlookCliAuthRequired("session expired")
    _apple(conn, root)
    mailbox.files("Archive", LISTED, RECEIVED, "Scanned letter", ("scan.png", SCAN))

    stats = _run(path, root)

    assert stats["stopped"] is True
    assert mailbox.listed() == []
    assert stats["lookup"]["left"] == 1


def test_both_halves_make_at_most_two_calls_at_once(store, mailbox):
    path, conn, root = store
    for day, message_id in enumerate((MESSAGE, OTHER, THIRD), start=1):
        _garbled(conn, root, _email(conn, message_id, f"2026-01-0{day}T00:00:00Z"), message_id)
        mailbox.holds(message_id, ("scan.png", SCAN))
    for i in range(3):
        _apple(conn, root, number=NUMBER + i, stored=f"2026-02-1{i}T10:15:30", subject=f"Scan {i}")
        listed = f"AAMkAGQ0TESTLISTEDMESSAGE0000000001{i}AAA="
        mailbox.files("Archive", listed, f"2026-02-1{i}T08:15:30Z", f"Scan {i}", ("scan.png", SCAN))
    mailbox.delay = 0.02

    stats = _run(path, root, workers=8)

    assert mailbox.peak <= rg.MAX_CONCURRENCY
    assert (stats["fetched"], stats["lookup"]["fetched"]) == (3, 3)


def test_the_sweep_keeps_a_looked_up_original_until_it_is_read(store, mailbox):
    """The lookup keeps its record under the same key as the id half, so the sweep's keep-rule
    holds for its files too."""
    path, conn, root = store
    conn.execute(
        "INSERT INTO sync_metadata (key, value) VALUES ('reextract_partial_since', '2026-01-01')"
    )
    att = _apple(conn, root, name="letter.pdf", mime="application/pdf", method="pymupdf+tesseract")
    mailbox.files("Archive", LISTED, RECEIVED, "Scanned letter", ("letter.pdf", SCAN))
    _run(path, root)
    letter = root / str(NUMBER) / "letter.pdf"

    kept = sweep_files(conn, root, SweepPolicy(apply=True))

    assert letter.exists()
    assert (kept["not-held"], kept["deleted"]) == (1, 0)
    conn.execute(
        "UPDATE attachment_content SET extraction_error = NULL WHERE attachment_id = ?", (att,)
    )
    conn.commit()
    assert _registered(conn)[(str(NUMBER), "letter.pdf")][1] == NOT_HELD


# --- the command -------------------------------------------------------------------------------


def _cli(monkeypatch, path, *argv):
    from src import cli

    monkeypatch.setattr(cli, "install_llm_deadline_for_this_process", lambda: None)
    monkeypatch.setattr(sys, "argv", ["brain", "--db", str(path), "refetch-garbled", *argv])
    try:
        cli.main()
    except SystemExit as exc:
        return exc.code
    return 0


def test_the_command_prints_the_counts(store, mailbox, monkeypatch, capsys):
    path, conn, root = store
    _garbled(conn, root, _email(conn, MESSAGE), MESSAGE)
    _garbled(conn, root, _email(conn, OTHER, "2025-05-01T00:00:00Z"), OTHER)
    mailbox.holds(MESSAGE, ("scan.png", SCAN))

    code = _cli(monkeypatch, path, "--root", str(root))

    out = capsys.readouterr().out
    assert code == 0
    for label in ("messages tried", "files fetched", "gone", "failed", "already fetched"):
        assert label in out
    assert "reextract --garbled" in out


def test_the_command_exits_5_when_a_download_failed(store, mailbox, monkeypatch):
    path, conn, root = store
    _garbled(conn, root, _email(conn, MESSAGE), MESSAGE)
    mailbox.errors[MESSAGE] = _unavailable()

    assert _cli(monkeypatch, path, "--root", str(root)) == 5


def test_the_command_exits_4_when_outlook_cli_needs_a_login(store, mailbox, monkeypatch):
    path, conn, root = store
    _garbled(conn, root, _email(conn, MESSAGE), MESSAGE)
    mailbox.errors[MESSAGE] = OutlookCliAuthRequired("session expired")

    assert _cli(monkeypatch, path, "--root", str(root)) == 4


def test_the_command_dry_run_downloads_nothing(store, mailbox, monkeypatch, capsys):
    path, conn, root = store
    _garbled(conn, root, _email(conn, MESSAGE), MESSAGE)

    code = _cli(monkeypatch, path, "--dry-run", "--root", str(root))

    assert code == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert mailbox.calls == []


def test_the_command_is_refused_on_a_replica(store, mailbox, monkeypatch, capsys):
    path, conn, root = store
    _garbled(conn, root, _email(conn, MESSAGE), MESSAGE)
    monkeypatch.setenv("BRAIN_ROLE", "replica")

    code = _cli(monkeypatch, path, "--root", str(root))

    assert code == 2
    assert "BRAIN_ROLE=replica" in capsys.readouterr().err
    assert mailbox.calls == []


def test_the_limit_must_be_positive(store, mailbox, monkeypatch):
    path, _conn, root = store

    assert _cli(monkeypatch, path, "--limit", "0", "--root", str(root)) == 2


def test_the_command_prints_both_halves(store, mailbox, monkeypatch, capsys):
    path, conn, root = store
    _garbled(conn, root, _email(conn, MESSAGE), MESSAGE)
    mailbox.holds(MESSAGE, ("scan.png", SCAN))
    _apple(conn, root)
    mailbox.files("Archive", LISTED, RECEIVED, "Scanned letter", ("scan.png", SCAN))

    code = _cli(monkeypatch, path, "--root", str(root))

    out = capsys.readouterr().out
    assert code == 0
    for label in (
        "By id",
        "By folder, time and subject",
        "messages looked up",
        "several messages matched",
        "not found",
    ):
        assert label in out


def test_the_command_dry_run_prints_both_halves_and_asks_for_nothing(
    store, mailbox, monkeypatch, capsys
):
    path, conn, root = store
    _garbled(conn, root, _email(conn, MESSAGE), MESSAGE)
    _apple(conn, root)

    code = _cli(monkeypatch, path, "--dry-run", "--root", str(root))

    out = capsys.readouterr().out
    assert code == 0
    assert mailbox.calls == []
    for label in ("DRY RUN", "By id", "By folder, time and subject", "to try"):
        assert label in out


def test_the_command_no_lookup_asks_by_id_only(store, mailbox, monkeypatch, capsys):
    path, conn, root = store
    _apple(conn, root)
    mailbox.files("Archive", LISTED, RECEIVED, "Scanned letter", ("scan.png", SCAN))

    code = _cli(monkeypatch, path, "--no-lookup", "--root", str(root))

    assert code == 0
    assert mailbox.calls == []
    assert "no mailbox id" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("error", "code"),
    [(OutlookCliAuthRequired("session expired"), 4), (_unavailable(), 5)],
    ids=["login", "service-error"],
)
def test_the_command_exit_code_covers_the_lookup(store, mailbox, monkeypatch, error, code):
    path, conn, root = store
    _apple(conn, root)
    mailbox.list_errors["Archive"] = error

    assert _cli(monkeypatch, path, "--root", str(root)) == code
