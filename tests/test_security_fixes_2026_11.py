"""Regression tests for the 2026-11-01 security audit fixes."""

import json
import re
import time
from types import SimpleNamespace

import pytest

from src import mcp_http
from src.extract import attachment_extractors as ax
from src.store import consolidate
from src.store import query as q
from src.store.figures import _term
from src.store.schema import create_database


def test_a_nul_in_a_query_word_never_reaches_fts5():
    for expression in (q._fts5_word("\x00secret"), q.fts5_phrase("\x00secret"), _term("a\x00b")):
        assert "\x00" not in expression


def test_a_nul_query_searches_instead_of_failing(tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    try:
        for expression, _partial in q.fts5_query_variants("\x00secret plan"):
            conn.execute(
                "SELECT rowid FROM emails_fts WHERE emails_fts MATCH ?", (expression,)
            ).fetchall()
    finally:
        conn.close()


@pytest.mark.parametrize(
    "xml",
    [
        "",
        '<p:sp><p:nvSpPr type="body"/><a:t>x</a:t></p:sp>',
        "<p:spTree><p:sp>a</p:sp>junk<p:sp >b</p:sp><p:sp>unclosed",
        "</p:sp><p:sp>a<p:sp>b</p:sp></p:sp>",
        "<p:spPr>no shape</p:sp>",
    ],
)
def test_shapes_are_the_ones_the_old_pattern_found(xml):
    assert ax._xml_shapes(xml) == re.findall(r"<p:sp\b.*?</p:sp>", xml, flags=re.DOTALL)


def test_unclosed_shapes_take_linear_time():
    started = time.monotonic()
    assert ax._xml_shapes("<p:sp " * 400_000) == []
    assert time.monotonic() - started < 2


def _page(width: float, height: float) -> SimpleNamespace:
    return SimpleNamespace(rect=SimpleNamespace(width=width, height=height))


def test_a_real_page_keeps_200_dpi():
    assert ax._render_dpi(_page(595, 842)) == 200  # A4
    assert ax._render_dpi(_page(2384, 3370)) == 200  # A0


def test_a_huge_page_renders_within_the_pixel_bound():
    for side in (6000, 14400):
        dpi = ax._render_dpi(_page(side, side))
        assert (side * dpi / 72) ** 2 <= ax.PDF_RENDER_MAX_PIXELS
        assert dpi >= 1


def test_revert_writes_back_only_known_columns(tmp_path):
    conn = create_database(str(tmp_path / "brain.db"))
    try:
        prior = {"canonical_id": None, "decision = 'x' WHERE 1=1; --": 1}
        conn.execute(
            "INSERT INTO item_labels (item_table, item_id, label, prior, method, thread_key,"
            " run_id, created_at) VALUES ('decisions', 999, 'duplicate_of', ?, 'test',"
            " 'email:1', 'run-1', '2026-11-01')",
            (json.dumps(prior),),
        )
        assert consolidate.revert(conn, "run-1") == 1
    finally:
        conn.close()


def test_a_token_file_of_another_user_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "mcp-token"
    path.write_text("a" * 64)
    path.chmod(0o600)
    monkeypatch.setattr(mcp_http.os, "getuid", lambda: path.stat().st_uid + 1)
    with pytest.raises(ValueError, match="another user"):
        mcp_http.load_token(str(path))
