"""Writers zero what they free.

Deleted text survived in free pages because secure_delete was set in two scripts
alone: forgotten documents, overwritten extractions and dedup deletes stayed in the
file, and in every copy and backup of it, for a page-level read to find.
"""

import sqlite3
import uuid
from pathlib import Path

import pytest

from src.extract import attachment_pipeline
from src.store import dedup_people
from src.store.schema import create_database, get_connection


def _store(tmp_path: Path) -> Path:
    """A real store, closed as `load` leaves it: WAL in its header, no -wal or -shm beside it."""
    data = tmp_path / "data"
    data.mkdir()
    path = data / "brain.db"
    create_database(str(path)).close()
    return path


WRITERS = [
    pytest.param(get_connection, id="schema.get_connection"),
    # The two writers that open the store themselves: attachment text is overwritten
    # and deleted on re-extraction, people rows deleted by the dedup.
    pytest.param(attachment_pipeline._connect, id="attachment_pipeline"),
    pytest.param(dedup_people.get_connection, id="dedup_people"),
]


def _freed_text_survives(conn: sqlite3.Connection, path: Path) -> bool:
    """Store a text long enough to spill onto overflow pages, delete it, and look for it
    in the file once the WAL is copied back. Closes `conn`."""
    marker = f"freed-{uuid.uuid4().hex}"
    try:
        conn.execute("CREATE TABLE scratch (body TEXT)")
        conn.execute("INSERT INTO scratch VALUES (?)", (marker * 300,))
        conn.commit()
        conn.execute("DELETE FROM scratch")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return marker.encode() in path.read_bytes()


@pytest.mark.parametrize("open_writer", WRITERS)
def test_a_writer_leaves_no_deleted_text_in_the_file(tmp_path, open_writer):
    path = _store(tmp_path)
    conn = open_writer(str(path))

    assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
    assert not _freed_text_survives(conn, path)


def test_without_secure_delete_the_deleted_text_stays_in_the_file(tmp_path):
    # The same steps with the pragma off, so the test above shows the pragma does the work.
    path = _store(tmp_path)
    conn = get_connection(str(path))
    conn.execute("PRAGMA secure_delete = OFF")

    assert _freed_text_survives(conn, path)
