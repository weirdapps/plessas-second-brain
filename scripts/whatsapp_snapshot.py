#!/usr/bin/env python3
"""Build the minimized WhatsApp snapshot the producer ingests.

    whatsapp_snapshot.py SOURCE DEST

SOURCE is the WhatsApp bridge's messages.db, opened read-only. DEST is written
from scratch with three tables and exactly the columns listed below, and nothing
else: the bridge also keeps, for every media message, the CDN URL, the media key
and the file hashes, which together download and decrypt that photo or voice
note. The producer needs none of it, so none of it leaves this machine.

The third table names people. WhatsApp shows most of them as an anonymous id,
so the chat names the bridge keeps are numbers; the bridge's own store,
whatsapp.db beside messages.db, knows their names. It is opened read-only too,
and only a user id and a display name per person who appears in the snapshot
leave this machine: the store also holds the account's encryption keys. When it
is missing or unreadable the snapshot is built without names.

Prints one JSON line of counts ({"chats", "messages", "contacts", "bytes"}) and
never a message, a name or a number, because its output lands in a launchd log.

Stdlib only and Python 3.9 compatible: scripts/wrappers/launchd/
sync-whatsapp-to-vps.sh runs it with /usr/bin/python3, which is 3.9 on macOS.

Both paths are checked before either is touched, because building a snapshot
replaces whatever is at DEST and writes message text there: DEST must be
whatsapp-snapshot.db inside a private directory of this user's under the temp
directory (the wrapper's mktemp -d), and SOURCE an existing messages.db, opened
read-only through a URI that pathlib builds, so no character in either path can
act as a URI parameter.

Exit codes: 0 done, 64 DEST is not a private temp path for the snapshot, 65 the
source lacks a column this needs, 66 the source is missing or unreadable.
"""

import json
import os
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path

# The allowlist. Anything the bridge adds later stays behind unless it is named here.
CHAT_COLUMNS = ("jid", "name")
MESSAGE_COLUMNS = (
    "id",
    "chat_jid",
    "sender",
    "content",
    "timestamp",
    "is_from_me",
    "media_type",
    "filename",
)

SNAPSHOT_NAME = "whatsapp-snapshot.db"
SOURCE_NAME = "messages.db"
CONTACT_STORE_NAME = "whatsapp.db"

EX_USAGE = 64
EX_DATAERR = 65
EX_NOINPUT = 66

_SCHEMA = """
CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT);
CREATE TABLE messages (
    id TEXT,
    chat_jid TEXT,
    sender TEXT,
    content TEXT,
    timestamp TEXT,
    is_from_me INTEGER,
    media_type TEXT,
    filename TEXT,
    PRIMARY KEY (id, chat_jid)
);
CREATE TABLE contacts (user TEXT PRIMARY KEY, name TEXT NOT NULL);
"""


class SnapshotError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _missing_columns(conn, table, wanted):
    have = {row[1] for row in conn.execute(f"PRAGMA src.table_info({table})")}
    return [c for c in wanted if c not in have]


def _checked_source(source):
    """The bridge store, resolved: an existing regular file named messages.db."""
    path = os.path.realpath(source)
    if os.path.basename(path) != SOURCE_NAME or not os.path.isfile(path):
        raise SnapshotError(EX_NOINPUT, "source store not found")
    return path


def _checked_dest(dest):
    """Where a snapshot may be written: whatsapp-snapshot.db in a private directory
    of this user's strictly under the temp directory.

    Anywhere else is refused. In a shared directory the snapshot would expose the
    messages it holds, and at any other file name building it would delete that file.
    """
    path = os.path.realpath(dest)
    parent = os.path.dirname(path)
    temp_root = os.path.realpath(tempfile.gettempdir())
    if (
        os.path.basename(path) != SNAPSHOT_NAME
        or parent == temp_root
        or os.path.commonpath([parent, temp_root]) != temp_root
    ):
        raise SnapshotError(EX_USAGE, f"DEST must be {SNAPSHOT_NAME} in a private temp directory")
    try:
        st = os.stat(parent)
    except OSError:
        raise SnapshotError(EX_USAGE, "DEST's directory does not exist") from None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise SnapshotError(EX_USAGE, "DEST's directory must be private to this user")
    return path


def _user(jid):
    """The user part of a JID ('30000000001@s.whatsapp.net' -> '30000000001')."""
    return (jid or "").split("@", 1)[0].split(":", 1)[0]


def _is_name(value):
    """A real name, not an empty string or a bare number standing in for one."""
    stripped = (value or "").strip().lstrip("+").replace(" ", "")
    return bool(stripped) and not stripped.isdigit()


def _copy_contacts(conn, source):
    """Name the people in the snapshot from the bridge's contact store; return how many.

    The name the owner saved in the address book comes first, then the name the
    person set in WhatsApp, then a business name. An anonymous id and the phone
    number it maps to are one person, so each is tried with the other's entry
    too. Returns 0, and names nobody, when the store is missing or unreadable.
    """
    store = os.path.join(os.path.dirname(source), CONTACT_STORE_NAME)
    if not os.path.isfile(store):
        return 0
    try:
        conn.execute("ATTACH DATABASE ? AS wa", (Path(store).as_uri() + "?mode=ro",))
    except sqlite3.Error:
        return 0
    try:
        rows = conn.execute(
            "SELECT their_jid, full_name, push_name, business_name FROM wa.whatsmeow_contacts"
        ).fetchall()
        try:
            pairs = conn.execute("SELECT lid, pn FROM wa.whatsmeow_lid_map").fetchall()
        except sqlite3.Error:
            pairs = []
    except sqlite3.Error:
        return 0
    finally:
        try:
            conn.execute("DETACH DATABASE wa")
        except sqlite3.Error:
            pass

    entries = {_user(jid): (full, push, business) for jid, full, push, business in rows}
    same_person = {}
    for lid, pn in pairs:
        same_person[_user(lid)] = _user(pn)
        same_person[_user(pn)] = _user(lid)
    wanted = {
        _user(jid)
        for (jid,) in conn.execute(
            "SELECT jid FROM chats WHERE jid LIKE '%@s.whatsapp.net' OR jid LIKE '%@lid'"
        )
    }
    wanted |= {
        _user(sender)
        for (sender,) in conn.execute(
            "SELECT DISTINCT sender FROM messages WHERE COALESCE(sender, '') <> ''"
        )
    }
    none = (None, None, None)
    named = []
    for user in sorted(wanted):
        own = entries.get(user, none)
        other = entries.get(same_person.get(user, ""), none)
        for candidate in (own[0], other[0], own[1], other[1], own[2], other[2]):
            if _is_name(candidate):
                named.append((user, candidate.strip()))
                break
    conn.executemany("INSERT INTO contacts (user, name) VALUES (?, ?)", named)
    return len(named)


def build_snapshot(source, dest):
    """Write the minimized copy of `source` to `dest`; return its counts.

    One read transaction covers both tables, so a message written by the bridge
    between the two copies cannot arrive without its chat.
    """
    source = _checked_source(source)
    dest = _checked_dest(dest)
    if os.path.exists(dest):
        os.remove(dest)
    # uri=True on the MAIN connection: ATTACH honours a file: URI, and so the
    # read-only mode below, only on a connection opened with URIs enabled. Some
    # builds enable them by default and some do not.
    dest_uri = Path(dest).as_uri()
    conn = sqlite3.connect(dest_uri, uri=True, isolation_level=None)
    try:
        uri = Path(source).as_uri() + "?mode=ro"
        try:
            conn.execute("ATTACH DATABASE ? AS src", (uri,))
            conn.execute("SELECT count(*) FROM src.sqlite_master").fetchone()
        except sqlite3.Error:
            raise SnapshotError(EX_NOINPUT, "source store unreadable") from None
        for table, wanted in (("chats", CHAT_COLUMNS), ("messages", MESSAGE_COLUMNS)):
            missing = _missing_columns(conn, table, wanted)
            if missing:
                raise SnapshotError(
                    EX_DATAERR, f"source {table} lacks {len(missing)} expected column(s)"
                )
        conn.executescript(_SCHEMA)
        conn.execute("BEGIN")
        chat_cols = ", ".join(CHAT_COLUMNS)
        msg_cols = ", ".join(MESSAGE_COLUMNS)
        conn.execute(f"INSERT INTO chats ({chat_cols}) SELECT {chat_cols} FROM src.chats")
        conn.execute(
            f"INSERT OR IGNORE INTO messages ({msg_cols}) SELECT {msg_cols} FROM src.messages"
        )
        conn.execute("COMMIT")
        conn.execute("DETACH DATABASE src")
        contacts = _copy_contacts(conn, source)
        chats = conn.execute("SELECT count(*) FROM chats").fetchone()[0]
        messages = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    except BaseException:
        conn.close()
        if os.path.exists(dest):
            os.remove(dest)
        raise
    conn.close()
    return {
        "chats": chats,
        "messages": messages,
        "contacts": contacts,
        "bytes": os.path.getsize(dest),
    }


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: whatsapp_snapshot.py SOURCE DEST", file=sys.stderr)
        return 2
    os.umask(0o077)
    try:
        counts = build_snapshot(argv[0], argv[1])
    except SnapshotError as e:
        print(f"whatsapp_snapshot: {e}", file=sys.stderr)
        return e.code
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
