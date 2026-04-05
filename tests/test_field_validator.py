"""Tests for field validation + normalization."""

from __future__ import annotations

from app.services.field_validator import (
    normalize_website,
    validate_and_normalize,
    validate_phone,
    validate_rating,
)


class TestWebsite:
    def test_company_subpage_collapses_to_homepage(self):
        out, ok = normalize_website("HTTPS://Lucali.COM/menu")
        assert ok
        assert out == "https://lucali.com"

    def test_bare_domain_gets_https_prefix(self):
        out, ok = normalize_website("lucali.com")
        assert ok
        assert out == "https://lucali.com"

    def test_bare_domain_with_path_ok(self):
        out, ok = normalize_website("www.lucali.com/about")
        assert ok
        assert out == "https://www.lucali.com"

    def test_fragment_without_tld_rejected(self):
        # This is the `robertaspizza` case from Iteration 7.
        _, ok = normalize_website("robertaspizza")
        assert not ok

    def test_empty_rejected(self):
        _, ok = normalize_website("")
        assert not ok

    def test_drops_trailing_slash_fragment_and_canonicalizes_homepage(self):
        out, ok = normalize_website("https://x.com/about/#top")
        assert ok
        assert out == "https://x.com"

    def test_rejects_editorial_article_url_as_website(self):
        _, ok = normalize_website(
            "https://techcrunch.com/2026/04/04/company-launches-agent",
            source_url="https://techcrunch.com/2026/04/04/company-launches-agent",
            source_title="Company launches agent | TechCrunch",
        )
        assert not ok

    def test_rejects_directory_listing_url_as_website(self):
        _, ok = normalize_website(
            "https://www.ycombinator.com/companies/industry/healthcare-it",
            source_url="https://www.ycombinator.com/companies/industry/healthcare-it",
            source_title="Healthcare IT Companies | Y Combinator",
        )
        assert not ok

    def test_official_blog_url_collapses_to_homepage(self):
        out, ok = normalize_website(
            "https://www.owkin.com/blog/new-model-launch",
            source_url="https://www.owkin.com/blog/new-model-launch",
            source_title="Owkin Blog",
        )
        assert ok
        assert out == "https://www.owkin.com"


class TestPhone:
    def test_standard_us_format_accepted(self):
        _, ok = validate_phone("(718) 555-1234")
        assert ok

    def test_international_accepted(self):
        _, ok = validate_phone("+44 20 7946 0958")
        assert ok

    def test_short_rejected(self):
        _, ok = validate_phone("555")
        assert not ok

    def test_empty_rejected(self):
        _, ok = validate_phone("")
        assert not ok


class TestRating:
    def test_simple_number_accepted(self):
        _, ok = validate_rating("4.7")
        assert ok

    def test_with_units_accepted(self):
        _, ok = validate_rating("4.5/5")
        assert ok

    def test_too_high_rejected(self):
        _, ok = validate_rating("42")
        assert not ok

    def test_no_number_rejected(self):
        _, ok = validate_rating("excellent")
        assert not ok


class TestDispatch:
    def test_unknown_column_passes_through(self):
        out, ok = validate_and_normalize("address", "575 Henry St")
        assert ok
        assert out == "575 Henry St"

    def test_website_column_dispatches_to_website_validator(self):
        _, ok = validate_and_normalize("website", "not a url")
        assert not ok

    def test_homepage_alias_routes_to_website(self):
        out, ok = validate_and_normalize("homepage", "x.com")
        assert ok
        assert out.startswith("https://")

    def test_empty_string_rejected(self):
        _, ok = validate_and_normalize("address", "   ")
        assert not ok
