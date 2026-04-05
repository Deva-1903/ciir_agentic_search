"""
Lightweight field validation + normalization for extracted cell values.

Applied at extraction time (earliest boundary where raw LLM output becomes a
typed cell). Keeps the system's promise of provenance by making sure malformed
values (e.g. `robertaspizza` as a website) never enter the pipeline.

Design principles:
- Rule-based and deterministic. No LLM calls here.
- Only reject clearly malformed values. Preserve everything else as-is.
- Normalize when possible (e.g. add `https://` to bare-domain URLs).
- Single `validate_and_normalize(col, value)` entry point → (value, ok).
"""

from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse

from app.services.source_quality import classify_source, is_curated_third_party
from app.utils.url import extract_domain

# Placeholder / filler values an LLM may emit when it has no grounded evidence.
# These are meaningless and should be dropped from any column.
_PLACEHOLDER_VALUES = {
    "n/a", "na", "not applicable", "not available", "not specified",
    "not found", "not provided", "not stated", "not listed",
    "unknown", "unspecified", "none", "null", "tbd", "tba",
    "pending", "coming soon", "to be determined", "to be announced",
    "—", "–", "-", "...", "…", "?", "??",
}

# Column name sets — driven by field semantics, not domain.
# Structural columns that typically hold an entity's canonical URL.
_WEBSITE_COLS = {
    "website", "url", "official_website", "homepage", "link",
    "website_or_repo", "website_or_profile", "site",
}
_PHONE_COLS = {"phone", "phone_number", "telephone", "contact_phone"}
_RATING_COLS = {"rating", "score", "stars"}

# Regexes
_BARE_DOMAIN_RE = re.compile(
    r"^(?!https?://)([a-z0-9][a-z0-9\-]{0,62}\.)+[a-z]{2,}(/.*)?$",
    re.IGNORECASE,
)
_DIGITS_RE = re.compile(r"\d")
_PHONE_CLEAN_RE = re.compile(r"[^\d+x]")
_RATING_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")
_DATE_PATH_RE = re.compile(r"/20\d{2}/\d{1,2}/\d{1,2}/")

_ARTICLE_SEGMENTS = {
    "article",
    "articles",
    "blog",
    "blogs",
    "insights",
    "magazine",
    "news",
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
    "categories",
    "category",
    "collections",
    "companies",
    "company-list",
    "directory",
    "directories",
    "industry",
    "industries",
    "list",
    "lists",
    "marketplace",
}
# Retained only for structural hint detection — path-segment shape is the
# primary signal (see _should_canonicalize_to_homepage).
_HOMEPAGE_HINT_SEGMENTS: set[str] = set()
_ARTICLE_TITLE_HINTS = ("blog", "news", "report", "review", "guide", "top ", "best ")
_DIRECTORY_TITLE_HINTS = ("category", "directory", "industry", "list")


def _homepage_url(parsed) -> str:
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), "", "", "", ""))


def _path_segments(parsed) -> list[str]:
    return [seg for seg in parsed.path.lower().split("/") if seg]


def _looks_like_article_page(parsed, title: str | None = None) -> bool:
    segments = _path_segments(parsed)
    title_l = (title or "").lower()
    if any(seg in _ARTICLE_SEGMENTS for seg in segments):
        return True
    if _DATE_PATH_RE.search(parsed.path.lower()):
        return True
    if len(segments) >= 3 and any("-" in seg and len(seg) >= 20 for seg in segments):
        return True
    return any(hint in title_l for hint in _ARTICLE_TITLE_HINTS)


_DIRECTORY_QUERY_PARAMS = (
    "category=", "categories=", "industry=", "industries=",
    "tag=", "tags=", "type=", "collection=",
)


def _looks_like_directory_page(parsed, title: str | None = None) -> bool:
    segments = _path_segments(parsed)
    title_l = (title or "").lower()
    query_l = parsed.query.lower()
    if any(seg in _DIRECTORY_SEGMENTS for seg in segments):
        return True
    if any(param in query_l for param in _DIRECTORY_QUERY_PARAMS):
        return True
    return any(hint in title_l for hint in _DIRECTORY_TITLE_HINTS)


def _should_canonicalize_to_homepage(parsed) -> bool:
    """Structural heuristic: a short single path segment (no dashes, not dated)
    is almost always a "hub" page on the entity's own site (about, contact,
    menu, docs, home, team, overview, …). Collapsing to the homepage gives
    the most stable canonical representation of the entity.

    This rule works across verticals because it relies on URL shape, not a
    vertical-specific wordlist.
    """
    segments = _path_segments(parsed)
    if not segments or len(segments) > 1:
        return False
    seg = segments[0]
    # Short (<=20 chars), no dashes, no digits — this rules out dated
    # slugs and article-style permalinks.
    return "-" not in seg and not any(ch.isdigit() for ch in seg) and 2 <= len(seg) <= 20


def _canonical_url_for_domain(canonical_domain: str | None) -> str | None:
    if not canonical_domain:
        return None
    return f"https://{canonical_domain}/"


# ── URL / website ─────────────────────────────────────────────────────────────

def _is_url_like(value: str) -> bool:
    """Is this string plausibly a URL or bare domain?"""
    if not value:
        return False
    if value.lower().startswith(("http://", "https://")):
        try:
            parsed = urlparse(value)
            return bool(parsed.netloc) and "." in parsed.netloc
        except Exception:
            return False
    return bool(_BARE_DOMAIN_RE.match(value))


def normalize_website(
    value: str,
    *,
    source_url: str | None = None,
    source_title: str | None = None,
    canonical_domain: str | None = None,
) -> tuple[str, bool]:
    """
    Normalize a website value. Returns (normalized, ok).

    Rejects: empty, bare words with no dot, fragments like `robertaspizza`.
    Accepts: `https://x.com`, `x.com`, `www.x.com/about`.
    Normalizes: adds `https://` if missing; strips trailing slash and fragment.
    Semantics: prefers canonical homepages over article/listing URLs. Editorial,
    directory, and marketplace URLs are rejected as final website values.
    """
    if not value:
        return value, False
    v = value.strip()
    if not _is_url_like(v):
        return value, False
    if not v.lower().startswith(("http://", "https://")):
        v = "https://" + v
    try:
        parsed = urlparse(v)
        if not parsed.netloc or "." not in parsed.netloc:
            return value, False
        normalized = urlunparse((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            parsed.params,
            parsed.query,
            "",  # drop fragment
        ))
        normalized_parsed = urlparse(normalized)
        candidate_domain = extract_domain(normalized)
        if not candidate_domain:
            return value, False

        # Hard-reject only when the candidate URL is on a curated third-party
        # domain. Shape-based editorial/directory signals are handled below
        # by collapsing to homepage (an entity's own blog post should
        # canonicalize to its homepage, not be rejected entirely).
        if is_curated_third_party(candidate_domain):
            return value, False

        source_domain = extract_domain(source_url) if source_url else None
        if source_url:
            source_parsed = urlparse(source_url)
            same_domain = source_domain == candidate_domain
            if same_domain and is_curated_third_party(source_domain or ""):
                return value, False
            if same_domain and _looks_like_directory_page(source_parsed, source_title):
                return value, False

        if canonical_domain and candidate_domain == canonical_domain:
            return _homepage_url(normalized_parsed), True

        if _looks_like_article_page(normalized_parsed, source_title) or _should_canonicalize_to_homepage(normalized_parsed):
            return _homepage_url(normalized_parsed), True

        return normalized, True
    except Exception:
        return value, False


# ── Phone ─────────────────────────────────────────────────────────────────────

def validate_phone(value: str) -> tuple[str, bool]:
    """Accept anything with at least 7 digits; strip obvious junk chars."""
    if not value:
        return value, False
    cleaned = _PHONE_CLEAN_RE.sub("", value)
    digit_count = sum(1 for c in cleaned if c.isdigit())
    return value.strip(), digit_count >= 7


# ── Rating ────────────────────────────────────────────────────────────────────

def validate_rating(value: str) -> tuple[str, bool]:
    """Accept ratings that contain a number between 0 and 10 inclusive."""
    if not value:
        return value, False
    match = _RATING_NUM_RE.search(value)
    if not match:
        return value, False
    try:
        num = float(match.group(1))
    except ValueError:
        return value, False
    return value.strip(), 0.0 <= num <= 10.0


# ── Entry point ───────────────────────────────────────────────────────────────

def validate_and_normalize(
    col: str,
    value: str,
    *,
    source_url: str | None = None,
    source_title: str | None = None,
    canonical_domain: str | None = None,
) -> tuple[str, bool]:
    """
    Validate and (where relevant) normalize a cell value for column `col`.
    Returns (possibly_normalized_value, ok). If ok is False the caller should
    drop the cell.

    Non-validated columns always return (value, True) — we only intervene for
    fields with strong structural expectations.
    """
    if not isinstance(value, str):
        return str(value), False
    stripped = value.strip()
    if not stripped:
        return value, False

    # Reject placeholder/filler values regardless of column type.
    if stripped.lower() in _PLACEHOLDER_VALUES:
        return value, False

    col_l = col.lower()
    if col_l in _WEBSITE_COLS:
        return normalize_website(
            stripped,
            source_url=source_url,
            source_title=source_title,
            canonical_domain=canonical_domain,
        )
    if col_l in _PHONE_COLS:
        return validate_phone(stripped)
    if col_l in _RATING_COLS:
        return validate_rating(stripped)
    return stripped, True
