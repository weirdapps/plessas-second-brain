"""scripts/whatsapp_snapshot.py: the minimized copy that is all that leaves the Mac.

The bridge's messages.db holds, per media message, the CDN URL, the media key
and the file hashes: everything needed to download and decrypt that photo. The
producer needs none of it, so the snapshot is built column by column from an
allowlist and the test reads the file's raw bytes to prove none of it survived.
"""

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tests.whatsapp.conftest import ALICE, DIRECT_JID, GROUP_JID, make_bridge_store

SCRIPT = Path(__file__).parents[2] / "scripts" / "whatsapp_snapshot.py"
MARKER = "zqx-private-marker-7731"


def _messages():
    return [
        ("m1", DIRECT_JID, ALICE, f"hello {MARKER}", "2026-09-01 10:00:00+03:00", 0, "", ""),
        ("m2", GROUP_JID, ALICE, "", "2026-09-01 10:05:00+03:00", 0, "image", "photo.jpg"),
        ("m3", GROUP_JID, "30000000009", "see you", "2026-09-01 10:06:00+03:00", 1, "", ""),
    ]


def _run(source: Path, dest: Path, python: str = sys.executable):
    return subprocess.run(
        [python, str(SCRIPT), str(source), str(dest)], capture_output=True, text=True
    )


def test_the_snapshot_has_no_media_key_url_or_file_hash_column(tmp_path):
    source = make_bridge_store(tmp_path / "messages.db", _messages())
    dest = tmp_path / "snapshot.db"
    assert _run(source, dest).returncode == 0

    conn = sqlite3.connect(dest)
    msg_cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)")]
    chat_cols = [r[1] for r in conn.execute("PRAGMA table_info(chats)")]
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert msg_cols == [
        "id", "chat_jid", "sender", "content", "timestamp", "is_from_me", "media_type", "filename",
    ]  # fmt: skip
    assert chat_cols == ["jid", "name"]
    assert tables == {"chats", "messages"}
    raw = dest.read_bytes()
    assert b"mmg.example" not in raw
    assert bytes.fromhex("00112233") not in raw
    assert bytes.fromhex("44556677") not in raw


def test_every_chat_and_message_is_kept(tmp_path):
    source = make_bridge_store(tmp_path / "messages.db", _messages())
    dest = tmp_path / "snapshot.db"
    out = _run(source, dest)
    assert json.loads(out.stdout) == {"chats": 2, "messages": 3, "bytes": dest.stat().st_size}


def test_the_source_is_read_only_and_left_untouched(tmp_path):
    source = make_bridge_store(tmp_path / "messages.db", _messages())
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    source.chmod(0o400)
    try:
        assert _run(source, tmp_path / "snapshot.db").returncode == 0
    finally:
        source.chmod(0o600)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert not Path(str(source) + "-journal").exists()


def test_the_snapshot_file_is_owner_only(tmp_path):
    source = make_bridge_store(tmp_path / "messages.db", _messages())
    dest = tmp_path / "snapshot.db"
    _run(source, dest)
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600


def test_nothing_it_prints_carries_message_content(tmp_path):
    source = make_bridge_store(tmp_path / "messages.db", _messages())
    out = _run(source, tmp_path / "snapshot.db")
    assert MARKER not in out.stdout + out.stderr
    assert "hello" not in out.stdout + out.stderr


def test_a_missing_source_exits_66(tmp_path):
    out = _run(tmp_path / "absent.db", tmp_path / "snapshot.db")
    assert out.returncode == 66
    assert not (tmp_path / "snapshot.db").exists()


def test_a_store_without_the_expected_columns_exits_65(tmp_path):
    source = tmp_path / "messages.db"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE messages (id TEXT)")
    conn.execute("CREATE TABLE chats (jid TEXT)")
    conn.commit()
    conn.close()
    assert _run(source, tmp_path / "snapshot.db").returncode == 65


def test_an_existing_destination_is_replaced_whole(tmp_path):
    source = make_bridge_store(tmp_path / "messages.db", _messages())
    dest = tmp_path / "snapshot.db"
    dest.write_bytes(b"stale")
    assert _run(source, dest).returncode == 0
    assert json.loads(_run(source, dest).stdout)["messages"] == 3


@pytest.mark.skipif(not os.access("/usr/bin/python3", os.X_OK), reason="no /usr/bin/python3")
def test_it_runs_under_the_system_python_the_mac_job_uses(tmp_path):
    """The LaunchAgent calls /usr/bin/python3, which is 3.9 on macOS."""
    source = make_bridge_store(tmp_path / "messages.db", _messages())
    out = _run(source, tmp_path / "snapshot.db", python="/usr/bin/python3")
    assert out.returncode == 0, out.stderr
