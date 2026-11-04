"""Forget everything older than a date (issue 174): `brain maint forget --before DATE`.

The owner's decision (2026-11-04) is manual only: no automatic cutoff, no per-source TTL that
deletes on its own, no timer. This module is the one path a forget takes, whatever chose the
rows: a cutoff (`--before`), or the mail the mailbox reports deleted (`--deleted-at-source`,
src/export/inbox_reconcile.py records it; nothing here acts on it unless the owner asks).

A forget picks PARENTS and takes everything they left:

- an email (mail, automation, news, standalone document, session note) received before the
  date, with its attachments, their text, chunks and figures, its items, topics, people
  links, HTML, aliases, thread-id map rows and inline-image occurrences;
- a Teams thread or WhatsApp session that ENDED before the date, with its messages, items and
  topic links; a message in no thread (system records) sent before the date;
- a calendar event that ended before the date, with its attendees, items and topic links;
- a Claude Code conversation that ended before the date, with its turns, items and topics.

A thread that straddles the date stays whole: its summary and items read all of it.

The full-text rows go with the delete triggers, and on --apply each touched index is merged
(`optimize`) and deletes run with `secure_delete`, so the text is not left in free pages or
old FTS segments. The vectors go from the `vectors` table (the gated step, issue 171) or from
embeddings.npz, whichever the store uses, with every chunk whose parent goes. Files under the
data home that held the forgotten content go too (attachment files, fetched SharePoint copies,
staging batches written before the date), never one a kept row still names.

The dry run (the default) does every delete in one transaction and rolls it back, so the counts
it prints are the ones --apply would produce. Either way the run holds the store's write lock
throughout (4.5 minutes for 33,084 emails on a copy of the owner's store, 2026-11-04): stop the
sb-* timers first, the dry run included, or the jobs wait for it. Cleanup that commits on its own (figure series
read again) and files run only on --apply.

What this cannot reach is printed at the end of every run (UNREACHABLE): the backup tiers
taken before it, the replicas until their next pull, the sources themselves.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

SOURCES = ("email", "teams", "whatsapp", "calendar", "conversation")

# The item tables and the parent columns they can carry (some only after a gated step).
ITEM_TABLES = ("decisions", "action_items", "key_facts", "commitments")
_ITEM_PARENTS = {
    "email_id": "_f_emails",
    "attachment_id": "_f_attachments",
    "teams_thread_id": "_f_teams_threads",
    "whatsapp_thread_id": "_f_whatsapp_threads",
    "event_id": "_f_events",
    "conversation_turn_id": "_f_turns",
}

# Every table a fully migrated store holds, sorted by what a forget does with it. A test
# (tests/test_retention.py) fails when a table appears in neither, so a new table is a
# decision, not an oversight.
COVERED = frozenset(
    {
        "emails",
        "attachments",
        "attachment_content",
        "attachment_chunks",
        "email_html",
        "email_people",
        "email_topics",
        "email_aliases",
        "thread_id_map",
        "thread_rollups",
        "inline_image_occurrences",
        "inline_images",
        *ITEM_TABLES,
        "item_labels",
        "item_ledger",
        "figure_editions",
        "figure_values",
        "figure_restated",
        "figure_metrics",
        "figure_series",
        "teams_threads",
        "teams_messages",
        "teams_thread_topics",
        "whatsapp_threads",
        "whatsapp_messages",
        "whatsapp_thread_topics",
        "calendar_events",
        "event_attendees",
        "event_topics",
        "conversations",
        "conversation_turns",
        "conversation_topics",
        "topic_pages",
        "vectors",
    }
)
# Kept on purpose: names and identifiers (people, topics, entities and their maps), the
# containers of chats, bookkeeping with no content of a forgotten item, and the records
# that keep a forgotten source from being fetched again (session notes, SharePoint links).
KEPT = frozenset(
    {
        "people",
        "person_identifiers",
        "person_mentions",
        "person_merges",
        "person_owner",
        "person_review",
        "teams_mri_resolution",
        "topics",
        "topic_family",
        "canonical_topics",
        "topic_aliases",
        "entities",
        "entity_aliases",
        "entity_adjudications",
        "teams_chats",
        "whatsapp_chats",
        "directory_entries",
        "boilerplate_fact_keys",
        "sender_signature_index",
        "consolidation_threads",
        "session_notes",
        "session_note_transcripts",
        "sharepoint_links",
        "findings",
        "job_runs",
        "sync_metadata",
        "schema_version",
        "gated_migrations",
        "sqlite_sequence",
    }
)

UNREACHABLE = (
    "Not reached by this run (the owner's to handle):",
    "- Local snapshots (scripts/backup_db.py --local-dir, the newest --local-keep, default 7) "
    "and the encrypted offsite archives (--gfs-daily 14 + --gfs-weekly 8) taken before now "
    "still hold the forgotten rows until they rotate out; delete older ones by hand to purge "
    "sooner (docs/RESTORE.md).",
    "- restic: a forget cannot rewrite its history. Snapshots taken before now keep the rows "
    "(and attachment files, when attachments/ is in the backup) until `restic forget --prune` "
    "drops them under the host's policy.",
    "- The --backup file this run wrote (or named): delete it once the result is checked.",
    "- Replicas (the Mac's brain.db and embeddings.npz) until their next hourly pull.",
    "- The sources: Outlook, Teams, WhatsApp, the calendar and ~/.claude/projects keep their "
    "copies; a backfill or a re-export that reaches before the date would load them again.",
    "- Logs under the data home and the journal may quote subjects; they are not rewritten.",
    "- People, topics and entities stay (names only), and so do session-note and SharePoint "
    "link records, which keep a forgotten note or file from being fetched again.",
    "- SQLite keeps the file at its size until `VACUUM` (timers stopped, free space of the "
    "store's size); secure_delete has already zeroed the freed pages.",
)


@dataclass
class Result:
    applied: bool
    tables: dict[str, int] = field(default_factory=dict)
    fts: dict[str, int] = field(default_factory=dict)
    vectors: int = 0
    vector_backend: str = "none"
    files: int = 0
    file_bytes: int = 0
    skipped_undated: dict[str, int] = field(default_factory=dict)


def parse_cutoff(text: str) -> str:
    """A YYYY-MM-DD date in the past, as the ISO string the stored times compare against."""
    day = date.fromisoformat(text)
    if day > datetime.now(UTC).date():
        raise ValueError(f"--before {text} is in the future")
    return day.isoformat()


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def _fts_of(conn: sqlite3.Connection) -> dict[str, str]:
    """content table -> its fts5 table, for the tables a delete trigger keeps in step."""
    out = {}
    for name in _tables(conn):
        if name.endswith("_fts") and name[: -len("_fts")] in _tables(conn):
            out[name[: -len("_fts")]] = name
    return out


# --- choosing the parents ---------------------------------------------------------------------


def _temp_tables(conn: sqlite3.Connection) -> None:
    for name in (
        "_f_emails",
        "_f_attachments",
        "_f_contents",
        "_f_teams_threads",
        "_f_teams_messages",
        "_f_whatsapp_threads",
        "_f_whatsapp_messages",
        "_f_events",
        "_f_conversations",
        "_f_turns",
    ):
        conn.execute(f"CREATE TEMP TABLE IF NOT EXISTS {name} (id INTEGER PRIMARY KEY)")
        conn.execute(f"DELETE FROM {name}")
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS _f_items (tbl TEXT, id INTEGER, PRIMARY KEY (tbl, id))"
    )
    conn.execute("DELETE FROM _f_items")


def _select_before(conn: sqlite3.Connection, cutoff: str, sources: set[str]) -> dict[str, int]:
    """Fill the temp id tables with the parents older than the cutoff; the undated rows
    left alone, per source."""
    tables = _tables(conn)
    undated: dict[str, int] = {}

    def pick(target: str, sql: str, *args) -> None:
        conn.execute(f"INSERT OR IGNORE INTO {target} (id) {sql}", args)

    if "email" in sources:
        pick("_f_emails", "SELECT id FROM emails WHERE date_received < ?", cutoff)
        undated["emails"] = conn.execute(
            "SELECT COUNT(*) FROM emails WHERE date_received IS NULL OR date_received = ''"
        ).fetchone()[0]
    if "teams" in sources and "teams_threads" in tables:
        pick("_f_teams_threads", "SELECT id FROM teams_threads WHERE ended_at < ?", cutoff)
        pick(
            "_f_teams_messages",
            "SELECT id FROM teams_messages WHERE thread_id IN (SELECT id FROM _f_teams_threads)"
            " OR (thread_id IS NULL AND composed_at < ?)",
            cutoff,
        )
    if "whatsapp" in sources and "whatsapp_threads" in tables:
        pick("_f_whatsapp_threads", "SELECT id FROM whatsapp_threads WHERE ended_at < ?", cutoff)
        pick(
            "_f_whatsapp_messages",
            "SELECT id FROM whatsapp_messages"
            " WHERE thread_id IN (SELECT id FROM _f_whatsapp_threads)"
            " OR (thread_id IS NULL AND sent_at < ?)",
            cutoff,
        )
    if "calendar" in sources and "calendar_events" in tables:
        pick(
            "_f_events",
            "SELECT id FROM calendar_events WHERE COALESCE(NULLIF(end_at, ''), start_at) < ?",
            cutoff,
        )
    if "conversation" in sources and "conversations" in tables:
        pick(
            "_f_conversations",
            "SELECT id FROM conversations WHERE COALESCE(NULLIF(ended_at, ''), started_at) < ?",
            cutoff,
        )
    return undated


def _select_emails(conn: sqlite3.Connection, email_ids) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO _f_emails (id) VALUES (?)", [(int(i),) for i in email_ids]
    )


def _derive(conn: sqlite3.Connection) -> None:
    """The rows hanging off the chosen parents: attachments, their content, turns, items."""
    conn.execute(
        "INSERT OR IGNORE INTO _f_attachments (id)"
        " SELECT id FROM attachments WHERE email_id IN (SELECT id FROM _f_emails)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO _f_contents (id) SELECT id FROM attachment_content"
        " WHERE attachment_id IN (SELECT id FROM _f_attachments)"
    )
    if "conversation_turns" in _tables(conn):
        conn.execute(
            "INSERT OR IGNORE INTO _f_turns (id) SELECT id FROM conversation_turns"
            " WHERE conversation_id IN (SELECT id FROM _f_conversations)"
        )
    tables = _tables(conn)
    for table in ITEM_TABLES:
        if table not in tables:
            continue
        cols = _columns(conn, table)
        where = " OR ".join(
            f"{col} IN (SELECT id FROM {temp})"
            for col, temp in _ITEM_PARENTS.items()
            if col in cols
        )
        if where:
            conn.execute(
                f"INSERT OR IGNORE INTO _f_items (tbl, id) SELECT ?, id FROM {table} WHERE {where}",
                (table,),
            )


# --- deleting ---------------------------------------------------------------------------------


def _delete(conn: sqlite3.Connection, counts: dict[str, int], table: str, where: str) -> None:
    if table not in _tables(conn):
        return
    n = conn.execute(f"DELETE FROM {table} WHERE {where}").rowcount
    counts[table] = counts.get(table, 0) + max(n, 0)


def _delete_rows(conn: sqlite3.Connection) -> dict[str, int]:
    """Every row of the chosen parents, children first (the foreign keys are NO ACTION)."""
    c: dict[str, int] = {}
    tables = _tables(conn)
    if "item_labels" in tables:
        _delete(
            conn,
            c,
            "item_labels",
            "EXISTS (SELECT 1 FROM _f_items f WHERE f.tbl = item_labels.item_table"
            " AND f.id = item_labels.item_id)",
        )
    for table in ITEM_TABLES:
        _delete(conn, c, table, f"id IN (SELECT id FROM _f_items WHERE tbl = '{table}')")

    # Mail and its attachments.
    emails = "email_id IN (SELECT id FROM _f_emails)"
    for table in ("email_topics", "email_people", "email_html", "email_aliases", "thread_id_map"):
        _delete(conn, c, table, emails)
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _f_images (sha256 TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM _f_images")
    conn.execute(
        "INSERT OR IGNORE INTO _f_images SELECT sha256 FROM attachments"
        " WHERE id IN (SELECT id FROM _f_attachments) AND sha256 IS NOT NULL"
    )
    if "inline_image_occurrences" in tables:
        conn.execute(
            "INSERT OR IGNORE INTO _f_images SELECT sha256 FROM inline_image_occurrences"
            " WHERE message_id IN (SELECT CAST(message_id AS TEXT) FROM emails"
            " WHERE id IN (SELECT id FROM _f_emails))"
        )
        _delete(
            conn,
            c,
            "inline_image_occurrences",
            "message_id IN (SELECT CAST(message_id AS TEXT) FROM emails"
            " WHERE id IN (SELECT id FROM _f_emails))",
        )
    _delete(conn, c, "attachment_chunks", "attachment_id IN (SELECT id FROM _f_attachments)")
    _delete(conn, c, "attachment_content", "id IN (SELECT id FROM _f_contents)")
    conversations = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT conversation_id FROM emails WHERE id IN (SELECT id FROM _f_emails)"
            " AND conversation_id IS NOT NULL"
        )
    ]
    _delete(conn, c, "attachments", "id IN (SELECT id FROM _f_attachments)")
    _delete(conn, c, "emails", "id IN (SELECT id FROM _f_emails)")
    if "thread_rollups" in tables and conversations:
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _f_convs (id TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM _f_convs")
        conn.executemany("INSERT OR IGNORE INTO _f_convs VALUES (?)", [(x,) for x in conversations])
        _delete(
            conn,
            c,
            "thread_rollups",
            "conversation_id IN (SELECT id FROM _f_convs) AND NOT EXISTS"
            " (SELECT 1 FROM emails e WHERE e.conversation_id = thread_rollups.conversation_id)",
        )
    if "inline_images" in tables:
        _delete(
            conn,
            c,
            "inline_images",
            "sha256 IN (SELECT sha256 FROM _f_images) AND NOT EXISTS"
            " (SELECT 1 FROM inline_image_occurrences o WHERE o.sha256 = inline_images.sha256)"
            " AND NOT EXISTS (SELECT 1 FROM attachments a WHERE a.sha256 = inline_images.sha256)",
        )

    # Teams and WhatsApp.
    _delete(conn, c, "teams_thread_topics", "thread_id IN (SELECT id FROM _f_teams_threads)")
    _delete(conn, c, "teams_messages", "id IN (SELECT id FROM _f_teams_messages)")
    _delete(conn, c, "teams_threads", "id IN (SELECT id FROM _f_teams_threads)")
    _delete(conn, c, "whatsapp_thread_topics", "thread_id IN (SELECT id FROM _f_whatsapp_threads)")
    _delete(conn, c, "whatsapp_messages", "id IN (SELECT id FROM _f_whatsapp_messages)")
    _delete(conn, c, "whatsapp_threads", "id IN (SELECT id FROM _f_whatsapp_threads)")

    # Calendar and conversations.
    _delete(conn, c, "event_attendees", "event_id IN (SELECT id FROM _f_events)")
    _delete(conn, c, "event_topics", "event_id IN (SELECT id FROM _f_events)")
    _delete(conn, c, "calendar_events", "id IN (SELECT id FROM _f_events)")
    _delete(conn, c, "conversation_topics", "conversation_id IN (SELECT id FROM _f_conversations)")
    _delete(conn, c, "conversation_turns", "id IN (SELECT id FROM _f_turns)")
    _delete(conn, c, "conversations", "id IN (SELECT id FROM _f_conversations)")

    # The ledger entries whose parent is gone (src/store/ledger.py), on a store with it.
    if "item_ledger" in tables:
        from src.store import ledger

        c["item_ledger"] = ledger._forget(conn)
    return {k: v for k, v in c.items() if v}


def _forgotten_refs(conn: sqlite3.Connection) -> set[str]:
    """The evidence keys a topic page cites the chosen parents by (src/store/topic_pages.py)."""
    refs: set[str] = set()
    for (i,) in conn.execute("SELECT id FROM _f_emails"):
        refs |= {f"email:{i}", f"document:{i}"}
    for (i,) in conn.execute("SELECT id FROM _f_attachments"):
        refs.add(f"attachment:{i}")
    for prefix, temp in (
        ("teams", "_f_teams_threads"),
        ("whatsapp", "_f_whatsapp_threads"),
        ("event", "_f_events"),
        ("meeting", "_f_events"),
        ("conversation", "_f_conversations"),
    ):
        refs |= {f"{prefix}:{r[0]}" for r in conn.execute(f"SELECT id FROM {temp}")}
    if "conversations" in _tables(conn):
        refs |= {
            f"conversation:{r[0]}"
            for r in conn.execute(
                "SELECT session_id FROM conversations WHERE id IN (SELECT id FROM _f_conversations)"
            )
        }
    return refs


def _delete_pages(conn: sqlite3.Connection, refs: set[str], cutoff: str | None) -> int:
    """Topic pages that cite a forgotten item, or hold nothing newer than the cutoff: the
    nightly build makes them again from what is left."""
    if "topic_pages" not in _tables(conn):
        return 0
    gone = []
    for page_id, keys, citations, newest in conn.execute(
        "SELECT id, evidence_keys, citations, newest_evidence_at FROM topic_pages"
    ).fetchall():
        cited: set[str] = set()
        try:
            cited |= {str(k) for k in json.loads(keys or "[]")}
            cited |= {
                str(c.get("ref")) for c in json.loads(citations or "[]") if isinstance(c, dict)
            }
        except (ValueError, AttributeError):
            pass
        if cited & refs or (cutoff and newest and newest < cutoff):
            gone.append(page_id)
    conn.executemany("DELETE FROM topic_pages WHERE id = ?", [(i,) for i in gone])
    return len(gone)


# --- vectors and files ------------------------------------------------------------------------


def _vector_ids(conn: sqlite3.Connection) -> set[int]:
    """The index ids of the chosen parents (src/store/embeddings.py namespaces); chunks follow
    their parent (an attachment's summary id, an email's id)."""
    from src.store import embeddings as emb

    ids = {r[0] for r in conn.execute("SELECT id FROM _f_emails")}
    ids |= {-r[0] for r in conn.execute("SELECT id FROM _f_contents")}
    ids |= {
        emb.CONVERSATION_ID_OFFSET - r[0] for r in conn.execute("SELECT id FROM _f_conversations")
    }
    ids |= {
        emb.TEAMS_THREAD_ID_OFFSET - r[0] for r in conn.execute("SELECT id FROM _f_teams_threads")
    }
    ids |= {
        emb.WHATSAPP_THREAD_ID_OFFSET - r[0]
        for r in conn.execute("SELECT id FROM _f_whatsapp_threads")
    }
    return ids


def _npz_count(ids: set[int]) -> int:
    import numpy as np

    from src.store import embeddings as emb

    if not ids or not emb.EMBEDDINGS_FILE.exists():
        return 0
    with np.load(emb.EMBEDDINGS_FILE, allow_pickle=False) as data:
        stored = data["ids"]
    drop = np.fromiter(ids, dtype=np.int64)
    gone = np.isin(stored, drop) | (
        (stored <= emb.CHUNK_ID_OFFSET) & np.isin(emb._parents(stored), drop)
    )
    return int(gone.sum())


def _files(conn: sqlite3.Connection, cutoff: str | None, staging: bool) -> list[Path]:
    """The files under the data home that held the chosen parents' content, and no kept
    row names. Read before the rows go."""
    from src import config

    out: list[Path] = []
    att_root = Path(config.ATTACHMENTS_DIR)
    kept = {
        (Path(p).parent.name, Path(p).name)
        for (p,) in conn.execute(
            "SELECT file_path FROM attachments WHERE file_path IS NOT NULL"
            " AND id NOT IN (SELECT id FROM _f_attachments)"
        )
    }
    for (p,) in conn.execute(
        "SELECT file_path FROM attachments WHERE id IN (SELECT id FROM _f_attachments)"
        " AND file_path IS NOT NULL AND file_removed_at IS NULL"
    ):
        key = (Path(p).parent.name, Path(p).name)
        if key not in kept:
            out.append(att_root / key[0] / key[1])
    if "sharepoint_links" in _tables(conn):
        # By name under the SharePoint directory, as attachments are matched: a stored path
        # is the producer's spelling. A file a kept document's link names too stays.
        sp_root = Path(config.SHAREPOINT_DATA_DIR)
        gone_docs = (
            "document_message_id IN (SELECT message_id FROM emails"
            " WHERE id IN (SELECT id FROM _f_emails))"
        )
        sp_kept = {
            Path(p).name
            for (p,) in conn.execute(
                "SELECT fetched_path FROM sharepoint_links WHERE fetched_path IS NOT NULL"
                f" AND (document_message_id IS NULL OR NOT {gone_docs})"
            )
        }
        for (p,) in conn.execute(
            f"SELECT fetched_path FROM sharepoint_links WHERE fetched_path IS NOT NULL AND {gone_docs}"
        ):
            if Path(p).name not in sp_kept:
                out.append(sp_root / Path(p).name)
    if staging and cutoff:
        # Staging holds mail, documents and conversations not yet loaded: only a forget of
        # every source takes it, or a --source teams run would drop mail.
        limit = datetime.fromisoformat(cutoff).replace(tzinfo=UTC).timestamp()
        for root in (Path(config.DATA_ROOT) / "staging", Path(config.RAW_BATCH_DIR)):
            if root.is_dir():
                out += [
                    f for f in root.rglob("*.json") if f.is_file() and f.stat().st_mtime < limit
                ]
    data_root = Path(config.DATA_ROOT).resolve()
    held = []
    for f in out:
        try:
            real = f.resolve()
        except OSError:
            continue
        inside = real.is_relative_to(data_root) or real.is_relative_to(att_root.resolve())
        if inside and real.is_file():
            held.append(real)
    return sorted(set(held))


def _remove_files(files: list[Path]) -> None:
    for f in files:
        try:
            f.unlink()
        except FileNotFoundError:
            continue
        parent = f.parent
        try:
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass


# --- the run ----------------------------------------------------------------------------------


def forget(
    conn: sqlite3.Connection,
    *,
    cutoff: str | None = None,
    email_ids=None,
    sources: set[str] | None = None,
    apply: bool = False,
) -> Result:
    """Forget the parents a cutoff (or a list of email ids) chooses; a dry run unless `apply`.

    One transaction: the dry run rolls it back. On apply, after the commit: the figure series
    of forgotten attachments are read again, the npz loses its vectors (a store without the
    gated `vectors` step), the files go, and each touched full-text index is merged."""
    from src.store import vector_store

    if (cutoff is None) == (email_ids is None):
        raise ValueError("forget takes a cutoff or email ids, not both")
    chosen = set(SOURCES) if sources is None else set(sources)
    if conn.in_transaction:
        conn.commit()
    if apply:
        conn.execute("PRAGMA secure_delete = ON")
    result = Result(applied=apply)
    fts = _fts_of(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        _temp_tables(conn)
        if cutoff is not None:
            result.skipped_undated = _select_before(conn, cutoff, chosen)
        else:
            _select_emails(conn, email_ids)
        _derive(conn)
        att_ids = [r[0] for r in conn.execute("SELECT id FROM _f_attachments")]
        vector_ids = _vector_ids(conn)
        files = _files(conn, cutoff, staging=chosen == set(SOURCES))
        figures = _figure_counts(conn, att_ids)
        refs = _forgotten_refs(conn)
        result.tables = _delete_rows(conn)
        pages = _delete_pages(conn, refs, cutoff)
        if pages:
            result.tables["topic_pages"] = pages
        result.tables.update(figures)
        if vector_store.enabled(conn):
            result.vector_backend = "vectors table"
            result.vectors = vector_store.delete(conn, vector_ids)
        else:
            result.vector_backend = "embeddings.npz"
            result.vectors = _npz_count(vector_ids)
        # Each delete trigger takes the row's full-text entry with it.
        result.fts = {fts[t]: n for t, n in result.tables.items() if t in fts}
        result.files = len(files)
        result.file_bytes = sum(_size(f) for f in files)
        if not apply:
            conn.rollback()
            return result
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    if att_ids:
        from src.store.figures import forget_attachments

        forget_attachments(conn, att_ids)
    if result.vector_backend == "embeddings.npz" and vector_ids:
        from src.store.embeddings import remove_vectors

        result.vectors = remove_vectors(vector_ids, conn=conn)
    _remove_files(files)
    for table in result.fts:
        conn.execute(f"INSERT INTO {table}({table}) VALUES ('optimize')")
    conn.commit()
    return result


def _size(path: Path) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _figure_counts(conn: sqlite3.Connection, att_ids: list[int]) -> dict[str, int]:
    """The figure rows of the forgotten attachments (src/store/figures.py forget_attachments
    takes them on --apply, after the commit, and reads their series again)."""
    if not att_ids or "figure_editions" not in _tables(conn):
        return {}
    gone = json.dumps(att_ids)
    out: dict[str, int] = {}
    for table in ("figure_editions", "figure_values", "figure_restated"):
        if table in _tables(conn) and "attachment_id" in _columns(conn, table):
            n = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE attachment_id IN (SELECT value FROM json_each(?))",
                (gone,),
            ).fetchone()[0]
            if n:
                out[table] = n
    return out


# --- deleted at the source --------------------------------------------------------------------


def deleted_at_source(conn: sqlite3.Connection) -> dict:
    """What the sources report deleted, for the owner to forget by hand; nothing is deleted
    here. Stored mail src/export/inbox_reconcile.py found in Deleted Items or Junk Email, less
    the owner's own sent mail: he sends with a copy to himself, so deleting the Inbox copy
    records an email whose Sent Items copy the mailbox may still keep. And the Teams messages
    the service deleted, whose text the sync has blanked already (src/export/teams_export.py):
    counted, since what is left of them is an empty row."""
    from src import config
    from src.export.mail_reconcile import LOCATION_KEY
    from src.store.owner import is_owner_address

    records = {}
    for key, value in conn.execute(
        "SELECT key, value FROM sync_metadata WHERE key GLOB ?", (LOCATION_KEY + "*",)
    ):
        try:
            loaded = json.loads(value)
        except ValueError:
            continue
        records[key[len(LOCATION_KEY) :]] = loaded if isinstance(loaded, dict) else {}
    by_location: dict[str, int] = {}
    candidates: list[int] = []
    held_back = 0
    for message_id, rec in records.items():
        row = conn.execute(
            "SELECT id, sender_address FROM emails WHERE CAST(message_id AS TEXT) = ?",
            (message_id,),
        ).fetchone()
        if row is None:
            continue
        if is_owner_address(row[1], config.USER_EMAIL_PATTERN):
            held_back += 1
            continue
        candidates.append(row[0])
        loc = str(rec.get("location") or "unknown")
        by_location[loc] = by_location.get(loc, 0) + 1
    teams = 0
    if "teams_messages" in _tables(conn):
        teams = conn.execute(
            "SELECT COUNT(*) FROM teams_messages"
            " WHERE json_extract(raw_json, '$.properties.deletetime') IS NOT NULL"
            " OR json_extract(raw_json, '$.message.properties.deletetime') IS NOT NULL"
        ).fetchone()[0]
    return {
        "email_ids": sorted(candidates),
        "emails_by_location": by_location,
        "owner_sent_held_back": held_back,
        "teams_deleted_messages": teams,
    }


# --- the command ------------------------------------------------------------------------------


def _print_result(result: Result) -> None:
    verb = "deleted" if result.applied else "would delete"
    print(("Applied" if result.applied else "Dry run, rolled back") + f": rows {verb}, by table")
    for table in sorted(result.tables):
        print(f"  {table}: {result.tables[table]:,}")
    if not result.tables:
        print("  (none)")
    print("Full-text rows (delete triggers):")
    for table in sorted(result.fts):
        print(f"  {table}: {result.fts[table]:,}")
    if not result.fts:
        print("  (none)")
    print(f"Vectors ({result.vector_backend}): {result.vectors:,}")
    print(f"Files under the data home: {result.files:,} ({result.file_bytes / 1e6:,.1f} MB)")
    for table, n in result.skipped_undated.items():
        if n:
            print(f"Left alone, no date: {table} {n:,}")


def cmd_forget(args) -> int:
    """`brain maint forget`: a dry run unless --apply, which needs --backup PATH."""
    from src import config
    from src.store.schema import ensure_schema, get_connection

    if config.is_replica():
        print(config.replica_refusal("maint forget"), file=sys.stderr)
        return 2
    if not Path(args.db).exists():
        print(f"Error: no store at {args.db}", file=sys.stderr)
        return 1
    if bool(args.before) == bool(args.deleted_at_source):
        print("Error: give --before DATE or --deleted-at-source", file=sys.stderr)
        return 2
    cutoff = None
    if args.before:
        try:
            cutoff = parse_cutoff(args.before)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
    conn = get_connection(str(args.db))
    try:
        ensure_schema(conn)
        email_ids = None
        if args.deleted_at_source:
            report = deleted_at_source(conn)
            email_ids = report["email_ids"]
            print(
                f"Reported deleted at the source: {len(email_ids):,} emails"
                f" {report['emails_by_location']}; the owner's sent mail held back:"
                f" {report['owner_sent_held_back']:,}; Teams messages deleted (text already"
                f" blanked, rows kept): {report['teams_deleted_messages']:,}"
            )
        backup = None
        if args.apply:
            from src.cli import _gated_backup

            backup = _gated_backup(conn, args.backup)
            _backup_npz(conn, backup)
        sources = set(args.source) if args.source else None
        result = forget(conn, cutoff=cutoff, email_ids=email_ids, sources=sources, apply=args.apply)
    finally:
        conn.close()
    what = f"before {cutoff}" if cutoff else "deleted at the source"
    print(f"Forget {what}{', sources ' + ', '.join(sorted(sources)) if sources else ''}")
    _print_result(result)
    if backup:
        print(f"Backup of the store: {backup}")
    if not result.applied:
        print("Nothing changed. --apply --backup PATH deletes them.")
    for line in UNREACHABLE:
        print(line)
    return 0


def _backup_npz(conn: sqlite3.Connection, backup: str) -> None:
    """On a store whose index is still embeddings.npz, a copy beside the store's backup: the
    forget rewrites the file, and the backup of brain.db does not hold it."""
    import shutil

    from src.store import embeddings as emb
    from src.store import vector_store

    if vector_store.enabled(conn) or not emb.EMBEDDINGS_FILE.exists():
        return
    target = Path(backup).with_name(Path(backup).name + ".embeddings.npz")
    shutil.copy2(emb.EMBEDDINGS_FILE, target)
    print(f"Backup of the index: {target}")


def add_maint_parsers(maint_subparsers) -> None:
    """`brain maint forget`."""
    p = maint_subparsers.add_parser(
        "forget",
        help="Forget everything older than a date (or reported deleted at the source)",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--before", metavar="YYYY-MM-DD", help="Forget what is older than this day")
    p.add_argument(
        "--deleted-at-source",
        action="store_true",
        help="Forget the stored mail the mailbox reports in Deleted Items or Junk Email",
    )
    p.add_argument(
        "--source",
        action="append",
        choices=SOURCES,
        help="Only this source (repeatable; default all)",
    )
    p.add_argument("--apply", action="store_true", help="Delete (default: a dry run)")
    p.add_argument(
        "--backup",
        metavar="PATH",
        help="With --apply: a new file to back the store up to first, or one under an hour old",
    )
    p.set_defaults(func=cmd_forget)
