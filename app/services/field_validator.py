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

from app.services.source_quality import classify_source
from app.utils.url import extract_domain

# Column name sets
_WEBSITE_COLS = {"website", "url", "official_website", "homepage", "link"}
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
_HOMEPAGE_HINT_SEGMENTS = {
    "about",
    "company",
    "contact",
    "contacts",
    "home",
    "hours",
    "location",
    "locations",
    "menu",
    "team",
    "visit",
}
_ARTICLE_TITLE_HINTS = ("blog", "news", "report", "review", "guide", "top ", "best ")
_DIRECTORY_TITLE_HINTS = ("category", "directory", "industry", "companies", "list", "find top startups")


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


def _looks_like_directory_page(parsed, title: str | None = None) -> bool:
    segments = _path_segments(parsed)
    title_l = (title or "").lower()
    if any(seg in _DIRECTORY_SEGMENTS for seg in segments):
        return True
    return any(hint in title_l for hint in _DIRECTORY_TITLE_HINTS)


def _should_canonicalize_to_homepage(parsed) -> bool:
    segments = _path_segments(parsed)
    if not segments:
        return False
    return any(seg in _HOMEPAGE_HINT_SEGMENTS for seg in segments)


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

        official_url = _canonical_url_for_domain(canonical_domain)
        candidate_kind, _ = classify_source(normalized, None, official_url)
        if candidate_kind in {"editorial", "directory", "marketplace"}:
            return value, False

        source_domain = extract_domain(source_url) if source_url else None
        if source_url:
            source_kind, _ = classify_source(source_url, source_title, official_url)
            source_parsed = urlparse(source_url)
            same_domain = source_domain == candidate_domain
            if same_domain and source_kind in {"editorial", "directory", "marketplace"}:
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
