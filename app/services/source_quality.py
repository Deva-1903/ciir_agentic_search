"""Heuristics for estimating provenance quality of row evidence."""

from __future__ import annotations

from collections import defaultdict
from typing import Literal
from urllib.parse import urlparse

from app.models.schema import EntityRow
from app.utils.dedupe import domains_match
from app.utils.url import extract_domain

SourceKind = Literal["official", "editorial", "directory", "marketplace", "unknown"]

_EDITORIAL_DOMAINS = {
    "eater.com",
    "foodandwine.com",
    "grubstreet.com",
    "michelin.com",
    "newyorker.com",
    "nytimes.com",
    "seriouseats.com",
    "tastingtable.com",
    "theinfatuation.com",
    "thrillist.com",
    "timeout.com",
    "vogue.com",
}

_DIRECTORY_DOMAINS = {
    "opentable.com",
    "tripadvisor.com",
    "yelp.com",
}

_MARKETPLACE_DOMAINS = {
    "doordash.com",
    "grubhub.com",
    "postmates.com",
    "seamless.com",
    "ubereats.com",
}

_HIGH_SIGNAL_TERMS = (
    "best",
    "guide",
    "review",
    "reviews",
    "top",
)

_LOW_SIGNAL_TERMS = (
    "category",
    "delivery",
    "near me",
    "order",
)

_WEBSITE_COLS = {"website", "url", "official_website", "homepage", "link"}


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _official_website(row: EntityRow) -> str | None:
    for key in _WEBSITE_COLS:
        cell = row.cells.get(key)
        if cell and cell.value:
            return cell.value
    return None


def classify_source(
    url: str,
    title: str | None = None,
    official_website: str | None = None,
) -> tuple[SourceKind, float]:
    """Return (kind, quality_score) for a source page."""
    domain = extract_domain(url)
    if official_website and domains_match(url, official_website):
        return "official", 1.0

    title_l = (title or "").lower()
    path_l = urlparse(url).path.lower()

    if domain in _MARKETPLACE_DOMAINS:
        score = 0.2
        kind: SourceKind = "marketplace"
    elif domain in _EDITORIAL_DOMAINS:
        score = 0.85
        kind = "editorial"
    elif domain in _DIRECTORY_DOMAINS:
        score = 0.65
        kind = "directory"
    else:
        score = 0.55
        kind = "unknown"

    if "official" in title_l or any(token in path_l for token in ("/contact", "/about", "/locations", "/menu")):
        score += 0.05

    if any(term in title_l or term in path_l for term in _HIGH_SIGNAL_TERMS):
        score += 0.05

    if any(term in title_l or term in path_l for term in _LOW_SIGNAL_TERMS):
        score -= 0.2

    return kind, _clamp(score)


def row_source_quality(row: EntityRow) -> float:
    """Return a confidence-like score for the overall evidence quality of a row."""
    official_website = _official_website(row)
    per_source_weight: dict[tuple[str, str | None], float] = defaultdict(float)
    per_source_score: dict[tuple[str, str | None], float] = {}

    for cell in row.cells.values():
        key = (cell.source_url, cell.source_title)
        _, quality = classify_source(cell.source_url, cell.source_title, official_website)
        per_source_score[key] = quality
        per_source_weight[key] += max(cell.confidence, 0.1)

    if not per_source_score:
        return 0.0

    total_weight = sum(per_source_weight.values())
    weighted_sum = sum(
        per_source_score[key] * per_source_weight[key]
        for key in per_source_score
    )
    return round(weighted_sum / total_weight, 3) if total_weight else 0.0


def row_source_profile(row: EntityRow) -> dict[SourceKind, int]:
    """Return counts of unique source kinds contributing to a row."""
    official_website = _official_website(row)
    seen: set[str] = set()
    counts: dict[SourceKind, int] = {
        "official": 0,
        "editorial": 0,
        "directory": 0,
        "marketplace": 0,
        "unknown": 0,
    }

    for cell in row.cells.values():
        url = cell.source_url
        if url in seen:
            continue
        seen.add(url)
        kind, _ = classify_source(url, cell.source_title, official_website)
        counts[kind] += 1

    return counts
