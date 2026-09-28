#!/usr/bin/env python3
"""Build the minimized WhatsApp snapshot the producer ingests.

    whatsapp_snapshot.py SOURCE DEST

SOURCE is the WhatsApp bridge's messages.db, opened read-only. DEST is written
from scratch with two tables and exactly the columns listed below, and nothing
else: the bridge also keeps, for every media message, the CDN URL, the media key
and the file hashes, which together download and decrypt that photo or voice
note. The producer needs none of it, so none of it leaves this machine.

Prints one JSON line of counts ({"chats", "messages", "bytes"}) and never a
message, a name or a number, because its output lands in a launchd log.

Stdlib only and Python 3.9 compatible: scripts/wrappers/launchd/
sync-whatsapp-to-vps.sh runs it with /usr/bin/python3, which is 3.9 on macOS.

Exit codes: 0 done, 65 the source lacks a column this needs, 66 the source is
missing or unreadable.
"""

import json
import os
import sqlite3
import sys
from urllib.parse import quote

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
"""


class SnapshotError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _missing_columns(conn, table, wanted):
    have = {row[1] for row in conn.execute(f"PRAGMA src.table_info({table})")}
    return [c for c in wanted if c not in have]


def build_snapshot(source, dest):
    """Write the minimized copy of `source` to `dest`; return its counts.

    One read transaction covers both tables, so a message written by the bridge
    between the two copies cannot arrive without its chat.
    """
    if not os.path.isfile(source):
        raise SnapshotError(EX_NOINPUT, "source store not found")
    if os.path.exists(dest):
        os.remove(dest)
    # uri=True on the MAIN connection: ATTACH honours a file: URI, and so the
    # read-only mode below, only on a connection opened with URIs enabled. Some
    # builds enable them by default and some do not.
    dest_uri = "file:" + quote(os.path.abspath(dest))
    conn = sqlite3.connect(dest_uri, uri=True, isolation_level=None)
    try:
        uri = "file:" + quote(os.path.abspath(source)) + "?mode=ro"
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
        chats = conn.execute("SELECT count(*) FROM chats").fetchone()[0]
        messages = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    except BaseException:
        conn.close()
        if os.path.exists(dest):
            os.remove(dest)
        raise
    conn.close()
    return {"chats": chats, "messages": messages, "bytes": os.path.getsize(dest)}


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
