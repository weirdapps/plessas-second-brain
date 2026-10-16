"""Recency fused into keyword ranking, and editions of one report collapsed
(src/store/ranking.py)."""

import pytest

from src.store import ranking
from src.store.ranking import editions_ranked, query_periods, series_key
from tests.patching import unset_env


@pytest.fixture(autouse=True)
def _fused(monkeypatch):
    unset_env(monkeypatch, "BRAIN_QUERY_RANKING")


# --- series keys -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Δείκτης Ζέβρας 31.08.2026.xlsx", "Δείκτης Ζέβρας 30.04.2026.xlsx"),
        ("Monthly KPIs August 2026.pptx", "Monthly KPIs September 2026.pptx"),
        ("Αναφορά Σεπτεμβρίου 2026.docx", "Αναφορά Αυγούστου 2026.docx"),
        ("Q3 2026 results.pdf", "Q2 2026 results.pdf"),
        ("okapi-flows 2026-2024.xlsx", "okapi-flows 2025-2023.xlsx"),
        ("report_20260831.xlsx", "report_20260731.xlsx"),
        ("sales 2026-08.csv", "sales 2026-09.csv"),
        ("10.10 Okapi Weekly.pptx", "17.10 Okapi Weekly.pptx"),
        # The same edition sent twice is one series too.
        ("Δείκτης Ζέβρας 31.08.2026.xlsx", "Δείκτης Ζέβρας 31.08.2026.xlsx"),
    ],
)
def test_names_that_differ_only_by_a_date_or_period_are_one_series(a, b):
    assert series_key(a) is not None
    assert series_key(a) == series_key(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Δείκτης Ζέβρας 31.08.2026.xlsx", "Δείκτης Ζέβρας 31.08.2026.pdf"),
        ("Invoice 12345 2026-08.pdf", "Invoice 12399 2026-09.pdf"),
        ("Part A 31.08.2026.pdf", "Part B 31.08.2026.pdf"),
        ("Budget 2026.xlsx", "Forecast 2026.xlsx"),
    ],
)
def test_other_words_keep_series_apart(a, b):
    assert series_key(a) != series_key(b)


@pytest.mark.parametrize(
    "name", ["image003.png", "report.pdf", "31.08.2026.xlsx", "Strategy 2.0.pptx", "", None]
)
def test_a_name_without_a_date_or_without_words_is_no_series(name):
    """A generic name repeats across unrelated items (image003.png), and a bare date
    names nothing: neither may hide other items."""
    assert series_key(name) is None


def test_subjects_lose_their_reply_prefixes_and_weekdays():
    key = series_key("RE: Daily dashboard 15/10/2026", subject=True)
    assert key == series_key("Daily dashboard 14/10/2026", subject=True)
    assert series_key("Monitor | Tue 9 JUN", subject=True) == series_key(
        "Monitor | Wed 10 JUN", subject=True
    )


def test_query_periods_are_the_dates_a_query_names():
    assert query_periods("δείκτης ζέβρας 31.05.2026") == ["31.05.2026"]
    assert query_periods("budget review") == []


# --- editions_ranked ---------------------------------------------------------------------


def _row(ref, name, date):
    return {"ref": ref, "name": name, "date": date}


def _ranked(rows, limit=10, query=""):
    return editions_ranked(
        rows,
        limit,
        name=lambda r: r["name"],
        date=lambda r: r["date"],
        ref=lambda r: r["ref"],
        query=query,
    )


def _monthly(n=6, start_ref=100):
    """n monthly editions of one workbook, oldest first: refs 100.., months 03..08."""
    return [
        _row(start_ref + i, f"Δείκτης Ζέβρας 28.{3 + i:02d}.2026.xlsx", f"2026-{4 + i:02d}-20")
        for i in range(n)
    ]


def test_editions_collapse_to_the_newest_with_a_count_and_the_refs():
    editions = _monthly()
    rows = [_row(1, "press clipping.pdf", "2026-02-01"), *editions]

    out = _ranked(rows)

    shown = [r for r in out if r["ref"] in {e["ref"] for e in editions}]
    assert [r["ref"] for r in shown] == [105]  # the newest edition stands for them all
    assert shown[0]["older_editions"] == 5
    assert shown[0]["editions"] == [104, 103, 102, 101, 100]  # newest first
    assert "older_editions" not in out[0] or out[0]["ref"] == 105


def test_the_newest_edition_reaches_the_top_five_from_behind_its_older_ones():
    """The audit's case: eight better-matching items, then five older editions sent
    twice each, then the newest edition, 19th and 20th. Collapsed and fused with
    recency, it is in the top 5, even when each better match is newer than the next."""
    others = [_row(i, f"clipping {i}.pdf", f"2026-08-0{9 - i}") for i in range(1, 9)]
    editions = [
        _row(
            100 + 2 * m + copy,
            f"Δείκτης Ζέβρας 28.{3 + m:02d}.2026.xlsx",
            f"2026-{4 + m:02d}-20",
        )
        for m in range(6)
        for copy in (0, 1)
    ]
    rows = [*others, *editions]
    assert [r["ref"] for r in rows].index(110) == 18

    out = _ranked(rows, limit=10)

    refs = [r["ref"] for r in out]
    assert refs.index(110) < 5
    assert out[refs.index(110)]["older_editions"] == 11


def test_a_query_naming_a_date_shows_that_edition():
    out = _ranked(_monthly(), query="δείκτης ζέβρας 28.05.2026")

    assert [r["ref"] for r in out] == [102]
    assert out[0]["older_editions"] == 5


def test_recency_lifts_a_newer_match_but_relevance_still_counts():
    rows = [
        _row(1, "a.pdf", "2024-01-01"),
        _row(2, "b.pdf", "2024-02-01"),
        _row(3, "c.pdf", "2026-10-01"),
    ]

    out = [r["ref"] for r in _ranked(rows)]

    assert out.index(3) < 2  # the newest of three moved up
    assert out.index(1) < out.index(2)  # the better match of two old ones stays ahead


def test_stages_keep_their_order_and_recency_reorders_inside_each():
    """A subject match stays ahead of a body match, however new the body match is."""
    rows = [
        {**_row(1, "s1", "2024-01-01"), "stage": 0},
        {**_row(2, "s2", "2024-02-01"), "stage": 0},
        {**_row(4, "s4", "2026-01-01"), "stage": 0},
        {**_row(3, "c1", "2026-10-01"), "stage": 1},
    ]

    out = editions_ranked(
        rows,
        10,
        name=lambda r: None,
        date=lambda r: r["date"],
        ref=lambda r: r["ref"],
        stage=lambda r: r["stage"],
    )

    # 4, newest of its stage, passes 2; a tie of swapped places goes to relevance.
    assert [r["ref"] for r in out] == [1, 4, 2, 3]


def test_undated_rows_rank_last_for_recency_and_nothing_breaks():
    rows = [_row(1, "a.pdf", None), _row(2, "b.pdf", "2026-01-01")]

    assert {r["ref"] for r in _ranked(rows)} == {1, 2}


def test_the_cut_comes_after_the_collapse():
    """A page of 3 holds 3 series, not 3 editions of one."""
    rows = [*_monthly(), _row(1, "x.pdf", "2026-01-01"), _row(2, "y.pdf", "2026-01-02")]

    out = _ranked(rows, limit=3)

    assert len(out) == 3 and {1, 2, 105} == {r["ref"] for r in out}


def test_relevance_ranking_switch_keeps_the_order_and_adds_nothing(monkeypatch):
    monkeypatch.setenv("BRAIN_QUERY_RANKING", "relevance")
    rows = [*_monthly(), _row(1, "x.pdf", "2026-12-01")]

    out = _ranked(rows, limit=4)

    assert [r["ref"] for r in out] == [100, 101, 102, 103]
    assert all("older_editions" not in r for r in out)
    assert not ranking.fused_ranking()


def test_an_unknown_ranking_value_is_the_default(monkeypatch):
    monkeypatch.setenv("BRAIN_QUERY_RANKING", "bogus")
    assert ranking.fused_ranking()


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("CEO deck v2.pptx", "CEO deck v3.pptx"),
        ("CEO deck v3.pptx", "CEO deck final.pptx"),
        ("CEO deck_FINAL_v2.pptx", "CEO deck draft.pptx"),
        ("Σύμβαση τελικό.docx", "Σύμβαση v1.docx"),
        ("Budget (1).xlsx", "Budget (2).xlsx"),
    ],
)
def test_versions_and_copies_of_a_name_are_one_series(a, b):
    assert series_key(a) is not None
    assert series_key(a) == series_key(b)


def _att(ref, name, date, sha=None):
    return {"ref": ref, "name": name, "date": date, "sha": sha}


def _by_copy(rows, limit=10):
    return editions_ranked(
        rows,
        limit,
        name=lambda r: r["name"],
        date=lambda r: r["date"],
        ref=lambda r: r["ref"],
        copy=lambda r: r["sha"],
    )


def test_copies_of_one_file_are_one_series_whatever_their_names():
    rows = [
        _att(1, "deck.pdf", "2026-03-01", sha="abc"),
        _att(2, "Board deck for the record.pdf", "2026-03-02", sha="abc"),
        _att(3, "other.pdf", "2026-03-03", sha="def"),
    ]

    out = _by_copy(rows)

    assert [r["ref"] for r in out if r["ref"] in (1, 2)] == [2]
    assert {r["ref"] for r in out} == {2, 3}


def test_a_plain_name_joins_its_versions_but_two_plain_names_stay_apart():
    """'CEO deck.pptx' is a version of 'CEO deck v3.pptx'; two unrelated image003.png
    are not copies unless their contents are the same."""
    rows = [
        _att(1, "CEO deck.pptx", "2026-03-01", sha="a"),
        _att(2, "CEO deck v3.pptx", "2026-03-05", sha="b"),
        _att(3, "image003.png", "2026-03-06", sha="c"),
        _att(4, "image003.png", "2026-03-07", sha="d"),
    ]

    out = _by_copy(rows)

    assert {r["ref"] for r in out} == {2, 3, 4}
    assert next(r for r in out if r["ref"] == 2)["editions"] == [1]


def test_the_final_version_stands_for_a_day_of_versions():
    rows = [
        _att(1, "CEO deck v4.pptx", "2026-03-05T18:00:00Z"),
        _att(2, "CEO deck final.pptx", "2026-03-05T09:00:00Z"),
        _att(3, "CEO deck v3.pptx", "2026-03-04T09:00:00Z"),
    ]

    out = _by_copy(rows)

    assert [r["ref"] for r in out] == [2]
    assert out[0]["editions"] == [1, 3]


def test_a_newer_day_beats_an_older_final():
    rows = [
        _att(1, "CEO deck final.pptx", "2026-03-05"),
        _att(2, "CEO deck v5.pptx", "2026-03-09"),
    ]

    assert [r["ref"] for r in _by_copy(rows)] == [2]


def test_an_edition_is_shown_by_its_own_mail_not_a_later_reply_to_an_older_one():
    """A reply to August's report, sent after September's, is not the newest edition."""
    rows = [
        _row(1, "Re: Monthly Okapi Survey August", "2026-10-09"),
        _row(2, "Monthly Okapi Survey September", "2026-10-05"),
        _row(3, "Monthly Okapi Survey August", "2026-09-04"),
    ]

    out = editions_ranked(
        rows,
        10,
        name=lambda r: r["name"],
        date=lambda r: r["date"],
        ref=lambda r: r["ref"],
        subject=True,
    )

    assert [r["ref"] for r in out] == [2]
    assert out[0]["editions"] == [1, 3]


def test_the_pool_is_four_rows_a_row_within_bounds():
    assert ranking.pool_size(5) == 40
    assert ranking.pool_size(20) == 80
    assert ranking.pool_size(50) == 100  # recall's left-out probes
    assert ranking.pool_size(200) == 200  # never fewer than asked


@pytest.mark.parametrize("word", ["ανά", "κατά", "προς", "μέσω", "έως"])
def test_greek_prepositions_are_stopwords(word):
    """'πωλήσεις ανά κανάλι' missed the weekly report that never says ανά: a row need not
    hold a preposition to answer (the qa set's a02 and q026)."""
    from src.store.greek import is_stopword

    assert is_stopword(word)
    assert is_stopword(word.upper())


def test_rows_are_not_mutated_in_place():
    rows = _monthly()
    _ranked(rows)
    assert all("older_editions" not in r for r in rows)
