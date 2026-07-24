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
