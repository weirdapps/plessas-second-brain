"""Tests for SharePoint URL scanner."""

from src.extract.sharepoint_url_scanner import extract_sharepoint_urls


def test_extracts_contoso_sharepoint_url():
    """Extract SharePoint URL from anchor tag."""
    html = '<a href="https://contoso.sharepoint.com/sites/Team/Shared/report.xlsx">link</a>'
    urls = extract_sharepoint_urls(html)
    assert len(urls) == 1
    assert "contoso.sharepoint.com" in urls[0]
    assert "report.xlsx" in urls[0]


def test_extracts_personal_sharepoint_url():
    """Extract bare SharePoint URL from personal OneDrive."""
    html = "Check this: https://contoso-my.sharepoint.com/personal/user/doc.pdf"
    urls = extract_sharepoint_urls(html)
    assert len(urls) == 1
    assert "contoso-my.sharepoint.com" in urls[0]
    assert "doc.pdf" in urls[0]


def test_deduplicates():
    """Deduplicate same URL with and without tracking params."""
    html = """
    <a href="https://contoso.sharepoint.com/sites/Team/doc.pdf">link1</a>
    <a href="https://contoso.sharepoint.com/sites/Team/doc.pdf?web=1">link2</a>
    """
    urls = extract_sharepoint_urls(html)
    assert len(urls) == 1
    assert "doc.pdf" in urls[0]


def test_strips_tracking_suffixes():
    """Remove tracking query parameters."""
    html = "https://contoso.sharepoint.com/sites/Team/file.xlsx?web=1&Source=foo"
    urls = extract_sharepoint_urls(html)
    assert len(urls) == 1
    assert "?" not in urls[0]
    assert "web=" not in urls[0]
    assert "Source=" not in urls[0]


def test_no_false_positives():
    """Don't extract non-SharePoint URLs."""
    html = '<a href="https://google.com/search">search</a>'
    urls = extract_sharepoint_urls(html)
    assert len(urls) == 0


def test_empty_input():
    """Handle empty and None input."""
    assert extract_sharepoint_urls("") == []
    assert extract_sharepoint_urls(None) == []


# --- A mangled URL is not a deleted document ---------------------------------
# The scanner read HTML as if it were plain text: no html.unescape(), and a
# character class that treated the apostrophe as a delimiter. 23 links on prod
# were stored broken and 404ed on every attempt, which the retirement rule then
# read as "the document is gone" and parked them for good. They were never
# gone: 802 other /SitePages/ links fetch fine. Three shapes of damage:
#   (a) "&amp;" survived inside the path of a page whose real title has an "&";
#   (b) an HTML-escaped href left the closing "&quot;" glued to the URL, of
#       which the old rstrip ate only the ";";
#   (c) a page title containing an apostrophe truncated at the apostrophe.


def test_an_escaped_ampersand_in_the_path_is_unescaped():
    """(a) The page title really does contain an "&", so "&amp;" in the path is
    the HTML's escaping of it, not part of the file name."""
    html = (
        '<a href="https://contoso.sharepoint.com/sites/Team/SitePages/'
        'Economy-&amp;-Markets-Snapshot--(2).aspx">link</a>'
    )
    urls = extract_sharepoint_urls(html)
    assert urls == [
        "https://contoso.sharepoint.com/sites/Team/SitePages/Economy-&-Markets-Snapshot--(2).aspx"
    ]


def test_an_html_escaped_href_keeps_no_trailing_quote_entity():
    """(b) Markup that itself arrived escaped: the closing delimiter is the
    five characters "&quot;", which the character class happily swallowed."""
    html = (
        "see &lt;a href=&quot;https://contoso.sharepoint.com/sites/Team/SitePages/"
        "reports(5).aspx&quot; target=&quot;_blank&quot;&gt;link&lt;/a&gt;"
    )
    urls = extract_sharepoint_urls(html)
    assert urls == ["https://contoso.sharepoint.com/sites/Team/SitePages/reports(5).aspx"]


def test_a_page_title_with_an_apostrophe_is_captured_in_full():
    """(c) Truncating at the apostrophe produced a prefix that 404s forever."""
    html = (
        '<a href="https://contoso.sharepoint.com/sites/Team/SitePages/'
        "Sales-Rally-Q2-'2025.aspx\">link</a>"
    )
    urls = extract_sharepoint_urls(html)
    assert urls == ["https://contoso.sharepoint.com/sites/Team/SitePages/Sales-Rally-Q2-'2025.aspx"]


def test_a_single_quoted_href_does_not_swallow_what_follows_it():
    """The apostrophe cannot simply be dropped from the character class: a
    single-quoted href would then run past its own closing delimiter and drag
    the anchor text in with it. Anchoring on the attribute is what fixes (c)."""
    html = "<a href='https://contoso.sharepoint.com/sites/Team/a.aspx'>text</a>"
    urls = extract_sharepoint_urls(html)
    assert urls == ["https://contoso.sharepoint.com/sites/Team/a.aspx"]


def test_a_bare_url_outside_an_href_is_still_found():
    """Anchoring on href= must stay a widening, not a narrowing: plain-text
    SharePoint URLs in message bodies exist and still have to be picked up."""
    html = "plain text https://contoso.sharepoint.com/sites/Team/notes.aspx end"
    urls = extract_sharepoint_urls(html)
    assert urls == ["https://contoso.sharepoint.com/sites/Team/notes.aspx"]


def test_an_already_unescaped_url_is_left_alone():
    """Unescaping is idempotent: a literal "&" already in the path stays one,
    so re-scanning an already-clean URL cannot corrupt it."""
    html = (
        '<a href="https://contoso.sharepoint.com/sites/Team/SitePages/'
        'Economy-&-Markets.aspx">link</a>'
    )
    urls = extract_sharepoint_urls(html)
    assert urls == ["https://contoso.sharepoint.com/sites/Team/SitePages/Economy-&-Markets.aspx"]


# --- Abandoned links must get a second chance --------------------------------
# The retry pass filters on `attempts < MAX_SHAREPOINT_ATTEMPTS`, which makes the
# cap an ABANDONMENT rather than a throttle. Between 2026-07-30 and 2026-08-10
# this tenant's SharePoint auth was broken (MCAS-gated, no bearer issued), so 43
# links burned through their attempts against a fetcher that could not have
# succeeded, and were never tried again — still unfetched nine days after the
# auth was fixed. A cap that can never expire turns any outage longer than five
# nightly runs into permanent data loss.


def _sp_db():
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE sharepoint_links (url TEXT PRIMARY KEY, message_id TEXT, fetched_at TEXT,"
        " fetched_path TEXT, last_status TEXT, last_attempt_at TEXT, file_name TEXT,"
        " file_size INT, attempts INT DEFAULT 0)"
    )
    return db


def _link(db, url, *, status, attempts, last_attempt):
    db.execute(
        "INSERT INTO sharepoint_links (url, message_id, fetched_at, last_status,"
        " last_attempt_at, attempts) VALUES (?, 'm1', NULL, ?, ?, ?)",
        (url, status, last_attempt, attempts),
    )
    db.commit()


def _selected(db, now="2026-08-19T01:00:00+00:00"):
    from src.export.sharepoint_fetcher import retry_candidates

    return [r[0] for r in retry_candidates(db, now=now)]


def test_a_link_under_the_cap_is_retried():
    db = _sp_db()
    _link(
        db, "https://x/a", status="http-error", attempts=2, last_attempt="2026-08-18T23:00:00+00:00"
    )

    assert _selected(db) == ["https://x/a"]


def test_a_link_at_the_cap_is_not_retried_immediately():
    """The cap still throttles — nightly hammering of a dead link is what it is for."""
    db = _sp_db()
    _link(
        db, "https://x/b", status="http-error", attempts=8, last_attempt="2026-08-18T23:00:00+00:00"
    )

    assert _selected(db) == []


def test_a_link_at_the_cap_is_retried_after_the_cool_off():
    """The 43 abandoned on a broken auth path: last tried 2026-08-10, cap hit."""
    db = _sp_db()
    _link(
        db, "https://x/c", status="http-error", attempts=8, last_attempt="2026-08-10T23:00:13+00:00"
    )

    assert _selected(db) == ["https://x/c"]


def test_an_already_fetched_link_is_never_retried():
    db = _sp_db()
    db.execute(
        "INSERT INTO sharepoint_links (url, message_id, fetched_at, last_status, attempts)"
        " VALUES ('https://x/d', 'm1', '2026-08-01T00:00:00+00:00', 'ok', 0)"
    )
    db.commit()

    assert _selected(db) == []


def test_an_unsupported_host_stays_excluded():
    """A permanent external tenant is not a transient failure."""
    db = _sp_db()
    _link(
        db,
        "https://x/e",
        status="unsupported-host",
        attempts=1,
        last_attempt="2026-07-01T00:00:00+00:00",
    )

    assert _selected(db) == []


# --- A deleted document is not a transient failure ---------------------------
# The cool-off exists so an AUTH outage cannot permanently abandon a link: 43
# links burned their attempts against a fetcher that could not have succeeded.
# But it resurrects every capped link indiscriminately, including the 23 whose
# every attempt returned HTTP 404. Those re-enter the pool every 7 days, burn an
# attempt, 404 again, and reset the clock — forever, for a document that no
# longer exists. sharepoint-cli maps not_found -> 'stale' (_ERROR_STATUS), and a
# link that has NEVER fetched OK cannot be "stale" in the re-fetch sense, so
# never-fetched + capped + 'stale' is a gone document, not an outage victim.


def test_a_404_link_at_the_cap_is_not_resurrected_by_the_cool_off():
    """The 23 on prod: attempts exhausted, every one a 404, never fetched."""
    db = _sp_db()
    _link(
        db, "https://x/gone", status="stale", attempts=6, last_attempt="2026-08-10T00:00:00+00:00"
    )

    assert _selected(db) == []


def test_a_404_link_under_the_cap_is_still_retried():
    """A 404 can mean moved or briefly unshared, so it gets its full budget of
    attempts first — only an EXHAUSTED one is treated as gone."""
    db = _sp_db()
    _link(
        db, "https://x/maybe", status="stale", attempts=2, last_attempt="2026-08-10T00:00:00+00:00"
    )

    assert _selected(db) == ["https://x/maybe"]


def test_an_auth_outage_link_is_still_resurrected_by_the_cool_off():
    """Regression guard: the reason the cool-off was added must survive intact."""
    db = _sp_db()
    _link(
        db,
        "https://x/authgone",
        status="auth-required",
        attempts=8,
        last_attempt="2026-08-10T00:00:00+00:00",
    )

    assert _selected(db) == ["https://x/authgone"]


def test_a_previously_fetched_link_that_went_stale_is_still_retried():
    """The genuine 'upstream changed, re-fetch it' case. It has a fetched_at, so
    the retirement rule must not touch it however many attempts it has spent."""
    db = _sp_db()
    db.execute(
        "INSERT INTO sharepoint_links (url, message_id, fetched_at, last_status,"
        " last_attempt_at, attempts) VALUES ('https://x/changed', 'm1',"
        " '2026-07-01T00:00:00+00:00', 'stale', '2026-08-10T00:00:00+00:00', 8)"
    )
    db.commit()

    assert _selected(db) == ["https://x/changed"]
