"""A newer revision of a pushed document replaces the older one (owner decision 2026-11-15).

A document is keyed on its bytes, so each saved revision of one file in the document inbox
became a new document beside the last: one folder's INDEX.md was held 148 times. With
`reverse-ingest --replace-revisions` (the document inbox only), document_paths maps a file's
folder label and logical name (a dated YYYYMMDDHHMM_ prefix left out, as the scan groups
versions) to its current document, and the newer revision forgets the older one through
forget_documents, as a newer session note does. `brain maint document-revisions` forgets the
revisions stored before, a dry run unless --apply, which needs --backup.
"""

import os
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from src.cli import cmd_reverse_ingest
from src.store import document_revisions as rev
from src.store.schema import create_database


@pytest.fixture
def db_path(tmp_path, monkeypatch) -> str:
    path = tmp_path / "brain.db"
    create_database(str(path)).close()
    monkeypatch.setattr("src.extract.attachment_pipeline.ATTACHMENTS_DIR", tmp_path / "att")
    return str(path)


def _write(path: Path, body: bytes, ts: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    epoch = datetime.strptime(ts, "%Y%m%d%H%M").timestamp()
    os.utime(path, (epoch, epoch))
    return path


def _ingest(db_path: str, root: Path, *, replace: bool = True) -> None:
    args = type(
        "Args",
        (),
        {
            "root": [root],
            "workers": 1,
            "dry_run": False,
            "verbose": False,
            "db": Path(db_path),
            "remove_ingested": True,
            "no_summary": True,
            "replace_revisions": replace,
        },
    )()
    cmd_reverse_ingest(args)


def _documents(db_path: str) -> list[tuple[str, str]]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT e.subject, a.filename FROM emails e JOIN attachments a ON a.email_id = e.id"
            " WHERE e.sender_address = 'external@documents.local' ORDER BY e.date_received"
        ).fetchall()
    finally:
        conn.close()


def _texts(db_path: str) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [r[0] for r in conn.execute("SELECT content FROM emails ORDER BY id")]
    finally:
        conn.close()


def test_v43_adds_document_paths_to_a_v42_store(tmp_path):
    from src.store.schema import get_schema_version, run_migrations, set_schema_version

    conn = create_database(str(tmp_path / "old.db"))
    conn.execute("DROP TABLE document_paths")
    set_schema_version(conn, 42)
    run_migrations(conn)
    assert get_schema_version(conn) == 43
    assert conn.execute("SELECT COUNT(*) FROM document_paths").fetchone()[0] == 0
    conn.close()


def test_the_backfill_dry_run_reads_a_store_without_the_table(tmp_path, capsys):
    conn = create_database(str(tmp_path / "old.db"))
    conn.execute("DROP TABLE document_paths")
    conn.commit()
    conn.close()

    assert rev.main(["--db", str(tmp_path / "old.db")]) == 0
    assert "0 older revision(s)" in capsys.readouterr().out


def test_logical_key_leaves_out_the_dated_prefix():
    assert rev.logical_key("National/units", "202611150900_plan.pptx") == "National/units/plan.pptx"
    assert rev.logical_key("National/units", "plan.pptx") == "National/units/plan.pptx"
    assert rev.logical_key("National", "2026_plan.pptx") == "National/2026_plan.pptx"


def test_a_newer_revision_replaces_the_older_document(db_path, tmp_path):
    inbox = tmp_path / "inbox" / "National"
    _write(inbox / "units" / "INDEX.md", b"first", "202611010900")
    _ingest(db_path, inbox)
    _write(inbox / "units" / "INDEX.md", b"second", "202611020900")
    _ingest(db_path, inbox)

    assert _documents(db_path) == [("[Document] National/units", "INDEX.md")]
    conn = sqlite3.connect(db_path)
    held = conn.execute("SELECT path, message_id FROM document_paths").fetchall()
    current = conn.execute(
        "SELECT message_id FROM emails WHERE sender_address = 'external@documents.local'"
    ).fetchone()[0]
    conn.close()
    assert held == [("National/units/INDEX.md", current)]


def test_a_dated_prefix_is_the_same_file(db_path, tmp_path):
    inbox = tmp_path / "inbox" / "National"
    _write(inbox / "units" / "202611010900_plan.md", b"v1", "202611010900")
    _ingest(db_path, inbox)
    _write(inbox / "units" / "202611050900_plan.md", b"v2", "202611050900")
    _ingest(db_path, inbox)

    assert _documents(db_path) == [("[Document] National/units", "202611050900_plan.md")]


def test_the_same_name_in_another_folder_is_another_file(db_path, tmp_path):
    inbox = tmp_path / "inbox" / "National"
    _write(inbox / "cards" / "README.md", b"cards", "202611010900")
    _write(inbox / "direct" / "README.md", b"direct", "202611010900")
    _ingest(db_path, inbox)

    assert len(_documents(db_path)) == 2


def test_an_older_revision_arriving_late_does_not_replace_the_newer(db_path, tmp_path):
    inbox = tmp_path / "inbox" / "National"
    _write(inbox / "units" / "plan.md", b"newer", "202611050900")
    _ingest(db_path, inbox)
    _write(inbox / "units" / "plan.md", b"older", "202611010900")
    _ingest(db_path, inbox)

    docs = _documents(db_path)
    assert len(docs) == 1
    assert not (inbox / "units" / "plan.md").exists()  # served: it leaves the inbox


def test_a_revision_held_before_the_table_existed_is_replaced_too(db_path, tmp_path):
    # Stored by an ingest without --replace-revisions (the mirror before 2026-10-07).
    old = tmp_path / "old" / "National"
    _write(old / "units" / "INDEX.md", b"mirror copy", "202610010900")
    _ingest(db_path, old, replace=False)
    inbox = tmp_path / "inbox" / "National"
    _write(inbox / "units" / "INDEX.md", b"revised", "202611010900")
    _ingest(db_path, inbox)

    assert len(_documents(db_path)) == 1


def test_without_the_flag_revisions_stay_as_before(db_path, tmp_path):
    inbox = tmp_path / "inbox" / "National"
    _write(inbox / "units" / "INDEX.md", b"first", "202611010900")
    _ingest(db_path, inbox, replace=False)
    _write(inbox / "units" / "INDEX.md", b"second", "202611020900")
    _ingest(db_path, inbox, replace=False)

    assert len(_documents(db_path)) == 2


def test_a_document_another_path_also_names_is_kept(db_path, tmp_path):
    inbox = tmp_path / "inbox" / "National"
    # The same bytes under two names: one document, two paths name it.
    _write(inbox / "units" / "a.md", b"shared", "202611010900")
    _ingest(db_path, inbox)
    _write(inbox / "units" / "b.md", b"shared", "202611010900")
    _ingest(db_path, inbox)
    _write(inbox / "units" / "a.md", b"a revised", "202611020900")
    _ingest(db_path, inbox)

    conn = sqlite3.connect(db_path)
    paths = dict(conn.execute("SELECT path, message_id FROM document_paths"))
    held = {r[0] for r in conn.execute("SELECT message_id FROM emails")}
    conn.close()
    assert paths["National/units/b.md"] in held
    assert paths["National/units/a.md"] in held


# --- the backfill -------------------------------------------------------------------------


def _seed_history(db_path: str, tmp_path: Path) -> None:
    """Revisions stored before: three of one INDEX.md, two of a README, one plan, and two
    different files of one generic name under a label that is no folder of a root."""
    old = tmp_path / "old" / "National"
    for i, ts in enumerate(("202605040900", "202606040900", "202607040900")):
        _write(old / "INDEX.md", f"index {i}".encode(), ts)
        _ingest(db_path, old, replace=False)
    for i, ts in enumerate(("202605040900", "202606040900")):
        _write(old / "cards" / "README.md", f"readme {i}".encode(), ts)
        _ingest(db_path, old, replace=False)
    _write(old / "cards" / "plan.pdf", b"plan", "202605040900")
    _ingest(db_path, old, replace=False)
    other = tmp_path / "mail-images" / "image002"
    for i in range(2):
        _write(other / "image002.txt", f"img {i}".encode(), f"20260{5 + i}040900")
        _ingest(db_path, other, replace=False)


def test_the_backfill_plans_one_kept_per_file_and_forgets_the_rest(db_path, tmp_path):
    _seed_history(db_path, tmp_path)
    conn = sqlite3.connect(db_path)
    plan = rev.plan_backfill(conn, roots=("National",))
    conn.close()

    assert plan.groups == 2
    assert len(plan.forget) == 3
    assert set(plan.keep) == {
        "National/INDEX.md",
        "National/cards/README.md",
        "National/cards/plan.pdf",
    }


def test_the_backfill_dry_run_changes_nothing(db_path, tmp_path, capsys):
    _seed_history(db_path, tmp_path)
    before = _texts(db_path)

    assert rev.main(["--db", db_path, "--root-label", "National"]) == 0
    assert _texts(db_path) == before
    out = capsys.readouterr().out
    assert "3 older revision(s)" in out and "Nothing changed" in out


def test_the_backfill_apply_needs_a_backup(db_path, tmp_path):
    _seed_history(db_path, tmp_path)

    assert rev.main(["--db", db_path, "--root-label", "National", "--apply"]) == 2
    assert len(_documents(db_path)) == 8


def test_the_backfill_apply_keeps_the_newest_and_maps_it(db_path, tmp_path):
    _seed_history(db_path, tmp_path)
    backup = tmp_path / "before.db"

    code = rev.main(
        ["--db", db_path, "--root-label", "National", "--apply", "--backup", str(backup)]
    )

    assert code == 0 and backup.exists()
    docs = _documents(db_path)
    assert len(docs) == 5  # 3 kept under National, the 2 images untouched
    conn = sqlite3.connect(db_path)
    mapped = dict(conn.execute("SELECT path, message_id FROM document_paths"))
    newest = conn.execute(
        "SELECT e.message_id FROM emails e WHERE e.subject = '[Document] National'"
    ).fetchall()
    conn.close()
    assert mapped["National/INDEX.md"] == newest[0][0]
    assert len(mapped) == 3
