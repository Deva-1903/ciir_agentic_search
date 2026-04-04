"""
Async web scraper with SQLite caching.

For each URL:
  1. Check SQLite cache (reuse if fresh)
  2. Fetch with httpx
  3. Extract clean text via trafilatura; fall back to BeautifulSoup
  4. Store in cache
  5. Return ScrapedPage

Respects a semaphore for polite concurrency.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import httpx
import trafilatura
from bs4 import BeautifulSoup

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.db import get_cached_page, save_cached_page
from app.models.schema import BraveResult, ScrapedPage
from app.utils.text import clean_text

log = get_logger(__name__)

_MIN_TEXT_LENGTH = 200  # pages shorter than this are skipped

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; AgenticSearch/1.0; "
        "+https://github.com/agentic-search)"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# ── Extraction helpers ────────────────────────────────────────────────────────

def _extract_with_trafilatura(html: str) -> tuple[str, str]:
    """Returns (title, text) or ("", "") on failure."""
    try:
        text = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=True,
            no_fallback=False,
        )
        meta = trafilatura.extract_metadata(html)
        title = meta.title if meta and meta.title else ""
        return title or "", text or ""
    except Exception:
        return "", ""


def _extract_with_bs4(html: str, url: str) -> tuple[str, str]:
    """Fallback: extract visible text with BeautifulSoup."""
    try:
        soup = BeautifulSoup(html, "lxml")
        # Title
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else ""

        # Remove boilerplate tags
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        text = soup.get_text(separator=" ", strip=True)
        return title, text
    except Exception:
        return "", ""


def _extract_page(html: str, url: str) -> tuple[str, str]:
    """Try trafilatura, fall back to BeautifulSoup."""
    title, text = _extract_with_trafilatura(html)
    if not text or len(text) < _MIN_TEXT_LENGTH:
        log.debug("trafilatura insufficient for %s, trying BS4", url)
        bs_title, bs_text = _extract_with_bs4(html, url)
        if len(bs_text) > len(text):
            text = bs_text
            if not title:
                title = bs_title
    return title, clean_text(text)


# ── Per-URL fetch ─────────────────────────────────────────────────────────────

async def _fetch_and_parse(
    client: httpx.AsyncClient,
    url: str,
    title_hint: str,
    timeout: int,
) -> Optional[ScrapedPage]:
    """Fetch a single URL and return a ScrapedPage, or None on failure."""
    # Check cache first
    cached = await get_cached_page(url)
    if cached:
        log.debug("Cache hit: %s", url)
        return ScrapedPage(
            url=url,
            title=cached.get("title") or title_hint,
            cleaned_text=cached["cleaned_text"],
            from_cache=True,
        )

    try:
        response = await client.get(url, headers=_HEADERS, timeout=float(timeout))
        response.raise_for_status()
        html = response.text
    except httpx.TooManyRedirects:
        log.debug("Too many redirects: %s", url)
        return None
    except httpx.HTTPStatusError as exc:
        log.debug("HTTP %s for %s", exc.response.status_code, url)
        return None
    except Exception as exc:
        log.debug("Fetch error for %s: %s", url, exc)
        return None

    title, text = _extract_page(html, url)
    if not title:
        title = title_hint

    if len(text) < _MIN_TEXT_LENGTH:
        log.debug("Too little text extracted from %s (%d chars)", url, len(text))
        return None

    await save_cached_page(url, title, text)

    return ScrapedPage(url=url, title=title, cleaned_text=text)


# ── Batch scrape ──────────────────────────────────────────────────────────────

async def scrape_pages(results: list[BraveResult]) -> list[ScrapedPage]:
    """
    Scrape a list of BraveResults concurrently, respecting a semaphore.
    Returns only successfully scraped pages.
    """
    settings = get_settings()
    sem = asyncio.Semaphore(settings.max_concurrent_scrapes)

    async def _bounded(result: BraveResult) -> Optional[ScrapedPage]:
        async with sem:
            return await _fetch_and_parse(
                client,
                result.url,
                result.title,
                settings.scrape_timeout,
            )

    async with httpx.AsyncClient(follow_redirects=True, max_redirects=5) as client:
        tasks = [_bounded(r) for r in results]
        raw = await asyncio.gather(*tasks, return_exceptions=False)

    pages = [p for p in raw if p is not None]
    log.info("Scraped %d/%d pages successfully", len(pages), len(results))
    return pages


async def scrape_urls(urls: list[str]) -> list[ScrapedPage]:
    """Scrape a plain list of URLs (no BraveResult metadata needed)."""
    results = [BraveResult(url=u, title="") for u in urls]
    return await scrape_pages(results)
