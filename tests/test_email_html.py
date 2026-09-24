"""Email bodies hold the text a reader sees; the HTML is kept compressed beside it.

19,753 Outlook emails held 963 MB of raw HTML, two thirds of emails.content:
styles, tables and markup were indexed as words and sent to the extraction
prompt, whose character cap then cut the text. The SharePoint link scan and the
inline-image positions read the markup, so it is kept (schema v23).
"""

import argparse
import sqlite3
from unittest.mock import patch

from src.store.email_html import markup_or_text, pack, split_body, unpack
from src.store.schema import create_database, get_connection, run_migrations

LINK = "https://contoso.sharepoint.com/sites/x/deck.pptx"
HTML = (
    "<html><head><style>p { color: red; }</style></head><body><p>Καλησπέρα,</p>"
    f'<p>the deck: <a href="{LINK}">deck</a></p>'
    + "<p>notes</p>" * 20
    + '<p><img src="cid:chart.png"></p></body></html>'
)
TEXT = f"Καλησπέρα,\n\nthe deck: deck ({LINK})\n\n" + "\n\n".join(["notes"] * 20)
# A shape fixture, not a credential (as in tests/test_scrub_secrets.py).
ANTHROPIC = "sk-ant-api03-" + "a1B2c3D4e5" * 9


def test_the_body_is_split_into_text_and_the_html_kept_beside_it():
    assert split_body(HTML) == (TEXT, HTML)
    assert split_body("plain text") == ("plain text", None)
    assert split_body(None) == (None, None)


def test_the_kept_html_is_compressed_and_read_back_whole():
    blob = pack(HTML)

    assert len(blob) < len(HTML.encode("utf-8"))
    assert unpack(blob) == HTML
    assert markup_or_text(TEXT, blob) == HTML
    assert markup_or_text(TEXT, None) == TEXT


def _load(conn, content, message_id="m1"):
    from src.store.loader import load_single_email

    metadata = {
        "message_id": message_id,
        "date_received": "2026-09-01T00:00:00Z",
        "subject": "Deck",
        "sender": {"name": "A", "address": "a@example.com"},
        "to_recipients": [],
        "cc_recipients": [],
        "mailbox_name": "Inbox",
        "content": content,
    }
    assert load_single_email(conn, metadata, {"summary": "s"})
    return conn.execute(
        "SELECT id, content FROM emails WHERE message_id = ?", (message_id,)
    ).fetchone()


def test_the_loader_stores_the_text_and_keeps_the_html(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))

    email_id, content = _load(conn, HTML)

    assert content == TEXT
    (blob,) = conn.execute("SELECT html FROM email_html WHERE email_id = ?", (email_id,)).fetchone()
    assert unpack(blob) == HTML


def test_a_text_body_is_stored_as_it_came(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))

    _email_id, content = _load(conn, "just text")

    assert content == "just text"
    assert conn.execute("SELECT count(*) FROM email_html").fetchone()[0] == 0


def test_the_body_index_holds_words_not_markup(tmp_path):
    conn = create_database(str(tmp_path / "b.db"))
    _load(conn, HTML)

    def hits(word):
        return conn.execute(
            "SELECT count(*) FROM emails_fts WHERE emails_fts.content_f MATCH ?", (word,)
        ).fetchone()[0]

    assert hits("deck") == 1
    assert hits("color") == 0


def test_the_extraction_prompt_reads_the_text_not_the_markup():
    from src.extract.prompt import MAX_CONTENT_CHARS, build_extraction_prompt

    html = "<html><body>" + "<div style='x'>" * 10000 + "<p>the decision is last</p></body></html>"
    assert len(html) > MAX_CONTENT_CHARS

    prompt = build_extraction_prompt({"message_id": "m", "subject": "s", "content": html})

    assert "the decision is last" in prompt
    assert "<div" not in prompt
    assert "truncated" not in prompt


def test_the_sharepoint_scan_reads_links_from_the_kept_html(tmp_path, monkeypatch):
    from src.cli import cmd_process_sharepoint
    from src.export.sharepoint_fetcher import SharepointFetchResult

    monkeypatch.setenv("SHAREPOINT_HOST", "contoso.sharepoint.com")
    db = tmp_path / "test.db"
    conn = create_database(str(db))
    _load(conn, HTML)
    conn.commit()
    conn.close()

    with patch("src.export.sharepoint_fetcher.fetch_sharepoint_link") as fetch:
        fetch.return_value = SharepointFetchResult(url=LINK, status="stale")
        cmd_process_sharepoint(argparse.Namespace(db=str(db), since=None, limit=0, dry_run=False))

    assert [c.args[0] for c in fetch.call_args_list] == [LINK]


def test_inline_image_positions_come_from_the_kept_html(tmp_path, monkeypatch):
    from PIL import Image

    from src.extract import image_pipeline

    conn = create_database(str(tmp_path / "b.db"))
    email_id, _ = _load(conn, HTML)
    image = tmp_path / "chart.png"
    Image.new("RGB", (200, 200), color="red").save(image, "PNG")
    conn.execute(
        "INSERT INTO attachments (email_id, message_id, filename, file_path, mime_type, "
        "file_size, exported_at) VALUES (?, 'm1', 'chart.png', ?, 'image/png', ?, "
        "datetime('now'))",
        (email_id, str(image), image.stat().st_size),
    )
    conn.commit()
    seen = []
    monkeypatch.setattr(
        image_pipeline,
        "process_single_image",
        lambda **kw: seen.append(kw["position_in_body"]) or {"status": "processed"},
    )

    image_pipeline.run_backfill(conn=conn, run_vision=False)

    assert len(seen) == 1
    assert seen[0] > 0.9  # the cid sits at the end of the markup; the text has none


def _html_store(tmp_path):
    db = tmp_path / "b.db"
    conn = create_database(str(db))
    conn.execute(
        "INSERT INTO emails (id, message_id, date_received, subject, content) "
        "VALUES (1, 'a', '2026-09-01', 's', ?), (2, 'b', '2026-09-01', 's', 'plain')",
        (HTML,),
    )
    conn.commit()
    conn.close()
    return db


def test_split_html_converts_the_emails_loaded_before_it(tmp_path, capsys):
    from src.cli import cmd_split_html

    db = _html_store(tmp_path)

    assert cmd_split_html(argparse.Namespace(db=str(db), batch=1, dry_run=False)) == 0

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT content FROM emails WHERE id = 1").fetchone()[0] == TEXT
    assert conn.execute("SELECT content FROM emails WHERE id = 2").fetchone()[0] == "plain"
    blob = conn.execute("SELECT html FROM email_html WHERE email_id = 1").fetchone()[0]
    assert unpack(blob) == HTML
    assert (
        conn.execute(
            "SELECT count(*) FROM emails_fts WHERE emails_fts.content_f MATCH 'color'"
        ).fetchone()[0]
        == 0
    )
    conn.close()
    assert "1 email" in capsys.readouterr().out

    assert cmd_split_html(argparse.Namespace(db=str(db), batch=1, dry_run=False)) == 0
    assert "0 emails" in capsys.readouterr().out


def test_split_html_converts_every_email_of_a_batch(tmp_path, capsys):
    from src.cli import cmd_split_html

    db = tmp_path / "b.db"
    conn = create_database(str(db))
    for i in range(1, 6):
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received, subject, content) "
            "VALUES (?, ?, '2026-09-01', 's', ?)",
            (i, f"m{i}", HTML if i != 3 else "plain"),
        )
    conn.commit()
    conn.close()

    assert cmd_split_html(argparse.Namespace(db=str(db), batch=2, dry_run=False)) == 0

    conn = sqlite3.connect(db)
    assert [r[0] for r in conn.execute("SELECT email_id FROM email_html ORDER BY 1")] == [
        1,
        2,
        4,
        5,
    ]
    assert {r[0] for r in conn.execute("SELECT content FROM emails WHERE id != 3")} == {TEXT}
    assert "converted 4 emails:" in capsys.readouterr().out


def test_split_html_leaves_a_body_that_changed_under_it(tmp_path, monkeypatch):
    """A batch is read and converted before the write lock is taken, so the
    timers sharing the database wait for the writes only. A body re-loaded in
    between is left as the re-load wrote it."""
    from src.cli import cmd_split_html
    from src.store import email_html

    db = _html_store(tmp_path)
    convert = email_html.split_body

    def racing(content):
        other = sqlite3.connect(db)
        other.execute("UPDATE emails SET content = 'reloaded' WHERE id = 1")
        other.commit()
        other.close()
        return convert(content)

    monkeypatch.setattr(email_html, "split_body", racing)

    assert cmd_split_html(argparse.Namespace(db=str(db), batch=10, dry_run=False)) == 0

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT content FROM emails WHERE id = 1").fetchone()[0] == "reloaded"
    assert conn.execute("SELECT count(*) FROM email_html").fetchone()[0] == 0


def test_split_html_never_converts_an_email_twice(tmp_path):
    """Text converted from HTML can itself open like markup (an escaped &lt;p&gt;).
    A second run must leave it and its kept HTML alone."""
    from src.cli import cmd_split_html

    html = "<html><body>&lt;p&gt; marks a paragraph</body></html>"
    db = tmp_path / "b.db"
    conn = create_database(str(db))
    conn.execute(
        "INSERT INTO emails (id, message_id, date_received, subject, content) "
        "VALUES (1, 'a', '2026-09-01', 's', ?)",
        (html,),
    )
    conn.commit()
    conn.close()

    for _ in range(2):
        assert cmd_split_html(argparse.Namespace(db=str(db), batch=10, dry_run=False)) == 0

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT content FROM emails").fetchone()[0] == "<p> marks a paragraph"
    assert unpack(conn.execute("SELECT html FROM email_html").fetchone()[0]) == html


def test_split_html_dry_run_changes_nothing(tmp_path, capsys):
    from src.cli import cmd_split_html

    db = _html_store(tmp_path)

    assert cmd_split_html(argparse.Namespace(db=str(db), batch=100, dry_run=True)) == 0

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT content FROM emails WHERE id = 1").fetchone()[0] == HTML
    assert conn.execute("SELECT count(*) FROM email_html").fetchone()[0] == 0
    assert "would" in capsys.readouterr().out


def test_split_html_dry_run_does_not_migrate_an_older_store(tmp_path, capsys):
    """A rehearsal on a copy of a v22 store must leave it at v22."""
    from src.cli import cmd_split_html

    db = _html_store(tmp_path)
    conn = get_connection(str(db))
    conn.execute("DROP TABLE email_html")
    conn.execute("UPDATE schema_version SET version = 22")
    conn.commit()
    conn.close()

    assert cmd_split_html(argparse.Namespace(db=str(db), batch=100, dry_run=True)) == 0

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 22
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'email_html'").fetchone()
    assert "would convert 1 email:" in capsys.readouterr().out


def test_split_html_redacts_what_it_keeps(tmp_path):
    """A body loaded before the ingest path redacted (#55) can hold a key. Moved
    into email_html it would be compressed, out of sight of the index, of a grep
    of the file and of any scan of text columns."""
    from src.cli import cmd_split_html

    db = tmp_path / "b.db"
    conn = create_database(str(db))
    conn.execute(
        "INSERT INTO emails (id, message_id, date_received, subject, content) "
        "VALUES (1, 'a', '2026-09-01', 's', ?)",
        (f"<html><body><p>the key is {ANTHROPIC}</p></body></html>",),
    )
    conn.commit()
    conn.close()

    assert cmd_split_html(argparse.Namespace(db=str(db), batch=10, dry_run=False)) == 0

    conn = sqlite3.connect(db)
    content = conn.execute("SELECT content FROM emails WHERE id = 1").fetchone()[0]
    html = unpack(conn.execute("SELECT html FROM email_html WHERE email_id = 1").fetchone()[0])
    assert content == "the key is [REDACTED:anthropic-key]"
    assert html == "<html><body><p>the key is [REDACTED:anthropic-key]</p></body></html>"


def test_split_html_finds_a_body_behind_a_byte_order_mark(tmp_path):
    """Its SQL prefilter skips the same leading characters looks_like_html does."""
    from src.cli import cmd_split_html

    db = tmp_path / "b.db"
    conn = create_database(str(db))
    conn.execute(
        "INSERT INTO emails (id, message_id, date_received, subject, content) "
        "VALUES (1, 'a', '2026-09-01', 's', ?), (2, 'b', '2026-09-01', 's', ?)",
        ("\ufeff\r\n\t " + HTML, "From: Petros <p.petrou@example.com>"),
    )
    conn.commit()
    conn.close()

    assert cmd_split_html(argparse.Namespace(db=str(db), batch=10, dry_run=False)) == 0

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT content FROM emails WHERE id = 1").fetchone()[0] == TEXT
    assert conn.execute("SELECT content FROM emails WHERE id = 2").fetchone()[0] == (
        "From: Petros <p.petrou@example.com>"
    )
    assert conn.execute("SELECT count(*) FROM email_html").fetchone()[0] == 1


def test_an_older_store_gets_the_table(tmp_path):
    path = tmp_path / "b.db"
    create_database(str(path)).close()
    conn = get_connection(str(path))
    conn.execute("DROP TABLE email_html")
    conn.execute("UPDATE schema_version SET version = 22")
    conn.commit()

    run_migrations(conn)

    assert conn.execute("SELECT count(*) FROM email_html").fetchone()[0] == 0
