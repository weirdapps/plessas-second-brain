"""The scrub must never rewrite identifier columns.

A message id, conversation id or hash that happens to be a 16-digit Luhn-valid
string is a key other rows point at. Masking it would orphan them, so the scan
skips every identifier-shaped column and reads only content."""

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from tests.payment_data import card

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "scrub_secrets.py"


@pytest.fixture
def scrub():
    spec = importlib.util.spec_from_file_location("scrub_secrets_ids", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "ids.db")
    conn.executescript(
        """
        CREATE TABLE emails (
            id INTEGER PRIMARY KEY, message_id TEXT, internet_message_id TEXT,
            conversation_id TEXT, "references" TEXT, change_key TEXT, extraction_hash TEXT,
            web_link TEXT, subject TEXT, content TEXT
        );
        CREATE TABLE whatsapp_messages (message_id TEXT PRIMARY KEY, chat_jid TEXT, content TEXT);
        CREATE TABLE inline_image_occurrences (message_id TEXT, image_sha256 TEXT, position INTEGER);
        """
    )
    return conn


def test_identifier_columns_are_not_scanned(scrub, tmp_path):
    conn = _db(tmp_path)
    cols = set(scrub._text_columns(conn))
    assert ("emails", "content") in cols
    assert ("emails", "subject") in cols
    assert ("whatsapp_messages", "content") in cols
    for skipped in (
        ("emails", "message_id"),
        ("emails", "internet_message_id"),
        ("emails", "conversation_id"),
        ("emails", "references"),
        ("emails", "change_key"),
        ("emails", "extraction_hash"),
        ("emails", "web_link"),
        ("whatsapp_messages", "message_id"),
        ("whatsapp_messages", "chat_jid"),
        ("inline_image_occurrences", "message_id"),
        ("inline_image_occurrences", "image_sha256"),
    ):
        assert skipped not in cols, skipped


def test_scan_reports_content_but_leaves_a_card_shaped_id_alone(scrub, tmp_path):
    conn = _db(tmp_path)
    pan = card("5", 16)
    conn.execute(
        "INSERT INTO emails (message_id, conversation_id, subject, content) VALUES (?, ?, ?, ?)",
        (pan, pan, "hello", f"card {pan} on file"),
    )
    conn.execute(
        "INSERT INTO whatsapp_messages VALUES (?, ?, ?)", (pan, "x@s.whatsapp.net", "nothing")
    )
    conn.commit()
    found, _unreadable = scrub._scan(conn)
    assert set(found) == {("emails", "content")}
    assert found[("emails", "content")] == [1]
