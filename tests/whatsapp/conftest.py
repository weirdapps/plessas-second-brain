"""Shared fixtures for the WhatsApp source tests.

Every value here is invented: chat names like "Chat A", numbers from the
30000000000 range, which no real Greek mobile starts with. The repository is
public, and the source these tests describe is someone's private messages.
"""

import sqlite3
from pathlib import Path

import pytest

from src.store.schema import create_database

ALICE = "30000000001"
BOB = "30000000002"
OWNER = "30000000009"
DIRECT_JID = f"{ALICE}@s.whatsapp.net"
GROUP_JID = "120000000000000001@g.us"


@pytest.fixture
def db(tmp_path):
    """A fresh store with every migration applied."""
    conn = create_database(str(tmp_path / "brain.db"))
    yield conn
    conn.close()


def make_bridge_store(path: Path, messages: list[tuple], chats: list[tuple] | None = None) -> Path:
    """A messages.db with the bridge's real schema, media-key columns included.

    messages: (id, chat_jid, sender, content, timestamp, is_from_me, media_type,
    filename) tuples; the media columns are filled with values a snapshot must
    never carry.
    """
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP);
        CREATE TABLE messages (
            id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP,
            is_from_me BOOLEAN, media_type TEXT, filename TEXT, url TEXT,
            media_key BLOB, file_sha256 BLOB, file_enc_sha256 BLOB, file_length INTEGER,
            PRIMARY KEY (id, chat_jid),
            FOREIGN KEY (chat_jid) REFERENCES chats(jid)
        );
        """
    )
    default_chats = [(DIRECT_JID, "Alice Example"), (GROUP_JID, "Chat A")]
    for jid, name in chats if chats is not None else default_chats:
        conn.execute("INSERT INTO chats (jid, name) VALUES (?, ?)", (jid, name))
    for m in messages:
        conn.execute(
            "INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me, "
            "media_type, filename, url, media_key, file_sha256, file_enc_sha256, file_length) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'https://mmg.example/enc', x'00112233', "
            "x'44556677', x'8899aabb', 1234)",
            m,
        )
    conn.commit()
    conn.close()
    return path


def build_snapshot(tmp_path: Path, messages: list[tuple], chats: list[tuple] | None = None) -> Path:
    """The snapshot the Mac would push: the real builder over a fake bridge store."""
    import importlib.util

    script = Path(__file__).parents[2] / "scripts" / "whatsapp_snapshot.py"
    spec = importlib.util.spec_from_file_location("whatsapp_snapshot", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = make_bridge_store(tmp_path / "bridge.db", messages, chats)
    dest = tmp_path / "whatsapp-snapshot.db"
    module.build_snapshot(str(source), str(dest))
    source.unlink()
    return dest
