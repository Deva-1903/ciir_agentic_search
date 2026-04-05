"""Heuristics for estimating provenance quality of row evidence.

Source scoring now uses the page evidence-regime layer as its primary
signal, then applies small curated-domain overrides where they clearly pay
off. This keeps the heuristics broad enough for software, companies, local
businesses, organizations, and research-ish sources without requiring
vertical-specific hardcoding.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Literal

from app.models.schema import EntityRow
from app.services.evidence_regimes import EvidenceRegime, classify_url_evidence_regime, clamp_regime_quality
from app.utils.dedupe import domains_match
from app.utils.url import extract_domain

SourceKind = Literal["official", "editorial", "directory", "marketplace", "unknown"]

_EDITORIAL_DOMAIN_BOOST = {
    "acm.org",
    "arstechnica.com",
    "arxiv.org",
    "bbc.com",
    "bloomberg.com",
    "cnet.com",
    "economist.com",
    "forbes.com",
    "ft.com",
    "ieee.org",
    "nature.com",
    "newyorker.com",
    "nytimes.com",
    "pubmed.ncbi.nlm.nih.gov",
    "reuters.com",
    "sciencemag.org",
    "springer.com",
    "techcrunch.com",
    "theatlantic.com",
    "theguardian.com",
    "theverge.com",
    "vox.com",
    "wired.com",
    "zdnet.com",
}

_DIRECTORY_DOMAIN_BOOST = {
    "booking.com",
    "capterra.com",
    "crunchbase.com",
    "g2.com",
    "glassdoor.com",
    "opentable.com",
    "producthunt.com",
    "tripadvisor.com",
    "trustpilot.com",
    "yelp.com",
}

_MARKETPLACE_DOMAIN_BOOST = {
    "airbnb.com",
    "amazon.com",
    "doordash.com",
    "ebay.com",
    "etsy.com",
    "expedia.com",
    "grubhub.com",
    "hotels.com",
    "kayak.com",
    "postmates.com",
    "ubereats.com",
}

_SOFTWARE_REGIME_DOMAINS = {
    "bitbucket.org",
    "codeberg.org",
    "docs.rs",
    "github.com",
    "gitlab.com",
    "npmjs.com",
    "pypi.org",
    "readthedocs.io",
}

_HIGH_SIGNAL_TITLE_TERMS = ("about", "contact", "docs", "documentation", "official", "overview")
_LOW_SIGNAL_TITLE_TERMS = ("category", "compare", "directory", "results")

_WEBSITE_COLS = {
    "homepage",
    "link",
    "official_website",
    "site",
    "url",
    "website",
    "website_or_profile",
    "website_or_repo",
}


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _official_website(row: EntityRow) -> str | None:
    for key in _WEBSITE_COLS:
        cell = row.cells.get(key)
        if cell and cell.value:
            return cell.value
    return None


def _regime_from_url(
    url: str,
    title: str | None = None,
    source_regime: EvidenceRegime | None = None,
) -> EvidenceRegime:
    return source_regime or classify_url_evidence_regime(url, title=title)


def classify_source(
    url: str,
    title: str | None = None,
    official_website: str | None = None,
    source_regime: EvidenceRegime | None = None,
) -> tuple[SourceKind, float]:
    """Return (kind, quality_score) for a source page."""
    domain = extract_domain(url) or ""
    title_l = (title or "").lower()

    if official_website and domains_match(url, official_website):
        return "official", 1.0

    regime = _regime_from_url(url, title, source_regime)

    if domain in _MARKETPLACE_DOMAIN_BOOST or regime == "marketplace_aggregator":
        return "marketplace", 0.15

    if regime == "software_repo_or_docs":
        quality = 0.86 if domain in _SOFTWARE_REGIME_DOMAINS else 0.8
        if "docs" in title_l or "documentation" in title_l:
            quality += 0.03
        return "unknown", _clamp(quality)

    if regime == "local_business_listing":
        quality = 0.7
        if domain in _DIRECTORY_DOMAIN_BOOST:
            quality = max(quality, 0.72)
        return "directory", _clamp(quality)

    if regime == "editorial_article":
        quality = 0.72
        if domain in _EDITORIAL_DOMAIN_BOOST:
            quality = 0.82
        return "editorial", _clamp(quality)

    if regime == "directory_listing":
        quality = 0.58
        if domain in _DIRECTORY_DOMAIN_BOOST:
            quality = 0.62
        return "directory", _clamp(quality)

    if regime == "official_site":
        quality = 0.72
        if any(term in title_l for term in _HIGH_SIGNAL_TITLE_TERMS):
            quality += 0.05
        return "unknown", _clamp(quality)

    if domain in _EDITORIAL_DOMAIN_BOOST:
        return "editorial", 0.8
    if domain in _DIRECTORY_DOMAIN_BOOST:
        return "directory", 0.6

    quality = clamp_regime_quality(regime)
    if any(term in title_l for term in _HIGH_SIGNAL_TITLE_TERMS):
        quality += 0.05
    if any(term in title_l for term in _LOW_SIGNAL_TITLE_TERMS):
        quality -= 0.08
    return "unknown", _clamp(quality)


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


def is_curated_third_party(domain: str) -> bool:
    return (
        domain in _EDITORIAL_DOMAIN_BOOST
        or domain in _DIRECTORY_DOMAIN_BOOST
        or domain in _MARKETPLACE_DOMAIN_BOOST
    )


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


def row_evidence_regime_profile(row: EntityRow) -> dict[EvidenceRegime, int]:
    """Return counts of unique evidence regimes contributing to a row."""
    seen: set[str] = set()
    counts: dict[EvidenceRegime, int] = {
        "official_site": 0,
        "directory_listing": 0,
        "editorial_article": 0,
        "local_business_listing": 0,
        "software_repo_or_docs": 0,
        "marketplace_aggregator": 0,
        "unknown": 0,
    }

    for cell in row.cells.values():
        url = cell.source_url
        if url in seen:
            continue
        seen.add(url)
        regime = classify_url_evidence_regime(url, title=cell.source_title)
        counts[regime] += 1

    return counts
