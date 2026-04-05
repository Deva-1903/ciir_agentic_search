"""
Tests for generalizable filtering improvements:
  - CTA / action-text entity name rejection
  - Article-title entity name rejection
  - Placeholder value rejection in field_validator
  - Final row cap (strict and non-strict queries)
  - Provider cooldown circuit breaker
  - Official-site name matching precision (tighter body window)
  - Job 404 handling clarity (API layer)
"""

from __future__ import annotations

import time

import pytest

from app.models.schema import Cell, EntityRow, PlannerOutput, ScrapedPage
from app.services.extractor import (
    _PROVIDER_COOLDOWN_SECONDS,
    _provider_failure_time,
    _provider_on_cooldown,
    _record_provider_failure,
)
from app.services.field_validator import validate_and_normalize
from app.services.official_site import _mentions_entity
from app.services.verifier import (
    _looks_like_article_title,
    _looks_like_cta_or_operational,
    verify_rows,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _plan(family: str = "place_venue", entity_type: str = "restaurant") -> PlannerOutput:
    return PlannerOutput(
        query_family=family,
        entity_type=entity_type,
        columns=["name", "address", "website"],
        search_angles=["test"],
    )


def _cell(value: str, url: str = "https://src.com", title: str = "Source", conf: float = 0.9) -> Cell:
    return Cell(
        value=value,
        source_url=url,
        source_title=title,
        evidence_snippet=value,
        confidence=conf,
    )


def _row(entity_id: str, cells_dict: dict, sources: int = 1) -> EntityRow:
    return EntityRow(
        entity_id=entity_id,
        cells={k: _cell(v) for k, v in cells_dict.items()},
        aggregate_confidence=0.85,
        sources_count=sources,
    )


# ── CTA / action-text rejection ───────────────────────────────────────────────


class TestCTARejection:
    @pytest.mark.parametrize("name", [
        "Order Online",
        "Book Now",
        "Reserve a Table",
        "Sign Up Free",
        "Learn More",
        "Get Directions",
        "Call Now",
        "View Menu",
        "Contact Us",
        "Subscribe Now",
        "Try It Free",
        "Shop Now",
        "Get Started",
        "Download Now",
        "Check Availability",
        "Browse All",
        "Apply Now",
    ])
    def test_cta_name_is_recognised(self, name: str):
        assert _looks_like_cta_or_operational(name), f"{name!r} should be recognised as CTA"

    @pytest.mark.parametrize("name", [
        "Lucali",
        "Roberta's Pizza",
        "Di Fara Pizza",
        "The Infatuation",
        "Order of the Phoenix",  # proper noun containing "order"
        "Book Club Cafe",        # proper noun containing "book"
        "Sign of the Times Bar", # proper noun starting with "sign"
    ])
    def test_real_names_not_flagged_as_cta(self, name: str):
        assert not _looks_like_cta_or_operational(name), f"{name!r} should NOT be CTA"

    def test_cta_row_is_rejected_by_verifier(self):
        row = _row("order-online", {"name": "Order Online", "address": "123 Main St"})
        good = _row("lucali", {"name": "Lucali", "address": "575 Henry St", "website": "https://lucali.com"})
        result = verify_rows([row, good], _plan(), "best pizza in Brooklyn")
        entity_ids = [r.entity_id for r in result]
        assert "order-online" not in entity_ids
        assert "lucali" in entity_ids


class TestOperationalTextRejection:
    @pytest.mark.parametrize("name", [
        "Mon-Fri 9am-5pm",
        "Open Daily",
        "Open Monday through Sunday",
        "Closed on Mondays",
        "Free Delivery",
        "Free Shipping on orders over $50",
    ])
    def test_operational_string_is_recognised(self, name: str):
        assert _looks_like_cta_or_operational(name), f"{name!r} should be operational"


# ── Article-title rejection ───────────────────────────────────────────────────


class TestArticleTitleRejection:
    @pytest.mark.parametrize("name", [
        # 4+ word article titles — the real risk class in production output.
        "Best Pizza Places in Brooklyn",
        "Top 10 Startups to Watch",
        "The 7 Best Pizza Spots",
        "Top Restaurants in New York",
        "Most Popular AI Startups Today",
        "Famous Restaurants in Italy",
        "Must-Try Restaurants Near Me",
        "Highest Rated Cafes in London",
        "Greatest Entrepreneurs of All Time",
    ])
    def test_article_title_is_recognised(self, name: str):
        assert _looks_like_article_title(name), f"{name!r} should be article title"

    @pytest.mark.parametrize("name", [
        "Lucali",
        "Di Fara",
        "Best Buy",           # 2-word brand — superlative used as part of proper name
        "Top Hat Lounge",     # 3-word venue — "Top Hat" is compound noun, not superlative
        "Leading Edge Technologies",  # company name
    ])
    def test_real_names_not_flagged_as_article_title(self, name: str):
        assert not _looks_like_article_title(name), f"{name!r} should NOT be article title"

    def test_article_title_row_is_rejected_by_verifier(self):
        fake = _row("best-pizza", {"name": "Best Pizza Places in Brooklyn"})
        real = _row("lucali", {"name": "Lucali", "address": "575 Henry St", "website": "https://lucali.com"})
        result = verify_rows([fake, real], _plan(), "pizza in Brooklyn")
        entity_ids = [r.entity_id for r in result]
        assert "best-pizza" not in entity_ids
        assert "lucali" in entity_ids


# ── Placeholder value rejection ───────────────────────────────────────────────


class TestPlaceholderRejection:
    @pytest.mark.parametrize("value", [
        "N/A", "n/a", "NA",
        "Not specified", "Not Available", "Not Provided", "Not Listed",
        "Unknown", "Unspecified",
        "TBD", "TBA",
        "None", "null", "—", "-", "...", "?",
        "Pending", "Coming Soon",
        "To Be Determined",
    ])
    def test_placeholder_rejected_for_address_column(self, value: str):
        _, ok = validate_and_normalize("address", value)
        assert not ok, f"Placeholder {value!r} should be rejected"

    @pytest.mark.parametrize("value", [
        "575 Henry St",
        "New York, NY",
        "https://lucali.com",
    ])
    def test_real_values_not_rejected(self, value: str):
        _, ok = validate_and_normalize("address", value)
        assert ok, f"Real value {value!r} should be accepted"

    def test_placeholder_website_rejected(self):
        _, ok = validate_and_normalize("website", "Not specified")
        assert not ok

    def test_placeholder_phone_rejected(self):
        _, ok = validate_and_normalize("phone", "Unknown")
        assert not ok


# ── Final row cap ──────────────────────────────────────────────────────────────


class TestFinalRowCap:
    def _many_rows(self, n: int) -> list[EntityRow]:
        rows = []
        for i in range(n):
            rows.append(EntityRow(
                entity_id=f"entity-{i}",
                cells={
                    "name": _cell(f"Entity {i}"),
                    "address": _cell(f"{i} Main St"),
                    "website": _cell(f"https://entity{i}.com"),
                },
                aggregate_confidence=0.85,
                sources_count=2,
                canonical_domain=f"entity{i}.com",
            ))
        return rows

    def test_strict_query_caps_at_15(self):
        rows = self._many_rows(25)
        result = verify_rows(rows, _plan(), "best restaurants in Brooklyn")
        assert len(result) <= 15

    def test_non_strict_query_caps_at_20(self):
        rows = self._many_rows(30)
        result = verify_rows(rows, _plan(), "restaurants in Brooklyn")
        assert len(result) <= 20

    def test_small_result_set_not_capped(self):
        rows = self._many_rows(5)
        result = verify_rows(rows, _plan(), "best restaurants in Brooklyn")
        assert len(result) == 5


# ── Provider cooldown circuit breaker ─────────────────────────────────────────


class TestProviderCooldown:
    def setup_method(self):
        # Ensure clean state before each test.
        _provider_failure_time.clear()

    def test_provider_not_on_cooldown_initially(self):
        assert not _provider_on_cooldown("openai")
        assert not _provider_on_cooldown("groq")

    def test_provider_on_cooldown_after_failure(self):
        _record_provider_failure("openai")
        assert _provider_on_cooldown("openai")

    def test_other_provider_not_affected(self):
        _record_provider_failure("openai")
        assert not _provider_on_cooldown("groq")

    def test_cooldown_expires(self):
        # Record a failure far in the past.
        _provider_failure_time["openai"] = time.monotonic() - (_PROVIDER_COOLDOWN_SECONDS + 1.0)
        assert not _provider_on_cooldown("openai")


# ── Official-site name matching precision ─────────────────────────────────────


class TestOfficialSiteNameMatching:
    def _page(self, url: str, title: str, body: str) -> ScrapedPage:
        return ScrapedPage(
            url=url,
            title=title,
            cleaned_text=body,
        )

    def test_entity_in_title_matches(self):
        page = self._page("https://lucali.com/", "Lucali - Brooklyn Pizza", "Welcome.")
        assert _mentions_entity(page, "lucali")

    def test_entity_in_first_200_chars_matches(self):
        body = "Lucali is a legendary pizzeria in Carroll Gardens. " + "x" * 300
        page = self._page("https://example.com/review", "Pizza Review", body)
        assert _mentions_entity(page, "lucali")

    def test_entity_beyond_200_chars_does_not_match(self):
        # Entity name only appears after the 200-char window — should NOT match.
        body = "A" * 250 + " Lucali is mentioned here only after the window."
        page = self._page("https://example.com/article", "Food Guide", body)
        assert not _mentions_entity(page, "lucali")

    def test_generic_body_text_does_not_match_wrong_entity(self):
        # Page about "Best Pizza in Brooklyn" shouldn't match entity "Roberta's"
        # unless that name is in the title or very early body.
        body = "Brooklyn has great pizza. " * 20 + "Roberta's is one option."
        page = self._page("https://example.com/list", "Best Pizza in Brooklyn", body)
        assert not _mentions_entity(page, "roberta's")

    def test_empty_entity_name_does_not_match(self):
        page = self._page("https://lucali.com/", "Lucali", "Welcome.")
        assert not _mentions_entity(page, "")
