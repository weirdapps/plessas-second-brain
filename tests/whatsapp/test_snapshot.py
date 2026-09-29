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

from tests.whatsapp.conftest import (
    ALICE,
    BOB,
    DIRECT_JID,
    GROUP_JID,
    IDENTITY_KEY,
    make_bridge_store,
    make_contact_store,
)

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
    dest = tmp_path / "whatsapp-snapshot.db"
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
    assert tables == {"chats", "messages", "contacts"}
    raw = dest.read_bytes()
    assert b"mmg.example" not in raw
    assert bytes.fromhex("00112233") not in raw
    assert bytes.fromhex("44556677") not in raw


def test_every_chat_and_message_is_kept(tmp_path):
    source = make_bridge_store(tmp_path / "messages.db", _messages())
    dest = tmp_path / "whatsapp-snapshot.db"
    out = _run(source, dest)
    assert json.loads(out.stdout) == {
        "chats": 2,
        "messages": 3,
        "contacts": 0,
        "bytes": dest.stat().st_size,
    }


def test_the_source_is_read_only_and_left_untouched(tmp_path):
    source = make_bridge_store(tmp_path / "messages.db", _messages())
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    source.chmod(0o400)
    try:
        assert _run(source, tmp_path / "whatsapp-snapshot.db").returncode == 0
    finally:
        source.chmod(0o600)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert not Path(str(source) + "-journal").exists()


def test_the_snapshot_file_is_owner_only(tmp_path):
    source = make_bridge_store(tmp_path / "messages.db", _messages())
    dest = tmp_path / "whatsapp-snapshot.db"
    _run(source, dest)
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600


def test_nothing_it_prints_carries_message_content(tmp_path):
    source = make_bridge_store(tmp_path / "messages.db", _messages())
    out = _run(source, tmp_path / "whatsapp-snapshot.db")
    assert MARKER not in out.stdout + out.stderr
    assert "hello" not in out.stdout + out.stderr


def test_a_missing_source_exits_66(tmp_path):
    out = _run(tmp_path / "absent.db", tmp_path / "whatsapp-snapshot.db")
    assert out.returncode == 66
    assert not (tmp_path / "whatsapp-snapshot.db").exists()


def test_a_store_without_the_expected_columns_exits_65(tmp_path):
    source = tmp_path / "messages.db"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE messages (id TEXT)")
    conn.execute("CREATE TABLE chats (jid TEXT)")
    conn.commit()
    conn.close()
    assert _run(source, tmp_path / "whatsapp-snapshot.db").returncode == 65


def test_an_existing_destination_is_replaced_whole(tmp_path):
    source = make_bridge_store(tmp_path / "messages.db", _messages())
    dest = tmp_path / "whatsapp-snapshot.db"
    dest.write_bytes(b"stale")
    assert _run(source, dest).returncode == 0
    assert json.loads(_run(source, dest).stdout)["messages"] == 3


@pytest.mark.skipif(not os.access("/usr/bin/python3", os.X_OK), reason="no /usr/bin/python3")
def test_it_runs_under_the_system_python_the_mac_job_uses(tmp_path):
    """The LaunchAgent calls /usr/bin/python3, which is 3.9 on macOS."""
    source = make_bridge_store(tmp_path / "messages.db", _messages())
    out = _run(source, tmp_path / "whatsapp-snapshot.db", python="/usr/bin/python3")
    assert out.returncode == 0, out.stderr


# --- People are named from the bridge's contact store -------------------------
# WhatsApp now shows most people as an anonymous id (a "LID"), so the chat names
# the bridge keeps are numbers. The bridge's own store, whatsapp.db beside
# messages.db, knows the names: from the owner's address book, or the name each
# person set. Only an id and a name per person in the snapshot leave the Mac; the
# store also holds the account's encryption keys and is never copied.

CAROL_LID = "100000000000001"
CAROL_PN = "30000000003"


def _people_messages():
    return [
        ("p1", DIRECT_JID, ALICE, "hi", "2026-09-01 10:00:00+03:00", 0, "", ""),
        ("p2", GROUP_JID, f"{BOB}@s.whatsapp.net", "hello", "2026-09-01 10:01:00+03:00", 0, "", ""),
        ("p3", GROUP_JID, f"{CAROL_LID}@lid", "hey", "2026-09-01 10:02:00+03:00", 0, "", ""),
    ]


def _people_contacts():
    return [
        (f"{ALICE}@s.whatsapp.net", "Alice Address", "Alice Self", None),
        (f"{BOB}@s.whatsapp.net", None, "Bob Self", None),
        # The anonymous id carries only the name Carol set; her number's entry has
        # the name the owner saved. The saved name wins, reached through the id map.
        (f"{CAROL_LID}@lid", None, "Carol Self", None),
        (f"{CAROL_PN}@s.whatsapp.net", "Carol Address", None, None),
        # Someone who is not in any chat of this snapshot.
        ("30000000007@s.whatsapp.net", "Nobody Here", None, None),
    ]


def _people_snapshot(tmp_path, contacts=None, lid_map=None):
    source = make_bridge_store(tmp_path / "messages.db", _people_messages())
    make_contact_store(
        tmp_path / "whatsapp.db",
        _people_contacts() if contacts is None else contacts,
        [(CAROL_LID, CAROL_PN)] if lid_map is None else lid_map,
    )
    dest = tmp_path / "whatsapp-snapshot.db"
    out = _run(source, dest)
    return dest, out


def _contacts(dest):
    conn = sqlite3.connect(dest)
    try:
        return dict(conn.execute("SELECT user, name FROM contacts").fetchall())
    finally:
        conn.close()


def test_people_are_named_address_book_first(tmp_path):
    dest, out = _people_snapshot(tmp_path)
    assert out.returncode == 0, out.stderr
    assert _contacts(dest) == {
        ALICE: "Alice Address",
        BOB: "Bob Self",
        CAROL_LID: "Carol Address",
    }
    assert json.loads(out.stdout)["contacts"] == 3


def test_only_people_in_the_snapshot_are_named(tmp_path):
    dest, _ = _people_snapshot(tmp_path)
    assert "30000000007" not in _contacts(dest)
    assert b"Nobody Here" not in dest.read_bytes()


def test_a_number_is_not_taken_for_a_name(tmp_path):
    contacts = [(f"{ALICE}@s.whatsapp.net", None, "+30 000 000 0001", None)]
    dest, _ = _people_snapshot(tmp_path, contacts=contacts, lid_map=[])
    assert _contacts(dest) == {}


def test_the_contact_store_keys_never_leave(tmp_path):
    dest, _ = _people_snapshot(tmp_path)
    conn = sqlite3.connect(dest)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    columns = [r[1] for r in conn.execute("PRAGMA table_info(contacts)")]
    conn.close()
    assert tables == {"chats", "messages", "contacts"}
    assert columns == ["user", "name"]
    assert IDENTITY_KEY not in dest.read_bytes()


def test_names_are_never_printed(tmp_path):
    _, out = _people_snapshot(tmp_path)
    assert "Alice Address" not in out.stdout + out.stderr
    assert "Carol" not in out.stdout + out.stderr


def test_an_unreadable_contact_store_still_builds_without_names(tmp_path):
    source = make_bridge_store(tmp_path / "messages.db", _people_messages())
    (tmp_path / "whatsapp.db").write_bytes(b"not a database")
    dest = tmp_path / "whatsapp-snapshot.db"
    out = _run(source, dest)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["contacts"] == 0
    assert json.loads(out.stdout)["messages"] == 3


@pytest.mark.skipif(not os.access("/usr/bin/python3", os.X_OK), reason="no /usr/bin/python3")
def test_naming_runs_under_the_system_python_the_mac_job_uses(tmp_path):
    source = make_bridge_store(tmp_path / "messages.db", _people_messages())
    make_contact_store(tmp_path / "whatsapp.db", _people_contacts(), [(CAROL_LID, CAROL_PN)])
    dest = tmp_path / "whatsapp-snapshot.db"
    out = _run(source, dest, python="/usr/bin/python3")
    assert out.returncode == 0, out.stderr
    assert _contacts(dest)[CAROL_LID] == "Carol Address"
