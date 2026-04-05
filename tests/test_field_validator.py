"""Tests for field validation + normalization."""

from __future__ import annotations

from app.services.field_validator import (
    normalize_website,
    validate_and_normalize,
    validate_phone,
    validate_rating,
)


class TestWebsite:
    def test_full_url_passes_and_lowercases_host(self):
        out, ok = normalize_website("HTTPS://Lucali.COM/menu")
        assert ok
        assert out == "https://lucali.com/menu"

    def test_bare_domain_gets_https_prefix(self):
        out, ok = normalize_website("lucali.com")
        assert ok
        assert out == "https://lucali.com"

    def test_bare_domain_with_path_ok(self):
        out, ok = normalize_website("www.lucali.com/about")
        assert ok
        assert out.startswith("https://")
        assert "lucali.com" in out

    def test_fragment_without_tld_rejected(self):
        # This is the `robertaspizza` case from Iteration 7.
        _, ok = normalize_website("robertaspizza")
        assert not ok

    def test_empty_rejected(self):
        _, ok = normalize_website("")
        assert not ok

    def test_drops_trailing_slash_and_fragment(self):
        out, ok = normalize_website("https://x.com/about/#top")
        assert ok
        assert out == "https://x.com/about"


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
