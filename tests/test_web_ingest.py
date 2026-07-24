"""Tests for web content ingestion — frontmatter parsing, URL fetching, YouTube."""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from src.extract.web_ingest import (
    _extract_youtube_id,
    parse_frontmatter,
)
from src.store.schema import create_database


class TestParseFrontmatter:
    def test_parses_yaml_frontmatter(self):
        content = "---\ntitle: My Article\nsource: https://example.com\n---\n\n# Content here"
        meta, body = parse_frontmatter(content)
        assert meta["title"] == "My Article"
        assert meta["source"] == "https://example.com"
        assert body.startswith("# Content here")

    def test_no_frontmatter(self):
        content = "# Just markdown\n\nNo frontmatter here."
        meta, body = parse_frontmatter(content)
        assert meta == {}
        assert body == content

    def test_obsidian_clipping_format(self):
        content = '---\ntitle: "Bitcoin Guide"\nsource: "https://bitcoinmagazine.com"\nauthor:\n  - "[[Bitcoin Magazine]]"\npublished: 2024-10-15\ntags:\n  - "clippings"\n---\n## Learn Bitcoin'
        meta, body = parse_frontmatter(content)
        assert meta["title"] == "Bitcoin Guide"
        assert meta["source"] == "https://bitcoinmagazine.com"
        assert body.startswith("## Learn Bitcoin")

    def test_frontmatter_with_missing_fields(self):
        content = "---\ntitle: Just a Title\n---\nBody"
        meta, body = parse_frontmatter(content)
        assert meta["title"] == "Just a Title"
        assert meta.get("source") is None


class TestFetchUrl:
    @patch("src.extract.web_ingest.httpx.get")
    def test_fetches_html_page(self, mock_get):
        from src.extract.web_ingest import fetch_url

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/html; charset=utf-8"}
        mock_response.text = "<html><body><h1>Hello</h1><p>World</p></body></html>"
        mock_get.return_value = mock_response

        result = fetch_url("https://example.com/article")
        assert result["content"] is not None
        assert result["content_type"] == "text/html"
        assert result["url"] == "https://example.com/article"

    @patch("src.extract.web_ingest.httpx.get")
    def test_fetches_pdf(self, mock_get):
        from src.extract.web_ingest import fetch_url

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/pdf"}
        mock_response.content = b"%PDF-1.4 fake content"
        mock_get.return_value = mock_response

        result = fetch_url("https://example.com/report.pdf")
        assert result["content_type"] == "application/pdf"
        assert result["is_binary"] is True

    @patch("src.extract.web_ingest.httpx.get")
    def test_raises_on_http_error(self, mock_get):
        from src.extract.web_ingest import fetch_url

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = Exception("404 Not Found")
        mock_get.return_value = mock_response

        with pytest.raises(Exception, match="404"):
            fetch_url("https://example.com/missing")


class TestIngestUrl:
    @patch("src.extract.web_ingest.fetch_url")
    def test_ingests_html_url(self, mock_fetch, tmp_path):
        from src.extract.web_ingest import ingest_url

        mock_fetch.return_value = {
            "url": "https://example.com/bitcoin-guide",
            "content": "<html><body><h1>Bitcoin</h1><p>A guide to BTC.</p></body></html>",
            "content_type": "text/html",
            "is_binary": False,
        }

        db_path = str(tmp_path / "test.db")
        conn = create_database(db_path)
        conn.close()

        result = ingest_url(
            "https://example.com/bitcoin-guide",
            title="Bitcoin Guide",
            db_path=db_path,
        )

        assert not result["skipped"]
        assert result["email_id"] > 0
        assert result["attachment_id"] > 0

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT subject, content, sender_name FROM emails WHERE id = ?",
            (result["email_id"],),
        ).fetchone()
        assert "Bitcoin Guide" in row[0]
        assert "example.com" in row[1]
        assert row[2] == "Web Import"
        conn.close()


class TestIngestMarkdownWithFrontmatter:
    def test_ingest_md_uses_frontmatter_title(self, tmp_path):
        from src.extract.attachment_pipeline import ingest_document

        md_file = tmp_path / "article.md"
        md_file.write_text(
            '---\ntitle: "My Great Article"\nsource: "https://example.com"\n---\n\n# Article content'
        )

        db_path = str(tmp_path / "test.db")
        conn = create_database(db_path)
        conn.close()

        result = ingest_document(str(md_file), db_path=db_path)

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT subject, content FROM emails WHERE id = ?",
            (result["email_id"],),
        ).fetchone()
        assert "My Great Article" in row[0]
        assert "example.com" in row[1]
        conn.close()


class TestExtractYoutubeId:
    def test_standard_watch_url(self):
        assert _extract_youtube_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_short_url(self):
        assert _extract_youtube_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_embed_url(self):
        assert _extract_youtube_id("https://www.youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_shorts_url(self):
        assert _extract_youtube_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_mobile_url(self):
        assert _extract_youtube_id("https://m.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_non_youtube_url(self):
        assert _extract_youtube_id("https://example.com/watch?v=abc") is None

    def test_watch_with_extra_params(self):
        assert (
            _extract_youtube_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=120")
            == "dQw4w9WgXcQ"
        )


class TestIngestYoutube:
    @patch("src.extract.web_ingest._fetch_youtube_transcript")
    @patch("src.extract.web_ingest._fetch_youtube_title")
    def test_ingests_youtube_transcript(self, mock_title, mock_transcript, tmp_path):
        from src.extract.web_ingest import ingest_youtube

        mock_transcript.return_value = "Hello this is a test transcript about Python programming."
        mock_title.return_value = "Learn Python - YouTube"

        db_path = str(tmp_path / "test.db")
        conn = create_database(db_path)
        conn.close()

        result = ingest_youtube(
            "https://www.youtube.com/watch?v=abc123",
            db_path=db_path,
        )

        assert not result["skipped"]
        assert result["email_id"] > 0
        assert result["url"] == "https://www.youtube.com/watch?v=abc123"

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT subject, content, sender_name FROM emails WHERE id = ?",
            (result["email_id"],),
        ).fetchone()
        assert "Learn Python" in row[0]
        assert "youtube.com" in row[1]
        assert row[2] == "YouTube Import"
        conn.close()

    @patch("src.extract.web_ingest._fetch_youtube_transcript")
    def test_raises_on_no_transcript(self, mock_transcript):
        from src.extract.web_ingest import ingest_youtube

        mock_transcript.return_value = None

        with pytest.raises(ValueError, match="No transcript"):
            ingest_youtube("https://www.youtube.com/watch?v=abc123")

    def test_raises_on_invalid_url(self):
        from src.extract.web_ingest import ingest_youtube

        with pytest.raises(ValueError, match="Could not extract"):
            ingest_youtube("https://example.com/not-youtube")

    @patch("src.extract.web_ingest._extract_youtube_id")
    @patch("src.extract.web_ingest.fetch_url")
    def test_ingest_url_routes_youtube(self, mock_fetch, mock_yt_id):
        """ingest_url auto-detects YouTube and routes to ingest_youtube."""
        from src.extract.web_ingest import ingest_url

        mock_yt_id.return_value = "abc123"

        with patch("src.extract.web_ingest.ingest_youtube") as mock_ingest_yt:
            mock_ingest_yt.return_value = {
                "skipped": False,
                "url": "https://youtube.com/watch?v=abc123",
            }
            result = ingest_url("https://www.youtube.com/watch?v=abc123")
            mock_ingest_yt.assert_called_once()
            assert result["url"] == "https://youtube.com/watch?v=abc123"
            mock_fetch.assert_not_called()
