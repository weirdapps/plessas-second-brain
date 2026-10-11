"""A keyword-search stage costs in proportion to its matches, not their square.

The content, whole-row and attachment stages of the waterfall pick a page of threads
in a CTE, then read the snippets of those picks in the query around it. SQLite ran
the CTE as a co-routine, again for every row the outer MATCH yields, so a stage cost
the square of its matches: one search took 17 seconds on a replica where its picks
alone took 15 milliseconds. Greek query stemming, which matches several forms of a
word, made such stages common. The words below are synthetic.
"""

import pytest

from src.store.query import query_by_keyword
from src.store.schema import create_database


def _matching_store(n: int, stage: str):
    """A store where `n` emails match "okapi zebra" only in `stage` of the waterfall."""
    conn = create_database(":memory:")
    for i in range(1, n + 1):
        subject, summary, content = f"Report {i}", f"Note {i}", f"Note {i}"
        if stage == "content":
            content = f"The okapi and the zebra {i}"
        elif stage == "mixed":
            subject, content = f"Okapi {i}", f"The zebra {i}"
        conn.execute(
            "INSERT INTO emails (id, message_id, date_received, subject, summary, content) "
            "VALUES (?, ?, '2026-09-01T10:00:00Z', ?, ?, ?)",
            (i, i, subject, summary, content),
        )
        if stage == "attachment":
            conn.execute(
                "INSERT INTO attachments (id, email_id, message_id, filename, file_path, "
                "exported_at) VALUES (?, ?, ?, 'a.pdf', 'a.pdf', '2026-09-01T10:00:00Z')",
                (i, i, i),
            )
            conn.execute(
                "INSERT INTO attachment_content (attachment_id, extracted_text, "
                "extraction_status) VALUES (?, ?, 'extracted')",
                (i, f"The okapi and the zebra {i}"),
            )
    conn.commit()
    return conn


def _search_steps(conn) -> int:
    """SQLite virtual-machine steps, in hundreds, that one keyword search takes."""
    ticks = [0]

    def tick():
        ticks[0] += 1
        return 0

    conn.set_progress_handler(tick, 100)
    try:
        assert query_by_keyword(conn, "okapi zebra", limit=10)
    finally:
        conn.set_progress_handler(None, 0)
    return ticks[0]


@pytest.mark.parametrize("stage", ["content", "mixed", "attachment"])
def test_a_stage_costs_in_proportion_to_its_matches(stage):
    """Twice the matches, less than three times the work; the square made it four."""
    steps = [_search_steps(_matching_store(n, stage)) for n in (60, 120)]

    assert steps[1] < 3 * steps[0], steps
