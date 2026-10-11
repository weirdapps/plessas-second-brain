"""scripts/scrub_secrets.py masks card numbers, IBANs and passwords too, in the
database and, with --files, in the staged and extracted files beside it.

It reuses src/redact.py, so the maskers reach it with no list of its own. What
it must not do is call a value found because a pattern matched: a card pattern
matches every long number, and only the checks behind it say which are cards.
Every number here is generated at run time (tests/payment_data.py).
"""

import importlib.util
import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from src.store.schema import create_database
from tests.payment_data import card, digits, grouped, iban, not_luhn, printed

SCRIPT = Path(__file__).parent.parent / "scripts" / "scrub_secrets.py"
VISA = card("4", 16)
IBAN = iban("GR", digits(23, 1))


@pytest.fixture
def scrub(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("scrub_secrets", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "DEFAULT_DB", tmp_path / "brain.db")
    return module


def _db(path: Path, *contents: str) -> None:
    conn = create_database(str(path))
    for i, content in enumerate(contents):
        conn.execute(
            "INSERT INTO emails (message_id, date_received, content) VALUES (?, ?, ?)",
            (f"m{i}", "2026-03-01T10:00:00", content),
        )
    conn.commit()
    conn.close()


class TestDatabase:
    def test_a_card_number_is_found_and_masked(self, scrub, tmp_path, capsys):
        _db(tmp_path / "brain.db", f"card {VISA} on file", "nothing here")

        assert scrub.main([]) == 1
        out = capsys.readouterr().out
        assert "emails.content: 1 row" in out and VISA not in out

        assert scrub.main(["--apply"]) == 0
        conn = sqlite3.connect(tmp_path / "brain.db")
        (content,) = conn.execute("SELECT content FROM emails WHERE message_id = 'm0'").fetchone()
        conn.close()
        assert content == f"card {VISA[:6]}[REDACTED:card]{VISA[-4:]} on file"
        assert scrub.main([]) == 0

    def test_a_long_number_that_is_no_card_is_not_reported(self, scrub, tmp_path, capsys):
        """The card pattern matches it; the Luhn check says it is something else.
        Reported, it could never be scrubbed, and every run would exit 1."""
        _db(tmp_path / "brain.db", f"order {not_luhn(VISA)} shipped")

        assert scrub.main([]) == 0
        assert "No credential-shaped values found." in capsys.readouterr().out
        assert scrub.main(["--apply"]) == 0

    def test_an_attachment_file_name_is_left_alone(self, scrub, tmp_path):
        """The registrar knows a file on disk by its name: a masked name would no
        longer match it, and the file would be registered and read again."""
        path = tmp_path / "brain.db"
        _db(path, "the email the attachment came with")
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO attachments (email_id, message_id, filename, mime_type, file_size,"
            " file_path, exported_at) VALUES (1, 1, ?, 'application/pdf', 1, ?, '2026-03-01')",
            (f"{VISA}.pdf", f"/data/attachments/m0/{VISA}.pdf"),
        )
        conn.commit()
        conn.close()

        assert scrub.main([]) == 0
        assert scrub.main(["--apply"]) == 0
        conn = sqlite3.connect(path)
        row = conn.execute("SELECT filename, file_path FROM attachments").fetchone()
        conn.close()
        assert row == (f"{VISA}.pdf", f"/data/attachments/m0/{VISA}.pdf")


def _tree(root: Path) -> dict[str, Path]:
    staging = root / "staging"
    extracted = root / "extracted" / "conversations"
    notes = root / "notes"
    for d in (staging, extracted, notes):
        d.mkdir(parents=True)
    files = {
        "batch": staging / "batch-00001.json",
        "extraction": extracted / "s1.json",
        "note": notes / "meeting.md",
        "clean": notes / "plain.txt",
        "other": notes / "scan.pdf",
    }
    files["batch"].write_text(
        json.dumps(
            {"emails": [{"message_id": "m1", "content": f"card {grouped(VISA)} and IBAN {IBAN}"}]},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    files["extraction"].write_text(
        json.dumps({"key_facts": [f"IBAN {printed(IBAN)}", "Κωδικός: 12345"]}), encoding="utf-8"
    )
    files["note"].write_text("Ατζέντα\r\nPassword: letmein\r\n", encoding="utf-8", newline="")
    files["clean"].write_text("nothing to see\n", encoding="utf-8")
    files["other"].write_bytes(f"%PDF {VISA}".encode())
    for f in files.values():
        f.chmod(0o600)
    return files


def _snapshot(files: dict[str, Path]) -> dict[str, bytes]:
    return {name: path.read_bytes() for name, path in files.items()}


class TestFiles:
    def test_the_dry_run_counts_and_changes_nothing(self, scrub, tmp_path, capsys):
        files = _tree(tmp_path)
        before = _snapshot(files)

        assert scrub.main(["--files", str(tmp_path / "staging"), str(tmp_path / "extracted"),
                           str(tmp_path / "notes"), "--dry-run"]) == 1  # fmt: skip

        out = capsys.readouterr()
        assert f"{tmp_path / 'staging'}: 1 file, 2 values" in out.out
        assert f"{tmp_path / 'extracted'}: 1 file, 1 value" in out.out
        assert f"{tmp_path / 'notes'}: 1 file, 1 value" in out.out
        for secret in (VISA, IBAN, "letmein", grouped(VISA), printed(IBAN)):
            assert secret not in out.out + out.err
        assert _snapshot(files) == before

    def test_apply_masks_in_place_and_keeps_each_file_whole(self, scrub, tmp_path, capsys):
        files = _tree(tmp_path)
        before = _snapshot(files)

        assert scrub.main(["--files", str(tmp_path), "--apply"]) == 0

        out = capsys.readouterr().out
        assert "3 files rewritten, 4 values masked" in out
        batch = json.loads(files["batch"].read_text(encoding="utf-8"))
        content = batch["emails"][0]["content"]
        assert (
            content
            == f"card {VISA[:6]}[REDACTED:card]{VISA[-4:]} and IBAN GR[REDACTED:iban]{IBAN[-4:]}"
        )
        assert files["batch"].read_text(encoding="utf-8").startswith('{\n  "emails"')
        facts = json.loads(files["extraction"].read_text(encoding="utf-8"))["key_facts"]
        assert facts == [f"IBAN GR[REDACTED:iban]{IBAN[-4:]}", "Κωδικός: 12345"]
        assert files["note"].read_bytes() == "Ατζέντα\r\nPassword: [REDACTED:password]\r\n".encode()
        assert files["clean"].read_bytes() == before["clean"]
        assert files["other"].read_bytes() == before["other"]  # not a text file it rewrites
        for name in ("batch", "extraction", "note"):
            assert stat.S_IMODE(files[name].stat().st_mode) == 0o600, name
        assert not list(tmp_path.rglob("*.tmp"))

        assert scrub.main(["--files", str(tmp_path)]) == 0
        assert "No credential-shaped values found." in capsys.readouterr().out

    def test_an_unreadable_file_is_counted_not_fatal(self, scrub, tmp_path, capsys):
        files = _tree(tmp_path)
        broken = tmp_path / "staging" / "batch-00002.json"
        broken.write_text('{"emails": [', encoding="utf-8")
        latin = tmp_path / "notes" / "export.csv"
        latin.write_bytes(b"\xe1\xe2\xe3;" + VISA.encode())

        assert scrub.main(["--files", str(tmp_path), "--apply"]) == 2

        err = capsys.readouterr().err
        assert "2 files could not be read" in err
        assert str(broken) in err and str(latin) in err
        assert broken.read_text(encoding="utf-8") == '{"emails": ['
        assert "[REDACTED:card]" in files["batch"].read_text(encoding="utf-8")  # the rest is done

    def test_a_file_with_another_hard_link_is_not_rewritten(self, scrub, tmp_path, capsys):
        """A rename gives the path a new file and leaves the other link holding the
        old one, so the scrub would report clean while a copy stayed in clear."""
        files = _tree(tmp_path)
        kept = tmp_path / "kept-originals"
        kept.mkdir()
        os.link(files["note"], kept / "meeting.md")

        assert scrub.main(["--files", str(tmp_path / "notes"), "--apply"]) == 1

        err = capsys.readouterr().err
        assert "1 file has other hard links" in err and str(files["note"]) in err
        assert "letmein" in (kept / "meeting.md").read_text(encoding="utf-8")

    @pytest.mark.skipif(os.geteuid() == 0, reason="root lists any directory")
    def test_a_directory_that_cannot_be_listed_is_not_passed_over(self, scrub, tmp_path, capsys):
        _tree(tmp_path)
        locked = tmp_path / "notes" / "locked"
        locked.mkdir()
        locked.chmod(0)
        try:
            assert scrub.main(["--files", str(tmp_path / "notes")]) == 2
        finally:
            locked.chmod(0o700)
        assert str(locked) in capsys.readouterr().err

    def test_a_missing_directory_is_an_error(self, scrub, tmp_path):
        assert scrub.main(["--files", str(tmp_path / "absent")]) == 2

    def test_the_flags_that_do_not_go_together_are_refused(self, scrub, tmp_path):
        for argv in (["--files", str(tmp_path), "--vacuum", "--apply"], ["--apply", "--dry-run"]):
            with pytest.raises(SystemExit) as exc:
                scrub.main(argv)
            assert exc.value.code == 2

    def test_the_database_is_not_opened(self, scrub, tmp_path, monkeypatch):
        _tree(tmp_path)
        monkeypatch.setattr(scrub.sqlite3, "connect", lambda *a, **k: pytest.fail("opened"))
        assert scrub.main(["--files", str(tmp_path), "--apply"]) == 0

    def test_apply_is_refused_on_a_replica(self, scrub, tmp_path, monkeypatch):
        files = _tree(tmp_path)
        before = _snapshot(files)
        monkeypatch.setenv("BRAIN_ROLE", "replica")

        assert scrub.main(["--files", str(tmp_path), "--apply"]) == 3
        assert _snapshot(files) == before
        assert scrub.main(["--files", str(tmp_path)]) == 1  # the dry run still reads
