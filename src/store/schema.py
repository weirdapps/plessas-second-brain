"""SQLite database schema for the second-brain knowledge store.

Creates tables for emails, topics, decisions, action items, people, and their relationships.
Includes FTS5 full-text search indexes for emails and key facts.
"""

import hashlib
import re
import sqlite3
from pathlib import Path

from src.config import CURRENT_SCHEMA_VERSION


def create_database(db_path: str) -> sqlite3.Connection:
    """Create a new SQLite database with all tables and indexes.

    Args:
        db_path: Path to the SQLite database file

    Returns:
        sqlite3.Connection object

    Raises:
        sqlite3.Error: If database creation fails
    """
    # Ensure parent directory exists
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Born in WAL mode (see get_connection). No-op on :memory: test DBs.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")

    # Core email metadata + extracted summary
    conn.execute("""
        CREATE TABLE emails (
            id INTEGER PRIMARY KEY,
            message_id INTEGER UNIQUE NOT NULL,
            date_received TEXT NOT NULL,
            sender_name TEXT,
            sender_address TEXT,
            subject TEXT,
            summary TEXT,
            sentiment TEXT,
            urgency TEXT,
            language TEXT,
            mailbox_name TEXT,
            content TEXT,
            in_reply_to TEXT,
            "references" TEXT,
            conversation_id TEXT,
            internet_message_id TEXT
        )
    """)
    # Partial unique index: enforce uniqueness only when imid is set, so
    # historical rows that pre-date capture remain insertable.
    conn.execute("""
        CREATE UNIQUE INDEX idx_emails_internet_message_id
        ON emails(internet_message_id)
        WHERE internet_message_id IS NOT NULL
    """)

    # Topic registry with optional hierarchy
    conn.execute("""
        CREATE TABLE topics (
            id INTEGER PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            display_name TEXT NOT NULL,
            parent_id INTEGER REFERENCES topics(id)
        )
    """)

    # Many-to-many: emails <-> topics
    conn.execute("""
        CREATE TABLE email_topics (
            email_id INTEGER REFERENCES emails(id),
            topic_id INTEGER REFERENCES topics(id),
            PRIMARY KEY (email_id, topic_id)
        )
    """)

    # Decisions extracted from emails
    conn.execute("""
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY,
            email_id INTEGER REFERENCES emails(id),
            decision TEXT NOT NULL,
            decided_by TEXT,
            decision_date TEXT
        )
    """)

    # Action items / tasks
    conn.execute("""
        CREATE TABLE action_items (
            id INTEGER PRIMARY KEY,
            email_id INTEGER REFERENCES emails(id),
            task TEXT NOT NULL,
            owner TEXT,
            deadline TEXT,
            status TEXT DEFAULT 'open'
        )
    """)

    # Commitments (promises made by/to people) — extracted by the LLM.
    conn.execute("""
        CREATE TABLE commitments (
            id INTEGER PRIMARY KEY,
            email_id INTEGER REFERENCES emails(id),
            commitment TEXT NOT NULL,
            by_person TEXT,
            to_person TEXT
        )
    """)

    # Person registry
    conn.execute("""
        CREATE TABLE people (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT UNIQUE,
            role TEXT,
            department TEXT
        )
    """)

    # Who was involved in each email + their role
    conn.execute("""
        CREATE TABLE email_people (
            email_id INTEGER REFERENCES emails(id),
            person_id INTEGER REFERENCES people(id),
            role_in_email TEXT,
            PRIMARY KEY (email_id, person_id, role_in_email)
        )
    """)

    # Searchable facts
    conn.execute("""
        CREATE TABLE key_facts (
            id INTEGER PRIMARY KEY,
            email_id INTEGER REFERENCES emails(id),
            fact TEXT NOT NULL
        )
    """)

    # Attachment metadata
    conn.execute("""
        CREATE TABLE attachments (
            id INTEGER PRIMARY KEY,
            email_id INTEGER REFERENCES emails(id),
            message_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            mime_type TEXT,
            file_size INTEGER,
            file_path TEXT NOT NULL,
            is_inline INTEGER DEFAULT 0,
            exported_at TEXT NOT NULL
        )
    """)

    # Attachment content extraction and LLM processing
    conn.execute("""
        CREATE TABLE attachment_content (
            id INTEGER PRIMARY KEY,
            attachment_id INTEGER NOT NULL REFERENCES attachments(id),
            extracted_text TEXT,
            extraction_method TEXT,
            extraction_status TEXT NOT NULL,
            extraction_error TEXT,
            extracted_at TEXT,
            summary TEXT,
            language TEXT,
            llm_status TEXT NOT NULL DEFAULT 'pending',
            llm_error TEXT,
            llm_extracted_at TEXT,
            UNIQUE(attachment_id)
        )
    """)

    # Sync metadata (tracks incremental sync state)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_metadata (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # SharePoint link tracking (v9; attempts added v16)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sharepoint_links (
            url TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            fetched_at TIMESTAMP,
            fetched_path TEXT,
            last_status TEXT,
            last_attempt_at TIMESTAMP,
            file_name TEXT,
            file_size INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0
        )
    """)

    # Inline image classifier tables (v10; visioned_at added v15)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS inline_images (
            sha256 TEXT PRIMARY KEY,
            width INTEGER NOT NULL,
            height INTEGER NOT NULL,
            bytes INTEGER NOT NULL,
            classification TEXT NOT NULL CHECK (classification IN ('noise','signature','content','unclassified')),
            classification_method TEXT NOT NULL,
            classified_at TIMESTAMP NOT NULL,
            vision_description TEXT,
            visioned_at TIMESTAMP,
            user_overridden BOOLEAN NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS inline_image_occurrences (
            sha256 TEXT NOT NULL REFERENCES inline_images(sha256) ON DELETE CASCADE,
            message_id TEXT NOT NULL,
            sender_email TEXT NOT NULL,
            position_in_body REAL NOT NULL,
            PRIMARY KEY (sha256, message_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sender_signature_index (
            sender_email TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            occurrence_count INTEGER NOT NULL,
            sender_total INTEGER NOT NULL,
            frequency REAL NOT NULL,
            PRIMARY KEY (sender_email, sha256)
        )
    """)

    # Create indexes
    conn.execute("CREATE INDEX idx_emails_date_received ON emails(date_received)")
    conn.execute("CREATE INDEX idx_emails_sender_address ON emails(sender_address)")
    conn.execute("CREATE INDEX idx_email_topics_topic_id ON email_topics(topic_id)")
    conn.execute("CREATE INDEX idx_email_people_person_id ON email_people(person_id)")
    conn.execute("CREATE INDEX idx_decisions_email_id ON decisions(email_id)")
    conn.execute("CREATE INDEX idx_action_items_email_id ON action_items(email_id)")
    conn.execute("CREATE INDEX idx_action_items_owner ON action_items(owner)")
    conn.execute("CREATE INDEX idx_key_facts_email_id ON key_facts(email_id)")
    conn.execute("CREATE INDEX idx_emails_conversation_id ON emails(conversation_id)")
    conn.execute("CREATE INDEX idx_attachments_email_id ON attachments(email_id)")
    conn.execute("CREATE INDEX idx_attachments_message_id ON attachments(message_id)")
    conn.execute(
        "CREATE INDEX idx_attachment_content_attachment_id ON attachment_content(attachment_id)"
    )
    conn.execute(
        "CREATE INDEX idx_attachment_content_extraction_status ON attachment_content(extraction_status)"
    )
    conn.execute("CREATE INDEX idx_attachment_content_llm_status ON attachment_content(llm_status)")

    # SharePoint links indexes (v9)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sharepoint_message ON sharepoint_links(message_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sharepoint_status ON sharepoint_links(last_status)"
    )

    # Inline image occurrences index (v10)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_occurrences_sender_sha "
        "ON inline_image_occurrences(sender_email, sha256)"
    )

    # FTS5 virtual tables
    conn.execute("""
        CREATE VIRTUAL TABLE emails_fts USING fts5(
            summary,
            content,
            content='emails',
            content_rowid='id'
        )
    """)

    conn.execute("""
        CREATE VIRTUAL TABLE key_facts_fts USING fts5(
            fact,
            content='key_facts',
            content_rowid='id'
        )
    """)

    conn.execute("""
        CREATE VIRTUAL TABLE attachment_content_fts USING fts5(
            extracted_text, summary,
            content='attachment_content',
            content_rowid='id'
        )
    """)

    # Triggers to keep FTS5 tables in sync
    conn.execute("""
        CREATE TRIGGER emails_ai AFTER INSERT ON emails BEGIN
            INSERT INTO emails_fts(rowid, summary, content) VALUES (new.id, new.summary, new.content);
        END
    """)

    conn.execute("""
        CREATE TRIGGER emails_ad AFTER DELETE ON emails BEGIN
            INSERT INTO emails_fts(emails_fts, rowid, summary, content) VALUES('delete', old.id, old.summary, old.content);
        END
    """)

    conn.execute("""
        CREATE TRIGGER emails_au AFTER UPDATE ON emails BEGIN
            INSERT INTO emails_fts(emails_fts, rowid, summary, content) VALUES('delete', old.id, old.summary, old.content);
            INSERT INTO emails_fts(rowid, summary, content) VALUES (new.id, new.summary, new.content);
        END
    """)

    conn.execute("""
        CREATE TRIGGER key_facts_ai AFTER INSERT ON key_facts BEGIN
            INSERT INTO key_facts_fts(rowid, fact) VALUES (new.id, new.fact);
        END
    """)

    conn.execute("""
        CREATE TRIGGER key_facts_ad AFTER DELETE ON key_facts BEGIN
            INSERT INTO key_facts_fts(key_facts_fts, rowid, fact) VALUES('delete', old.id, old.fact);
        END
    """)

    conn.execute("""
        CREATE TRIGGER key_facts_au AFTER UPDATE ON key_facts BEGIN
            INSERT INTO key_facts_fts(key_facts_fts, rowid, fact) VALUES('delete', old.id, old.fact);
            INSERT INTO key_facts_fts(rowid, fact) VALUES (new.id, new.fact);
        END
    """)

    conn.execute("""
        CREATE TRIGGER attachment_content_ai AFTER INSERT ON attachment_content BEGIN
            INSERT INTO attachment_content_fts(rowid, extracted_text, summary)
            VALUES (new.id, new.extracted_text, new.summary);
        END
    """)

    conn.execute("""
        CREATE TRIGGER attachment_content_ad AFTER DELETE ON attachment_content BEGIN
            INSERT INTO attachment_content_fts(attachment_content_fts, rowid, extracted_text, summary)
            VALUES('delete', old.id, old.extracted_text, old.summary);
        END
    """)

    conn.execute("""
        CREATE TRIGGER attachment_content_au AFTER UPDATE ON attachment_content BEGIN
            INSERT INTO attachment_content_fts(attachment_content_fts, rowid, extracted_text, summary)
            VALUES('delete', old.id, old.extracted_text, old.summary);
            INSERT INTO attachment_content_fts(rowid, extracted_text, summary)
            VALUES (new.id, new.extracted_text, new.summary);
        END
    """)

    # Store schema version
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER NOT NULL
        )
    """)
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (CURRENT_SCHEMA_VERSION,))

    conn.commit()
    return conn


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Get the current schema version from the database."""
    try:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        return row["version"] if row else 0
    except sqlite3.OperationalError:
        return 0


def set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    """Set the schema version in the database."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER NOT NULL
        )
    """)
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    conn.commit()


def run_migrations(conn: sqlite3.Connection) -> None:
    """Run all pending migrations based on schema version."""
    current = get_schema_version(conn)

    if current < 1:
        migrate_fts_content(conn)
    if current < 2:
        migrate_add_threads(conn)
    if current < 3:
        migrate_add_sync_metadata(conn)
    if current < 4:
        migrate_add_attachments(conn)
    if current < 5:
        migrate_add_attachment_content(conn)
    if current < 6:
        # v6 originally added the wiki layer; superseded by v8 (drop). No-op.
        pass
    if current < 7:
        migrate_add_conversations(conn)
    if current < 8:
        migrate_drop_wiki_pages(conn)
    if current < 9:
        migrate_add_sharepoint_links(conn)
    if current < 10:
        migrate_add_inline_images(conn)
    if current < 11:
        migrate_add_internet_message_id(conn)
    if current < 12:
        migrate_add_teams(conn)
    if current < 13:
        migrate_add_calendar(conn)
    if current < 14:
        migrate_add_commitments(conn)
    if current < 15:
        migrate_add_visioned_at(conn)
    if current < 16:
        migrate_add_sharepoint_attempts(conn)

    if current < CURRENT_SCHEMA_VERSION:
        set_schema_version(conn, CURRENT_SCHEMA_VERSION)


def migrate_add_commitments(conn: sqlite3.Connection) -> None:
    """v14: add the commitments table.

    The LLM already extracts commitments (promises made by/to people); before this
    they were parsed and then silently dropped on load because no table or loader
    code existed for them.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS commitments (
            id INTEGER PRIMARY KEY,
            email_id INTEGER REFERENCES emails(id),
            commitment TEXT NOT NULL,
            by_person TEXT,
            to_person TEXT
        )
    """)
    conn.commit()


def migrate_add_sharepoint_attempts(conn: sqlite3.Connection) -> None:
    """v16 — track consecutive failed fetch attempts per SharePoint link so the
    retry pass can give up on permanently-dead links (not-found / access-denied)
    instead of re-attempting them every night forever.
    """
    has_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sharepoint_links'"
    ).fetchone()
    if not has_table:
        return
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sharepoint_links)").fetchall()}
    if "attempts" not in cols:
        conn.execute("ALTER TABLE sharepoint_links ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
    conn.commit()


def migrate_add_visioned_at(conn: sqlite3.Connection) -> None:
    """v15 — track when the Stage-3 vision pass last classified an image.

    classified_at is written only at the Stage-1 INSERT and never touched by the
    vision pass, so it froze at the last new-unique-image insert and misrepresented
    freshness. visioned_at records the last vision LLM run. Nullable: historical
    rows and heuristic-only (signature/noise) images never get a vision run.
    """
    has_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='inline_images'"
    ).fetchone()
    if not has_table:
        return
    cols = {row[1] for row in conn.execute("PRAGMA table_info(inline_images)").fetchall()}
    if "visioned_at" not in cols:
        conn.execute("ALTER TABLE inline_images ADD COLUMN visioned_at TIMESTAMP")
    conn.commit()


def migrate_add_internet_message_id(conn: sqlite3.Connection) -> None:
    """v11 — add the RFC822 Message-ID column for cross-source dedup.

    Source IDs differ between AppleScript (Mail.app local index) and outlook-cli
    (Microsoft Graph base64 ids). The RFC822 Message-ID is the only identifier
    that's the same across both, so we use a partial unique index on it for
    cross-source deduplication while leaving historical rows (which never had
    the imid captured) untouched.
    """
    # Tolerate fixture / partial DBs where the emails table hasn't been
    # created yet — create_database will create it with the column.
    has_emails = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='emails'"
    ).fetchone()
    if not has_emails:
        return

    cols = {row[1] for row in conn.execute("PRAGMA table_info(emails)").fetchall()}
    if "internet_message_id" not in cols:
        conn.execute("ALTER TABLE emails ADD COLUMN internet_message_id TEXT")

    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='emails'"
        ).fetchall()
    }
    if "idx_emails_internet_message_id" not in indexes:
        conn.execute(
            """
            CREATE UNIQUE INDEX idx_emails_internet_message_id
            ON emails(internet_message_id)
            WHERE internet_message_id IS NOT NULL
            """
        )

    conn.commit()


def migrate_fts_content(conn: sqlite3.Connection) -> None:
    """Migrate the emails_fts table to index both summary and content columns.

    Drops and recreates the FTS table and its triggers, then rebuilds the index.

    Args:
        conn: Database connection
    """
    # Drop existing triggers
    conn.execute("DROP TRIGGER IF EXISTS emails_ai")
    conn.execute("DROP TRIGGER IF EXISTS emails_ad")
    conn.execute("DROP TRIGGER IF EXISTS emails_au")

    # Drop existing FTS table
    conn.execute("DROP TABLE IF EXISTS emails_fts")

    # Recreate FTS table with both summary and content
    conn.execute("""
        CREATE VIRTUAL TABLE emails_fts USING fts5(
            summary,
            content,
            content='emails',
            content_rowid='id'
        )
    """)

    # Recreate triggers
    conn.execute("""
        CREATE TRIGGER emails_ai AFTER INSERT ON emails BEGIN
            INSERT INTO emails_fts(rowid, summary, content) VALUES (new.id, new.summary, new.content);
        END
    """)

    conn.execute("""
        CREATE TRIGGER emails_ad AFTER DELETE ON emails BEGIN
            INSERT INTO emails_fts(emails_fts, rowid, summary, content) VALUES('delete', old.id, old.summary, old.content);
        END
    """)

    conn.execute("""
        CREATE TRIGGER emails_au AFTER UPDATE ON emails BEGIN
            INSERT INTO emails_fts(emails_fts, rowid, summary, content) VALUES('delete', old.id, old.summary, old.content);
            INSERT INTO emails_fts(rowid, summary, content) VALUES (new.id, new.summary, new.content);
        END
    """)

    # Rebuild index from existing data
    conn.execute("INSERT INTO emails_fts(emails_fts) VALUES('rebuild')")

    conn.commit()


_SUBJECT_PREFIX_RE = re.compile(
    r"^(Re|Fwd|FW|RE|Απ|Πρ|ΑΠ|ΠΡ)\s*:\s*",
    re.IGNORECASE,
)


def normalize_subject(subject: str) -> str:
    """Strip reply/forward prefixes and normalize a subject for threading."""
    if not subject:
        return ""
    s = subject
    changed = True
    while changed:
        new_s = _SUBJECT_PREFIX_RE.sub("", s, count=1)
        changed = new_s != s
        s = new_s
    return s.strip().lower()


def subject_to_conversation_id(subject: str) -> str:
    """Hash a normalized subject into a conversation_id."""
    normalized = normalize_subject(subject)
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def migrate_add_threads(conn: sqlite3.Connection) -> None:
    """Add thread/conversation columns to existing databases.

    Adds in_reply_to, references, and conversation_id columns if missing.
    Backfills conversation_id for existing emails using subject normalization.
    """
    try:
        conn.execute("ALTER TABLE emails ADD COLUMN in_reply_to TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists
    try:
        conn.execute("ALTER TABLE emails ADD COLUMN references TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE emails ADD COLUMN conversation_id TEXT")
    except sqlite3.OperationalError:
        pass

    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_emails_conversation_id ON emails(conversation_id)"
        )
    except sqlite3.OperationalError:
        pass

    # Backfill conversation_id for rows that don't have one
    cursor = conn.execute("SELECT id, subject FROM emails WHERE conversation_id IS NULL")
    rows = cursor.fetchall()
    for row_id, subject in rows:
        conv_id = subject_to_conversation_id(subject or "")
        conn.execute("UPDATE emails SET conversation_id = ? WHERE id = ?", (conv_id, row_id))

    conn.commit()


def migrate_add_sync_metadata(conn: sqlite3.Connection) -> None:
    """Add sync_metadata table to existing databases."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_metadata (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()


def migrate_add_attachments(conn: sqlite3.Connection) -> None:
    """Add attachments table to existing databases."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY,
            email_id INTEGER REFERENCES emails(id),
            message_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            mime_type TEXT,
            file_size INTEGER,
            file_path TEXT NOT NULL,
            is_inline INTEGER DEFAULT 0,
            exported_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_attachments_email_id ON attachments(email_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_attachments_message_id ON attachments(message_id)")
    conn.commit()


def migrate_add_attachment_content(conn: sqlite3.Connection) -> None:
    """Add attachment_content table and FTS index for extracted attachment text."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS attachment_content (
            id INTEGER PRIMARY KEY,
            attachment_id INTEGER NOT NULL REFERENCES attachments(id),
            extracted_text TEXT,
            extraction_method TEXT,
            extraction_status TEXT NOT NULL,
            extraction_error TEXT,
            extracted_at TEXT,
            summary TEXT,
            language TEXT,
            llm_status TEXT NOT NULL DEFAULT 'pending',
            llm_error TEXT,
            llm_extracted_at TEXT,
            UNIQUE(attachment_id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attachment_content_attachment_id "
        "ON attachment_content(attachment_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attachment_content_extraction_status "
        "ON attachment_content(extraction_status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attachment_content_llm_status "
        "ON attachment_content(llm_status)"
    )

    # FTS5 for full-text search on extracted content
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS attachment_content_fts USING fts5(
            extracted_text, summary,
            content='attachment_content',
            content_rowid='id'
        )
    """)

    # Triggers to keep FTS in sync
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS attachment_content_ai AFTER INSERT ON attachment_content BEGIN
            INSERT INTO attachment_content_fts(rowid, extracted_text, summary)
            VALUES (new.id, new.extracted_text, new.summary);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS attachment_content_ad AFTER DELETE ON attachment_content BEGIN
            INSERT INTO attachment_content_fts(attachment_content_fts, rowid, extracted_text, summary)
            VALUES('delete', old.id, old.extracted_text, old.summary);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS attachment_content_au AFTER UPDATE ON attachment_content BEGIN
            INSERT INTO attachment_content_fts(attachment_content_fts, rowid, extracted_text, summary)
            VALUES('delete', old.id, old.extracted_text, old.summary);
            INSERT INTO attachment_content_fts(rowid, extracted_text, summary)
            VALUES (new.id, new.extracted_text, new.summary);
        END
    """)

    conn.commit()


def migrate_drop_wiki_pages(conn: sqlite3.Connection) -> None:
    """Drop the wiki_pages table, FTS index, indexes, and triggers.

    Idempotent: safe to run on a fresh DB (no wiki tables) or on a legacy DB
    where migrate_add_wiki_pages had run.
    """
    conn.execute("DROP TRIGGER IF EXISTS wiki_pages_ai")
    conn.execute("DROP TRIGGER IF EXISTS wiki_pages_ad")
    conn.execute("DROP TRIGGER IF EXISTS wiki_pages_au")
    conn.execute("DROP INDEX IF EXISTS idx_wiki_pages_slug")
    conn.execute("DROP INDEX IF EXISTS idx_wiki_pages_page_type")
    conn.execute("DROP TABLE IF EXISTS wiki_pages_fts")
    conn.execute("DROP TABLE IF EXISTS wiki_pages")
    conn.commit()


def migrate_add_conversations(conn: sqlite3.Connection) -> None:
    """Add conversation memory tables for Claude Code session history."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY,
            session_id TEXT UNIQUE NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            workspace TEXT,
            project_name TEXT,
            turn_count INTEGER DEFAULT 0,
            summary TEXT,
            topics_summary TEXT,
            created_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversation_turns (
            id INTEGER PRIMARY KEY,
            conversation_id INTEGER REFERENCES conversations(id),
            turn_index INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            speaker TEXT NOT NULL,
            content TEXT NOT NULL,
            content_length INTEGER,
            summary TEXT,
            sentiment TEXT,
            urgency TEXT,
            language TEXT,
            has_code INTEGER DEFAULT 0,
            has_tool_use INTEGER DEFAULT 0,
            UNIQUE(conversation_id, turn_index)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversation_topics (
            conversation_id INTEGER REFERENCES conversations(id),
            topic_id INTEGER REFERENCES topics(id),
            PRIMARY KEY (conversation_id, topic_id)
        )
    """)

    # Extend existing tables with optional conversation FK
    for table in ("decisions", "action_items", "key_facts"):
        try:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN conversation_turn_id "
                f"INTEGER REFERENCES conversation_turns(id)"
            )
        except sqlite3.OperationalError:
            pass  # Column already exists

    # Indexes
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversations_started_at ON conversations(started_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversations_workspace ON conversations(workspace)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversation_turns_conversation_id "
        "ON conversation_turns(conversation_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversation_turns_speaker ON conversation_turns(speaker)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversation_topics_topic_id "
        "ON conversation_topics(topic_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_decisions_conversation_turn_id "
        "ON decisions(conversation_turn_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_action_items_conversation_turn_id "
        "ON action_items(conversation_turn_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_key_facts_conversation_turn_id "
        "ON key_facts(conversation_turn_id)"
    )

    # FTS5 for conversation turns
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS conversation_turns_fts USING fts5(
            summary, content,
            content='conversation_turns',
            content_rowid='id'
        )
    """)

    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS conversation_turns_ai
        AFTER INSERT ON conversation_turns BEGIN
            INSERT INTO conversation_turns_fts(rowid, summary, content)
            VALUES (new.id, new.summary, new.content);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS conversation_turns_ad
        AFTER DELETE ON conversation_turns BEGIN
            INSERT INTO conversation_turns_fts(conversation_turns_fts, rowid, summary, content)
            VALUES('delete', old.id, old.summary, old.content);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS conversation_turns_au
        AFTER UPDATE ON conversation_turns BEGIN
            INSERT INTO conversation_turns_fts(conversation_turns_fts, rowid, summary, content)
            VALUES('delete', old.id, old.summary, old.content);
            INSERT INTO conversation_turns_fts(rowid, summary, content)
            VALUES (new.id, new.summary, new.content);
        END
    """)

    # FTS5 for conversation-level search
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts USING fts5(
            summary, topics_summary,
            content='conversations',
            content_rowid='id'
        )
    """)

    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS conversations_ai
        AFTER INSERT ON conversations BEGIN
            INSERT INTO conversations_fts(rowid, summary, topics_summary)
            VALUES (new.id, new.summary, new.topics_summary);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS conversations_ad
        AFTER DELETE ON conversations BEGIN
            INSERT INTO conversations_fts(conversations_fts, rowid, summary, topics_summary)
            VALUES('delete', old.id, old.summary, old.topics_summary);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS conversations_au
        AFTER UPDATE ON conversations BEGIN
            INSERT INTO conversations_fts(conversations_fts, rowid, summary, topics_summary)
            VALUES('delete', old.id, old.summary, old.topics_summary);
            INSERT INTO conversations_fts(rowid, summary, topics_summary)
            VALUES (new.id, new.summary, new.topics_summary);
        END
    """)

    conn.commit()


def migrate_add_sharepoint_links(conn: sqlite3.Connection) -> None:
    """Add sharepoint_links table for tracking fetched SharePoint URLs.

    Each row tracks one SharePoint URL discovered in an email body, the
    most recent fetch attempt, and (on success) the local file path the
    content was saved to. Idempotent.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sharepoint_links (
            url TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            fetched_at TIMESTAMP,
            fetched_path TEXT,
            last_status TEXT,
            last_attempt_at TIMESTAMP,
            file_name TEXT,
            file_size INTEGER
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sharepoint_message ON sharepoint_links(message_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sharepoint_status ON sharepoint_links(last_status)"
    )
    conn.commit()


def migrate_add_inline_images(conn: sqlite3.Connection) -> None:
    """Add inline_images, occurrences, and sender_signature_index tables.

    Used by the inline-image classifier (Phase 6 of the Outlook ingestion
    plan). inline_images is content-addressed by sha256, occurrences track
    where each image appeared, and sender_signature_index is a denormalized
    rollup that lets the classifier quickly find recurring per-sender
    signature images. Idempotent.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS inline_images (
            sha256 TEXT PRIMARY KEY,
            width INTEGER NOT NULL,
            height INTEGER NOT NULL,
            bytes INTEGER NOT NULL,
            classification TEXT NOT NULL CHECK (classification IN ('noise','signature','content','unclassified')),
            classification_method TEXT NOT NULL,
            classified_at TIMESTAMP NOT NULL,
            vision_description TEXT,
            user_overridden BOOLEAN NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS inline_image_occurrences (
            sha256 TEXT NOT NULL REFERENCES inline_images(sha256) ON DELETE CASCADE,
            message_id TEXT NOT NULL,
            sender_email TEXT NOT NULL,
            position_in_body REAL NOT NULL,
            PRIMARY KEY (sha256, message_id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_occurrences_sender_sha "
        "ON inline_image_occurrences(sender_email, sha256)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sender_signature_index (
            sender_email TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            occurrence_count INTEGER NOT NULL,
            sender_total INTEGER NOT NULL,
            frequency REAL NOT NULL,
            PRIMARY KEY (sender_email, sha256)
        )
    """)
    conn.commit()


def migrate_add_teams(conn: sqlite3.Connection) -> None:
    """Add Teams chats/channels ingestion tables (schema v12)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS teams_chats (
            id INTEGER PRIMARY KEY,
            teams_chat_id TEXT UNIQUE NOT NULL,
            chat_kind TEXT NOT NULL CHECK (chat_kind IN ('oneOnOne','group','meeting','channel')),
            topic TEXT,
            team_uuid TEXT,
            team_name TEXT,
            channel_id TEXT,
            member_mris TEXT,
            last_message_at TEXT,
            first_seen_at TEXT NOT NULL,
            sync_state TEXT,
            ingest_disabled INTEGER NOT NULL DEFAULT 0
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS teams_threads (
            id INTEGER PRIMARY KEY,
            chat_id INTEGER NOT NULL REFERENCES teams_chats(id),
            thread_kind TEXT NOT NULL CHECK (thread_kind IN ('channel_post','chat_session')),
            anchor_message_id TEXT,
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0,
            participants TEXT,
            participant_display_names TEXT,
            title TEXT,
            summary TEXT,
            sentiment TEXT,
            language TEXT,
            extraction_status TEXT NOT NULL DEFAULT 'pending',
            extraction_error TEXT,
            extracted_at TEXT,
            embedding_at TEXT,
            UNIQUE(chat_id, anchor_message_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS teams_messages (
            id INTEGER PRIMARY KEY,
            teams_message_id TEXT UNIQUE NOT NULL,
            chat_id INTEGER NOT NULL REFERENCES teams_chats(id),
            thread_id INTEGER REFERENCES teams_threads(id),
            sender_mri TEXT,
            sender_display_name TEXT,
            sender_person_id INTEGER REFERENCES people(id),
            composed_at TEXT NOT NULL,
            message_type TEXT,
            content_text TEXT,
            content_html TEXT,
            parent_message_id TEXT,                -- raw upstream id (NOT the composite teams_message_id), matches anchor for replies
            is_system INTEGER NOT NULL DEFAULT 0,
            raw_json TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS teams_mri_resolution (
            mri TEXT PRIMARY KEY,
            email TEXT,
            display_name TEXT,
            person_id INTEGER REFERENCES people(id),
            resolved_at TEXT,
            last_attempt_at TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
        )
    """)

    # Additive FK columns on existing tables — same try/except pattern as conversations migration.
    # Tolerate fixture / partial DBs where the parent tables haven't been created yet.
    existing_tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    for table in ("decisions", "action_items", "key_facts"):
        if table not in existing_tables:
            continue
        try:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN teams_thread_id "
                f"INTEGER REFERENCES teams_threads(id)"
            )
        except sqlite3.OperationalError:
            pass  # Column already exists.

    # Indexes
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_teams_chats_active ON teams_chats(last_message_at)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_teams_messages_chat ON teams_messages(chat_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_teams_messages_thread ON teams_messages(thread_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_teams_messages_sender ON teams_messages(sender_person_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_teams_messages_composed ON teams_messages(composed_at)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_teams_threads_chat ON teams_threads(chat_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_teams_threads_status ON teams_threads(extraction_status)"
    )
    if "decisions" in existing_tables:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_decisions_teams_thread_id ON decisions(teams_thread_id)"
        )
    if "action_items" in existing_tables:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_action_items_teams_thread_id ON action_items(teams_thread_id)"
        )
    if "key_facts" in existing_tables:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_key_facts_teams_thread_id ON key_facts(teams_thread_id)"
        )

    # FTS5 virtual tables
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS teams_messages_fts USING fts5(
            content_text, sender_display_name,
            content='teams_messages', content_rowid='id'
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS teams_threads_fts USING fts5(
            title, summary,
            content='teams_threads', content_rowid='id'
        )
    """)

    # FTS triggers — teams_messages
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS teams_messages_ai
        AFTER INSERT ON teams_messages BEGIN
            INSERT INTO teams_messages_fts(rowid, content_text, sender_display_name)
            VALUES (new.id, new.content_text, new.sender_display_name);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS teams_messages_ad
        AFTER DELETE ON teams_messages BEGIN
            INSERT INTO teams_messages_fts(teams_messages_fts, rowid, content_text, sender_display_name)
            VALUES('delete', old.id, old.content_text, old.sender_display_name);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS teams_messages_au
        AFTER UPDATE ON teams_messages BEGIN
            INSERT INTO teams_messages_fts(teams_messages_fts, rowid, content_text, sender_display_name)
            VALUES('delete', old.id, old.content_text, old.sender_display_name);
            INSERT INTO teams_messages_fts(rowid, content_text, sender_display_name)
            VALUES (new.id, new.content_text, new.sender_display_name);
        END
    """)

    # FTS triggers — teams_threads
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS teams_threads_ai
        AFTER INSERT ON teams_threads BEGIN
            INSERT INTO teams_threads_fts(rowid, title, summary)
            VALUES (new.id, new.title, new.summary);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS teams_threads_ad
        AFTER DELETE ON teams_threads BEGIN
            INSERT INTO teams_threads_fts(teams_threads_fts, rowid, title, summary)
            VALUES('delete', old.id, old.title, old.summary);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS teams_threads_au
        AFTER UPDATE ON teams_threads BEGIN
            INSERT INTO teams_threads_fts(teams_threads_fts, rowid, title, summary)
            VALUES('delete', old.id, old.title, old.summary);
            INSERT INTO teams_threads_fts(rowid, title, summary)
            VALUES (new.id, new.title, new.summary);
        END
    """)

    conn.commit()


def migrate_add_calendar(conn: sqlite3.Connection) -> None:
    """Add calendar events tables and extend decisions/action_items (schema v13)."""
    # Main calendar events table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calendar_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            outlook_event_id TEXT NOT NULL UNIQUE,
            subject TEXT,
            body TEXT,
            body_summary TEXT,
            organizer_email TEXT,
            organizer_name TEXT,
            start_at TIMESTAMP NOT NULL,
            end_at TIMESTAMP NOT NULL,
            location TEXT,
            is_recurring BOOLEAN NOT NULL DEFAULT 0,
            recurrence_master_id TEXT,
            response_status TEXT,
            is_self_organized BOOLEAN NOT NULL DEFAULT 0,
            is_cancelled BOOLEAN NOT NULL DEFAULT 0,
            created_at TIMESTAMP,
            modified_at TIMESTAMP,
            body_extracted_at TIMESTAMP,
            ingested_at TIMESTAMP NOT NULL
        )
    """)

    # Event attendees
    conn.execute("""
        CREATE TABLE IF NOT EXISTS event_attendees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL REFERENCES calendar_events(id) ON DELETE CASCADE,
            person_id INTEGER REFERENCES people(id),
            email TEXT NOT NULL,
            name TEXT,
            response_status TEXT NOT NULL,
            is_organizer BOOLEAN NOT NULL DEFAULT 0,
            is_self BOOLEAN NOT NULL DEFAULT 0
        )
    """)

    # Extend decisions and action_items with event_id FK
    existing_tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }

    for table in ("decisions", "action_items"):
        if table not in existing_tables:
            continue
        # Check if column already exists
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "event_id" not in cols:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN event_id INTEGER REFERENCES calendar_events(id)"
            )

    # Indexes
    conn.execute("CREATE INDEX IF NOT EXISTS idx_calendar_start ON calendar_events(start_at)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_calendar_master ON calendar_events(recurrence_master_id)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_attendees_event ON event_attendees(event_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_attendees_person ON event_attendees(person_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_attendees_email ON event_attendees(email)")

    if "decisions" in existing_tables:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_decisions_event ON decisions(event_id)")
    if "action_items" in existing_tables:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_actions_event ON action_items(event_id)")

    # FTS5 virtual table for calendar events
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS calendar_events_fts USING fts5(
            subject, body_summary,
            content='calendar_events',
            content_rowid='id'
        )
    """)

    # FTS triggers
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS calendar_events_ai
        AFTER INSERT ON calendar_events BEGIN
            INSERT INTO calendar_events_fts(rowid, subject, body_summary)
            VALUES (new.id, new.subject, new.body_summary);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS calendar_events_ad
        AFTER DELETE ON calendar_events BEGIN
            INSERT INTO calendar_events_fts(calendar_events_fts, rowid, subject, body_summary)
            VALUES('delete', old.id, old.subject, old.body_summary);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS calendar_events_au
        AFTER UPDATE ON calendar_events BEGIN
            INSERT INTO calendar_events_fts(calendar_events_fts, rowid, subject, body_summary)
            VALUES('delete', old.id, old.subject, old.body_summary);
            INSERT INTO calendar_events_fts(rowid, subject, body_summary)
            VALUES (new.id, new.subject, new.body_summary);
        END
    """)

    conn.commit()


def get_connection(db_path: str) -> sqlite3.Connection:
    """Open an existing SQLite database.

    Args:
        db_path: Path to the SQLite database file

    Returns:
        sqlite3.Connection object

    Raises:
        sqlite3.Error: If database doesn't exist or can't be opened
    """
    if not Path(db_path).exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Multiple launchd jobs (daily-sync, calendar-sync, teams-sync, outlook-sync,
    # curate-docs) can hit brain.db concurrently after a missed-fire wake-up.
    # Without a busy timeout, the second writer fails immediately with
    # "database is locked" (observed: calendar-sync at 19:00 after auth-watch
    # restoration trigger). 60s is long enough to outlast any single transaction.
    conn.execute("PRAGMA busy_timeout = 60000")
    # WAL journal + NORMAL sync so readers never block the writer, and a rebuilt DB
    # (VPS reinstall, fresh clone) doesn't silently revert to DELETE mode — the
    # historical "database is locked" cause. WAL persists as a file property;
    # synchronous is per-connection, so both are (re)applied on every open.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn
