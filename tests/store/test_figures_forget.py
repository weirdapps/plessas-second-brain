"""A forgotten document takes its figures with it, and its series is read again without it.

`forget_documents` (src/store/forget.py) deletes a document's attachments: the session-note
replacement, the SharePoint shells and the twin documents all go through it. Its figures,
restated values and edition record go too, so no figure cites a missing attachment, and the
series it belonged to is rebuilt from the editions left: a period only the forgotten edition
stated last falls back to the edition before; a series left with fewer than three editions
holds no figures; a copy of the forgotten bytes becomes the edition in its place.
"""

from datetime import datetime

import openpyxl
import pytest

from src.extract.attachment_extractors import extract_text_from_file
from src.store import figures
from src.store.forget import forget_documents
from src.store.schema import create_database
from tests import factories as f

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture
def conn(tmp_path):
    connection = create_database(str(tmp_path / "brain.db"))
    yield connection
    connection.close()


def _edition(
    conn, tmp_path, month: int, august: float, *, sha: str | None = None
) -> tuple[int, int]:
    """An edition of a monthly report as a document: (its message id, its attachment id)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Product", *[datetime(2026, m, 28) for m in range(7, month + 1)]])
    ws.append(["Consumer loans", *[august if m == 8 else 100 + m for m in range(7, month + 1)]])
    book = tmp_path / f"book-{month}-{sha}.xlsx"
    wb.save(book)
    message_id = -(month * 10 + (1 if sha else 0))
    email_id = f.email(
        conn,
        subject=f"[Document] report {month}",
        message_id=message_id,
        date_received=f"2026-{month:02d}-28T08:00:00Z",
        mailbox_name="External",
    )
    attachment_id = f.attachment(
        conn,
        email_id=email_id,
        filename=f"Digital sales 2026-{month:02d}.xlsx",
        text=extract_text_from_file(str(book), XLSX)["text"],
        sha256=sha or f"sha-{month}",
        content_columns={"extraction_method": "openpyxl"},
    )
    return message_id, attachment_id


def _built(conn):
    figures.register_editions(conn)
    figures.parse_pending(conn)


def _cited(conn, attachment_id: int) -> int:
    return sum(
        conn.execute(
            f"SELECT COUNT(*) FROM {t} WHERE attachment_id = ?", (attachment_id,)
        ).fetchone()[0]
        for t in ("figure_values", "figure_restated", "figure_editions")
    )


def _august(conn) -> dict:
    out = figures.search_figures(conn, "consumer loans", period="2026-08")
    return out["result"][0]["latest"]


def test_a_forgotten_edition_leaves_no_figure_and_its_period_falls_back(conn, tmp_path):
    for month in (8, 9, 10):
        _edition(conn, tmp_path, month, 0.70)
    newest, newest_id = _edition(conn, tmp_path, 11, 0.75)  # restates August
    _built(conn)
    assert _august(conn)["value"] == 0.75

    forget_documents(conn, [newest])

    assert _cited(conn, newest_id) == 0
    assert _august(conn)["value"] == 0.70
    conn.execute("INSERT INTO figure_metrics_fts(figure_metrics_fts) VALUES('integrity-check')")


def test_a_series_left_with_two_editions_holds_no_figures(conn, tmp_path):
    ids = [_edition(conn, tmp_path, month, 0.70)[0] for month in (8, 9, 10)]
    _built(conn)

    forget_documents(conn, [ids[-1]])

    assert figures.list_series(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM figure_values").fetchone()[0] == 0


def test_an_edition_forgotten_before_the_registry_followed_is_pruned(conn, tmp_path):
    """An attachment deleted before forget_documents took figures with it left them behind;
    figures-backfill takes them out before it reads anything."""
    for month in (8, 9, 10):
        _edition(conn, tmp_path, month, 0.70)
    _, newest_id = _edition(conn, tmp_path, 11, 0.75)
    _built(conn)
    conn.execute("DELETE FROM attachment_content WHERE attachment_id = ?", (newest_id,))
    conn.execute("DELETE FROM attachments WHERE id = ?", (newest_id,))
    conn.commit()

    stats = figures.forget_orphans(conn)

    assert stats["editions"] == 1 and _cited(conn, newest_id) == 0
    assert _august(conn)["value"] == 0.70


def test_a_copy_of_the_forgotten_bytes_becomes_the_edition(conn, tmp_path):
    for month in (8, 9):
        _edition(conn, tmp_path, month, 0.70)
    original, original_id = _edition(conn, tmp_path, 10, 0.70, sha=None)
    sha = conn.execute("SELECT sha256 FROM attachments WHERE id = ?", (original_id,)).fetchone()[0]
    _, copy_id = _edition(conn, tmp_path, 10, 0.70, sha=sha)
    _built(conn)
    assert (
        conn.execute(
            "SELECT status FROM figure_editions WHERE attachment_id = ?", (copy_id,)
        ).fetchone()[0]
        == "copy"
    )

    forget_documents(conn, [original])

    status = conn.execute(
        "SELECT status, copy_of FROM figure_editions WHERE attachment_id = ?", (copy_id,)
    ).fetchone()
    assert tuple(status) == ("parsed", None)
    assert figures.list_series(conn)[0]["editions"] == 3
    assert _august(conn)["source"] == f"attachment:{copy_id}"
