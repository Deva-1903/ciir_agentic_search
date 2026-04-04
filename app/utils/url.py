"""URL normalization and filtering utilities."""

from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse


# Patterns that are almost never useful to scrape
_JUNK_EXTENSIONS = re.compile(
    r"\.(pdf|docx?|xlsx?|pptx?|zip|gz|tar|mp4|mp3|avi|mov|jpg|jpeg|png|gif|svg|ico|woff2?)$",
    re.IGNORECASE,
)

_JUNK_DOMAINS = re.compile(
    r"(youtube\.com|youtu\.be|twitter\.com|x\.com|facebook\.com|instagram\.com"
    r"|tiktok\.com|reddit\.com|pinterest\.com|linkedin\.com/in/"
    r"|play\.google\.com|apps\.apple\.com|webcache\.googleusercontent\.com)",
    re.IGNORECASE,
)


def normalize_url(url: str) -> str:
    """Lowercase scheme+host, strip fragment and trailing slash."""
    try:
        p = urlparse(url.strip())
        normalized = urlunparse((
            p.scheme.lower(),
            p.netloc.lower(),
            p.path.rstrip("/"),
            p.params,
            p.query,
            "",  # drop fragment
        ))
        return normalized
    except Exception:
        return url.strip()


def is_useful_url(url: str) -> bool:
    """Return False for URLs we should skip."""
    if not url.startswith(("http://", "https://")):
        return False
    if _JUNK_EXTENSIONS.search(url):
        return False
    if _JUNK_DOMAINS.search(url):
        return False
    return True


def extract_domain(url: str) -> str:
    """Return just the registered domain (e.g. 'example.com')."""
    try:
        host = urlparse(url).netloc.lower()
        host = re.sub(r"^www\.", "", host)
        return host
    except Exception:
        return ""


def dedupe_urls(urls: list[str]) -> list[str]:
    """Normalize and deduplicate a list of URLs, preserving order."""
    seen: set[str] = set()
    result: list[str] = []
    for url in urls:
        norm = normalize_url(url)
        if norm not in seen and is_useful_url(norm):
            seen.add(norm)
            result.append(url)  # keep original for fetching, deduped by norm
    return result
