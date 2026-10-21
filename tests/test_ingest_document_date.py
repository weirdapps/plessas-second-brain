"""A standalone document is dated by its file's mtime in UTC, as mail is.

ingest_document wrote the mtime as the host's wall time with no zone, which every reader takes
for UTC: in Athens two or three hours late (schema v41 converted the 2,145 rows stored so).
"""

import os
import sqlite3
from datetime import UTC, datetime

import pytest

from src.extract.attachment_pipeline import ingest_document
from src.store.schema import create_database
from tests.local_zone import local_zone


@pytest.fixture
def db_path(tmp_path, monkeypatch) -> str:
    path = tmp_path / "brain.db"
    create_database(str(path)).close()
    monkeypatch.setattr("src.extract.attachment_pipeline.ATTACHMENTS_DIR", tmp_path / "att")
    return str(path)


def _date(db_path: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT date_received FROM emails").fetchone()[0]
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("mtime", "stored"),
    [
        # Summer in Athens: 08:39:28 wall time is 05:39:28 UTC.
        (datetime(2026, 4, 8, 5, 39, 28, tzinfo=UTC).timestamp(), "2026-04-08T05:39:28Z"),
        (
            datetime(2024, 7, 15, 14, 11, 50, 754618, tzinfo=UTC).timestamp(),
            "2024-07-15T14:11:50.754618Z",
        ),
    ],
)
def test_a_document_is_dated_by_its_mtime_in_utc(db_path, tmp_path, mtime, stored):
    path = tmp_path / "notes.txt"
    path.write_text("figures for the quarter")
    os.utime(path, (mtime, mtime))

    with local_zone("Europe/Athens"):
        ingest_document(str(path), db_path=db_path)

    assert _date(db_path) == stored
