"""Fuzzy entity deduplication helpers using rapidfuzz."""

from __future__ import annotations

from rapidfuzz import fuzz

from app.utils.text import normalize_name
from app.utils.url import extract_domain


# Threshold for considering two entity names the same entity
_NAME_SIM_THRESHOLD = 82.0


def names_are_similar(a: str, b: str) -> bool:
    """Return True if two entity names likely refer to the same entity."""
    na, nb = normalize_name(a), normalize_name(b)
    if na == nb:
        return True
    # token_set_ratio handles "OpenAI Inc" vs "OpenAI" well
    score = fuzz.token_set_ratio(na, nb)
    return score >= _NAME_SIM_THRESHOLD


def domains_match(url_a: str, url_b: str) -> bool:
    """Return True if two URLs share the same registered domain."""
    da, db = extract_domain(url_a), extract_domain(url_b)
    return bool(da and db and da == db)


def find_matching_entity_idx(
    name: str,
    website: str | None,
    existing: list[dict],  # list of {"name": str, "website": str|None}
) -> int | None:
    """
    Find the index in *existing* that matches (name, website).
    Returns None if no match found.
    """
    for idx, entity in enumerate(existing):
        existing_name: str = entity.get("name", "")
        existing_site: str | None = entity.get("website")

        # Website domain is a strong signal
        if website and existing_site:
            if domains_match(website, existing_site):
                return idx

        # Name similarity fallback
        if names_are_similar(name, existing_name):
            return idx

    return None
