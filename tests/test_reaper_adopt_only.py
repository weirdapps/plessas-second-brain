"""`reap_orphan_attachments.py --adopt-only --apply` loses no bytes, and the full reap keeps
the last copy of anything the store does not hold.

Adopt-only deletes no duplicate and no stray; an adopted file's original is removed once its
copy is registered, which is a move. But adoption also removed the original when
ingest_document answered "already ingested" and named a copy that is not on disk: a text-only
document of the same bytes (a SharePoint fetch, file_path "text:..."), or a document whose
file is gone. Nothing then held the bytes, and if the document's content was not held either
(rights-protected, unreadable) the file was lost. The full reap had the same hole through its
duplicate rule, which counted the hash of a failed, skipped or encrypted row as stored.
"""

import hashlib
from pathlib import Path

import tests.test_orphan_reaper as tor
from src.extract.attachment_pipeline import ingest_text_document

db_path = tor.db_path
db = tor.db
reap = tor.reap_orphan_attachments


def _bytes_on_disk(*roots: Path) -> set[str]:
    return {
        hashlib.sha256(p.read_bytes()).hexdigest()
        for root in roots
        if root.exists()
        for p in root.rglob("*")
        if p.is_file() and p.suffix != ".db"
    }


def _text_only_document(db, body: bytes) -> None:
    """A document holding no file, made of these bytes: a SharePoint fetch kept as text only,
    and read only in part, so its content is not held."""
    ingest_text_document(
        db,
        source="sharepoint",
        key="pack",
        filename="pack.zip",
        mime_type="application/zip",
        text="the first member",
        sha256=hashlib.sha256(body).hexdigest(),
        method="zip",
        status="extracted",
        error="time budget spent, 3 members left unread",
        subject="[SharePoint] pack",
        sender_name="SharePoint",
        date="2026-07-01",
    )


def _row(db, message_id: str, name: str, body: bytes, path: str, status: str) -> None:
    """A registered mail attachment with these bytes, at `path`, read with `status`."""
    db.execute(
        "INSERT INTO emails (message_id, date_received) VALUES (?, '2026-10-01')", (message_id,)
    )
    email_id = db.execute("SELECT id FROM emails WHERE message_id = ?", (message_id,)).fetchone()[0]
    att = db.execute(
        "INSERT INTO attachments (email_id, message_id, filename, file_size, file_path,"
        " exported_at, sha256) VALUES (?, ?, ?, ?, ?, 'now', ?)",
        (email_id, message_id, name, len(body), path, hashlib.sha256(body).hexdigest()),
    ).lastrowid
    db.execute(
        "INSERT INTO attachment_content (attachment_id, extraction_status, llm_status)"
        " VALUES (?, ?, 'pending')",
        (att, status),
    )
    db.commit()


def test_an_adopt_only_run_loses_no_bytes(db, db_path, tmp_path):
    root = tmp_path / "att"
    # A stored duplicate, a unique file, and the bytes of a text-only document that holds
    # no file, all in an abandoned orphan directory.
    _row(db, "AAMk-held", "dup.pdf", b"dup", "/gone/AAMk-held/dup.pdf", "extracted")
    _text_only_document(db, b"pack bytes")
    tor._orphan(
        root,
        "AAMk-gone",
        {"dup.pdf": b"dup", "new.pdf": b"only here", "pack.zip": b"pack bytes"},
        age_days=30,
    )
    # A stray beside a registered file.
    tor._registered(db, root, "AAMk-live", "deck.pdf", b"deck")
    (root / "AAMk-live" / "copy.pdf").write_bytes(b"stray")
    before = _bytes_on_disk(root, tmp_path / "adopted")

    stats = reap(db_path, root, apply=True, adopt_only=True)

    assert before <= _bytes_on_disk(root, tmp_path / "adopted")
    assert stats["deleted"] == 0 and stats["strays_deleted"] == 0
    assert stats["adopted"] == 1 and stats["originals_kept"] == 1


def test_the_full_reap_keeps_an_orphan_whose_twin_is_unheld_and_gone(db, db_path, tmp_path):
    """The hash belongs to a rights-protected attachment whose registered copy is not on disk:
    deleting the orphan as its duplicate would leave the bytes nowhere."""
    root = tmp_path / "att"
    _row(db, "AAMk-moved", "locked.xlsx", b"locked", "/gone/AAMk-moved/locked.xlsx", "encrypted")
    tor._orphan(root, "AAMk-old", {"locked.xlsx": b"locked"}, age_days=30)

    stats = reap(db_path, root, apply=True)

    assert stats["deleted"] == 0
    assert hashlib.sha256(b"locked").hexdigest() in _bytes_on_disk(root, tmp_path / "adopted")


def test_the_full_reap_deletes_an_orphan_whose_unheld_twin_is_on_disk(db, db_path, tmp_path):
    """The registered copy stays (the sweep keeps an encrypted original), so this one is waste."""
    root = tmp_path / "att"
    twin = root / "AAMk-moved" / "locked.xlsx"
    twin.parent.mkdir(parents=True)
    twin.write_bytes(b"locked")
    _row(db, "AAMk-moved", "locked.xlsx", b"locked", str(twin), "encrypted")
    dead = tor._orphan(root, "AAMk-old", {"renamed.xlsx": b"locked"}, age_days=30)

    stats = reap(db_path, root, apply=True)

    assert stats["deleted"] == 1 and stats["adopted"] == 0
    assert not dead.exists() and twin.exists()
