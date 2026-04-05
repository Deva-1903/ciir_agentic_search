"""Tests for lightweight query normalization."""

from app.services.query_normalizer import normalize_query


def test_normalize_query_trims_and_collapses_noise():
    result = normalize_query("  top   pizza places in Brooklyn,, NY!!  ")
    assert result.original_query == "top   pizza places in Brooklyn,, NY!!"
    assert result.normalized_query == "top pizza places in Brooklyn, NY"


def test_normalize_query_fixes_safe_location_typo():
    result = normalize_query("best coffee shops in amhersrt MA")
    assert result.normalized_query == "best coffee shops in Amherst MA"


def test_normalize_query_fixes_safe_category_typos():
    result = normalize_query("best restraunts in brookln")
    assert result.normalized_query == "best restaurants in Brooklyn"
