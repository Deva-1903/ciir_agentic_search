"""Evidence-regime classification for scraped pages and source URLs.

The pipeline already had source-quality heuristics, but not a reusable
page-level regime object. This module classifies pages into a small set of
regimes that downstream code can route on:

  - official_site
  - directory_listing
  - editorial_article
  - local_business_listing
  - software_repo_or_docs
  - marketplace_aggregator
  - unknown

Classification is intentionally lightweight and explainable. It uses URL
shape, domain hints, title signals, lightweight HTML metadata, and a few
content heuristics. It is not query-specific.
"""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlparse

from app.utils.url import extract_domain

EvidenceRegime = Literal[
    "official_site",
    "directory_listing",
    "editorial_article",
    "local_business_listing",
    "software_repo_or_docs",
    "marketplace_aggregator",
    "unknown",
]

_ARTICLE_SEGMENTS = {
    "analysis",
    "announcement",
    "announcements",
    "article",
    "articles",
    "blog",
    "blogs",
    "column",
    "columns",
    "coverage",
    "editorial",
    "essay",
    "feature",
    "features",
    "insights",
    "magazine",
    "news",
    "opinion",
    "post",
    "posts",
    "press",
    "report",
    "reports",
    "review",
    "reviews",
    "story",
    "stories",
}

_DIRECTORY_SEGMENTS = {
    "browse",
    "categories",
    "category",
    "collection",
    "collections",
    "companies",
    "compare",
    "comparison",
    "database",
    "databases",
    "directory",
    "directories",
    "discover",
    "explore",
    "industry",
    "industries",
    "list",
    "lists",
    "listing",
    "listings",
    "results",
    "search",
    "tag",
    "tags",
    "topic",
    "topics",
}

_DIRECTORY_QUERY_HINTS = (
    "browse=",
    "category=",
    "categories=",
    "collection=",
    "filter=",
    "industry=",
    "industries=",
    "list=",
    "q=",
    "query=",
    "search=",
    "tag=",
    "tags=",
    "topic=",
    "type=",
)

_MARKETPLACE_DOMAINS = {
    "airbnb.com",
    "amazon.com",
    "booking.com",
    "doordash.com",
    "ebay.com",
    "etsy.com",
    "expedia.com",
    "grubhub.com",
    "hotels.com",
    "instacart.com",
    "kayak.com",
    "postmates.com",
    "ubereats.com",
}

_SOFTWARE_REPO_DOMAINS = {
    "bitbucket.org",
    "codeberg.org",
    "docs.rs",
    "gitlab.com",
    "github.com",
    "npmjs.com",
    "pypi.org",
    "readthedocs.io",
}

_OFFICIAL_TITLE_HINTS = ("about", "contact", "home", "official", "overview", "team")
_DIRECTORY_TITLE_HINTS = ("category", "compare", "directory", "list of", "top ", "best ", "results")
_ARTICLE_TITLE_HINTS = ("analysis", "blog", "news", "review", "story", "why ", "how ")
_MARKETPLACE_TITLE_HINTS = ("buy", "book", "delivery", "order online", "reserve")
_SOFTWARE_TITLE_HINTS = ("api", "developer", "docs", "documentation", "github", "reference", "readme")

_LOCAL_BUSINESS_TYPES = {
    "bakery",
    "barorspub",
    "cafetorcoffeeshop",
    "foodestablishment",
    "healthandbeautybusiness",
    "hotel",
    "lodgingbusiness",
    "localbusiness",
    "organization",
    "place",
    "restaurant",
    "store",
}

_SOFTWARE_TYPES = {
    "dataset",
    "howto",
    "softwareapplication",
    "softwarepackage",
    "techarticle",
    "webapi",
    "webpage",
}

_ARTICLE_TYPES = {
    "article",
    "blogposting",
    "discussionforumposting",
    "newsarticle",
    "report",
    "reviewnewsarticle",
    "scholarlyarticle",
    "socialmediaposting",
    "techarticle",
}

_REPO_PATH_RE = re.compile(r"^/[^/]+/[^/]+(?:/|$)")
_DATED_PATH_RE = re.compile(r"/(?:19|20)\d{2}/\d{1,2}/")
_LONG_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+){2,}")
_PHONE_RE = re.compile(r"(?:\+?\d[\d\-\(\) ]{7,}\d)")
_ADDRESS_HINT_RE = re.compile(
    r"\b\d{1,5}\s+[a-z0-9][a-z0-9 .'-]{2,80}\b(?:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|way|court|ct)\b",
    re.IGNORECASE,
)
_HOURS_HINTS = ("hours", "open now", "reservations", "book a table")
_JS_SHELL_MARKERS = (
    "__NEXT_DATA__",
    "data-reactroot",
    "id=\"__next\"",
    "id=\"app\"",
    "id=\"root\"",
    "ng-version",
    "Please enable JavaScript",
    "Loading...",
    "window.__INITIAL_STATE__",
)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _segments(path: str) -> list[str]:
    return [segment for segment in path.lower().split("/") if segment]


def _metadata_types(metadata: dict[str, Any]) -> set[str]:
    types = metadata.get("json_ld_types") or []
    return {str(item).strip().lower() for item in types if item}


def _shallow_path(path: str) -> bool:
    parts = _segments(path)
    return len(parts) <= 1


def _looks_article(path: str, title_l: str) -> bool:
    parts = _segments(path)
    return (
        any(part in _ARTICLE_SEGMENTS for part in parts)
        or bool(_DATED_PATH_RE.search(path))
        or bool(parts and _LONG_SLUG_RE.search(parts[-1]))
        or any(hint in title_l for hint in _ARTICLE_TITLE_HINTS)
    )


def _looks_directory(path: str, query: str, title_l: str, metadata: dict[str, Any]) -> bool:
    parts = _segments(path)
    anchor_count = int(metadata.get("anchor_count") or 0)
    return (
        any(part in _DIRECTORY_SEGMENTS for part in parts)
        or any(hint in query.lower() for hint in _DIRECTORY_QUERY_HINTS)
        or any(hint in title_l for hint in _DIRECTORY_TITLE_HINTS)
        or anchor_count >= 30
    )


def _looks_marketplace(domain: str, path: str, title_l: str) -> bool:
    path_l = path.lower()
    return (
        domain in _MARKETPLACE_DOMAINS
        or any(hint in title_l for hint in _MARKETPLACE_TITLE_HINTS)
        or any(token in path_l for token in ("/book", "/booking", "/buy", "/cart", "/checkout", "/delivery", "/order"))
    )


def _looks_software(domain: str, path: str, title_l: str, text_l: str, metadata: dict[str, Any]) -> bool:
    types = _metadata_types(metadata)
    if domain in _SOFTWARE_REPO_DOMAINS:
        return True
    if domain.startswith("docs."):
        return True
    if any(hint in title_l for hint in _SOFTWARE_TITLE_HINTS):
        return True
    if any(token in path.lower() for token in ("/docs", "/documentation", "/reference", "/api", "/readme")):
        return True
    if _REPO_PATH_RE.match(path) and domain in {"github.com", "gitlab.com", "bitbucket.org", "codeberg.org"}:
        return True
    if types & _SOFTWARE_TYPES:
        return True
    return "install" in text_l and "package" in text_l and "version" in text_l


def _looks_local_business(title_l: str, text_l: str, metadata: dict[str, Any]) -> bool:
    types = _metadata_types(metadata)
    if types & _LOCAL_BUSINESS_TYPES:
        return True
    structured = metadata.get("structured_data") or []
    for item in structured:
        if not isinstance(item, dict):
            continue
        if item.get("telephone") or item.get("address"):
            return True
        if item.get("servesCuisine") or item.get("priceRange"):
            return True
    if _PHONE_RE.search(text_l) and _ADDRESS_HINT_RE.search(text_l):
        return True
    return any(hint in text_l for hint in _HOURS_HINTS)


def _looks_official_site(domain: str, path: str, title_l: str, metadata: dict[str, Any]) -> bool:
    if domain in _MARKETPLACE_DOMAINS or domain in _SOFTWARE_REPO_DOMAINS:
        return False
    if not _shallow_path(path):
        return False
    if any(hint in title_l for hint in _DIRECTORY_TITLE_HINTS):
        return False
    if any(hint in title_l for hint in _OFFICIAL_TITLE_HINTS):
        return True
    headings = " ".join(metadata.get("headings") or []).lower()
    if any(hint in headings for hint in _OFFICIAL_TITLE_HINTS):
        return True
    return True


def classify_page_evidence(
    url: str,
    title: str | None = None,
    cleaned_text: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> tuple[EvidenceRegime, float]:
    """Classify a scraped page into an evidence regime."""
    metadata = metadata or {}
    parsed = urlparse(url)
    domain = extract_domain(url) or ""
    path = parsed.path or "/"
    title_l = (title or "").lower()
    text_l = (cleaned_text or "").lower()
    types = _metadata_types(metadata)

    if _looks_marketplace(domain, path, title_l):
        return "marketplace_aggregator", 0.95 if domain in _MARKETPLACE_DOMAINS else 0.78

    if _looks_software(domain, path, title_l, text_l, metadata):
        return "software_repo_or_docs", 0.94 if domain in _SOFTWARE_REPO_DOMAINS else 0.82

    if types & _ARTICLE_TYPES or _looks_article(path, title_l):
        return "editorial_article", 0.82

    if _looks_directory(path, parsed.query, title_l, metadata):
        return "directory_listing", 0.78

    if _looks_local_business(title_l, text_l, metadata):
        return "local_business_listing", 0.8

    if _looks_official_site(domain, path, title_l, metadata):
        return "official_site", 0.72

    return "unknown", 0.5


def classify_url_evidence_regime(
    url: str,
    title: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> EvidenceRegime:
    """Lightweight URL/title-based regime classification for source scoring."""
    regime, _ = classify_page_evidence(url, title=title, metadata=metadata)
    return regime


def page_likely_needs_js(
    html: str,
    cleaned_text: str,
    metadata: dict[str, Any] | None = None,
    min_text_length: int = 200,
) -> bool:
    """Heuristic detector for app-shell or JS-heavy pages.

    It intentionally biases toward false negatives. We only want to spend
    browser-rendering budget when static extraction looks especially weak.
    """
    metadata = metadata or {}
    text_length = len(cleaned_text or "")
    script_count = int(metadata.get("script_count") or 0)
    marker_hits = sum(1 for marker in _JS_SHELL_MARKERS if marker in html)

    if text_length >= min_text_length:
        return False

    if marker_hits >= 1 and script_count >= 5:
        return True
    if script_count >= 12 and text_length < min_text_length // 2:
        return True
    if "application/ld+json" in html and text_length < min_text_length // 3:
        return True
    return False


def regime_quality(regime: EvidenceRegime) -> float:
    """Default confidence-like quality per regime, before official-domain boost."""
    if regime == "official_site":
        return 0.72
    if regime == "software_repo_or_docs":
        return 0.8
    if regime == "local_business_listing":
        return 0.7
    if regime == "editorial_article":
        return 0.72
    if regime == "directory_listing":
        return 0.58
    if regime == "marketplace_aggregator":
        return 0.15
    return 0.55


def clamp_regime_quality(regime: EvidenceRegime, adjustment: float = 0.0) -> float:
    return _clamp(regime_quality(regime) + adjustment)
