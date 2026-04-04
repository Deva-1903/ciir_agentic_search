"""Tests for URL normalization, deduplication, and filtering."""

import pytest
from app.utils.url import dedupe_urls, extract_domain, is_useful_url, normalize_url


class TestNormalizeUrl:
    def test_strips_fragment(self):
        assert normalize_url("https://example.com/page#section") == "https://example.com/page"

    def test_lowercases_scheme_and_host(self):
        assert normalize_url("HTTPS://Example.COM/Path") == "https://example.com/Path"

    def test_strips_trailing_slash(self):
        assert normalize_url("https://example.com/page/") == "https://example.com/page"

    def test_preserves_query(self):
        url = "https://example.com/search?q=test"
        assert "q=test" in normalize_url(url)

    def test_handles_garbage_gracefully(self):
        result = normalize_url("not-a-url")
        assert isinstance(result, str)


class TestIsUsefulUrl:
    def test_accepts_http(self):
        assert is_useful_url("https://example.com/article")

    def test_rejects_pdf(self):
        assert not is_useful_url("https://example.com/report.pdf")

    def test_rejects_youtube(self):
        assert not is_useful_url("https://www.youtube.com/watch?v=abc123")

    def test_rejects_twitter(self):
        assert not is_useful_url("https://twitter.com/someone")

    def test_rejects_non_http(self):
        assert not is_useful_url("ftp://example.com/file")

    def test_rejects_image(self):
        assert not is_useful_url("https://example.com/logo.png")


class TestExtractDomain:
    def test_strips_www(self):
        assert extract_domain("https://www.example.com/page") == "example.com"

    def test_returns_domain(self):
        assert extract_domain("https://techcrunch.com/article") == "techcrunch.com"

    def test_handles_empty(self):
        assert extract_domain("") == ""


class TestDedupeUrls:
    def test_removes_duplicates(self):
        urls = [
            "https://example.com/page",
            "https://example.com/page#section",  # same after norm
            "https://other.com/article",
        ]
        result = dedupe_urls(urls)
        assert len(result) == 2

    def test_filters_junk(self):
        urls = [
            "https://good.com/article",
            "https://youtube.com/watch?v=123",
            "https://good.com/other",
        ]
        result = dedupe_urls(urls)
        assert all("youtube" not in u for u in result)

    def test_preserves_order(self):
        urls = [
            "https://alpha.com/",
            "https://beta.com/",
            "https://gamma.com/",
        ]
        result = dedupe_urls(urls)
        assert result[0].startswith("https://alpha.com")
