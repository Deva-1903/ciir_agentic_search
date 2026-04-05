"""
Async web scraper with SQLite caching and lightweight page metadata.

For each URL:
  1. Check SQLite cache (reuse if fresh)
  2. Fetch with httpx
  3. Extract clean text plus lightweight HTML metadata
  4. Detect an evidence regime for downstream routing
  5. Optionally try a JS-render fallback when the static page looks app-shell-ish
  6. Store in cache
  7. Return ScrapedPage

Respects a semaphore for polite concurrency.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, Optional

import httpx
import trafilatura
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.db import get_cached_page, save_cached_page
from app.models.schema import BraveResult, ScrapedPage
from app.services.evidence_regimes import classify_page_evidence, page_likely_needs_js
from app.utils.text import clean_text
from app.utils.url import is_useful_url

log = get_logger(__name__)

_MIN_TEXT_LENGTH = 200
_MAX_HEADING_COUNT = 8
_MAX_STRUCTURED_ITEMS = 6

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; AgenticSearch/1.0; "
        "+https://github.com/agentic-search)"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _bump_stat(stats: dict[str, int] | None, key: str, amount: int = 1) -> None:
    if stats is None:
        return
    stats[key] = stats.get(key, 0) + amount


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


def _extract_with_bs4(html: str) -> tuple[str, str]:
    """Fallback: extract visible text with BeautifulSoup."""
    try:
        soup = BeautifulSoup(html, "lxml")
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else ""

        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        text = soup.get_text(separator=" ", strip=True)
        return title, text
    except Exception:
        return "", ""


def _extract_page_text(html: str, url: str) -> tuple[str, str]:
    """Try trafilatura, fall back to BeautifulSoup."""
    title, text = _extract_with_trafilatura(html)
    if not text or len(text) < _MIN_TEXT_LENGTH:
        log.debug("trafilatura insufficient for %s, trying BS4", url)
        bs_title, bs_text = _extract_with_bs4(html)
        if len(bs_text) > len(text):
            text = bs_text
            if not title:
                title = bs_title
    return title, clean_text(text)


def _iter_ld_items(payload: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    def _walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                _walk(item)
            return
        if not isinstance(node, dict):
            return
        items.append(node)
        for key in ("@graph", "mainEntity", "item", "itemListElement", "about"):
            if key in node:
                _walk(node[key])

    _walk(payload)
    return items


def _flatten_address(value: Any) -> str:
    if isinstance(value, str):
        return clean_text(value)
    if not isinstance(value, dict):
        return ""
    parts = [
        value.get("streetAddress"),
        value.get("addressLocality"),
        value.get("addressRegion"),
        value.get("postalCode"),
        value.get("addressCountry"),
    ]
    return clean_text(", ".join(str(part) for part in parts if part))


def _value_to_text(value: Any) -> str:
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        return clean_text(", ".join(str(item) for item in value if item))
    return clean_text(str(value)) if value not in (None, "") else ""


def _flatten_ld_item(item: dict[str, Any]) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    type_value = item.get("@type")
    if isinstance(type_value, list):
        flat["@type"] = [str(entry) for entry in type_value if entry]
    elif type_value:
        flat["@type"] = [str(type_value)]
    else:
        flat["@type"] = []

    for key in ("name", "url", "telephone", "description", "priceRange", "servesCuisine"):
        text = _value_to_text(item.get(key))
        if text:
            flat[key] = text

    address = _flatten_address(item.get("address"))
    if address:
        flat["address"] = address

    offers = item.get("offers")
    offers_text = _value_to_text(offers.get("price") if isinstance(offers, dict) else offers)
    if offers_text:
        flat["offers"] = offers_text

    return flat


def _extract_html_metadata(html: str, url: str) -> dict[str, Any]:
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return {}

    title_tag = soup.find("title")
    meta_description = ""
    desc_tag = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )
    if desc_tag:
        meta_description = clean_text(desc_tag.get("content", ""))

    headings: list[str] = []
    for tag_name in ("h1", "h2", "h3"):
        for tag in soup.find_all(tag_name):
            text = clean_text(tag.get_text(" ", strip=True))
            if text and text not in headings:
                headings.append(text)
            if len(headings) >= _MAX_HEADING_COUNT:
                break
        if len(headings) >= _MAX_HEADING_COUNT:
            break

    tel_links: list[str] = []
    mailto_links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        if href.startswith("tel:"):
            tel_links.append(href[4:])
        elif href.startswith("mailto:"):
            mailto_links.append(href[7:])

    json_ld_types: list[str] = []
    structured_data: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        for item in _iter_ld_items(payload):
            flattened = _flatten_ld_item(item)
            item_types = flattened.get("@type") or []
            json_ld_types.extend(str(item_type) for item_type in item_types if item_type)
            if flattened and len(structured_data) < _MAX_STRUCTURED_ITEMS:
                structured_data.append(flattened)

    useful_link_count = 0
    for anchor in soup.find_all("a", href=True):
        href = urljoin(url, anchor.get("href", ""))
        if is_useful_url(href):
            useful_link_count += 1

    return {
        "anchor_count": useful_link_count,
        "headings": headings,
        "json_ld_types": sorted(set(json_ld_types)),
        "meta_description": meta_description,
        "mailto_links": mailto_links[:5],
        "script_count": len(soup.find_all("script")),
        "structured_data": structured_data,
        "tel_links": tel_links[:5],
        "title": clean_text(title_tag.get_text(" ", strip=True)) if title_tag else "",
    }


def _build_scraped_page(
    url: str,
    title_hint: str,
    html: str | None,
    fetch_method: str,
) -> ScrapedPage | None:
    if html is None:
        return None

    title, text = _extract_page_text(html, url)
    metadata = _extract_html_metadata(html, url)
    title = title or metadata.get("title") or title_hint
    regime, confidence = classify_page_evidence(url, title=title, cleaned_text=text, metadata=metadata)

    return ScrapedPage(
        url=url,
        title=title or title_hint,
        cleaned_text=text,
        raw_html=html,
        page_metadata=metadata,
        evidence_regime=regime,
        regime_confidence=confidence,
        fetch_method=fetch_method,
    )


async def _fetch_with_js(url: str, timeout: int) -> str | None:
    """Selective browser-render fallback. Requires Playwright to be installed."""
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except Exception as exc:
        log.debug("Playwright unavailable for JS fallback on %s: %s", url, exc)
        return None

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(user_agent=_HEADERS["User-Agent"])
            try:
                await page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
                html = await page.content()
            finally:
                await browser.close()
        return html
    except Exception as exc:
        log.debug("JS fallback failed for %s: %s", url, exc)
        return None


# ── Per-URL fetch ─────────────────────────────────────────────────────────────

async def _fetch_and_parse(
    client: httpx.AsyncClient,
    url: str,
    title_hint: str,
    timeout: int,
    *,
    stats: dict[str, int] | None = None,
    reserve_js_budget: Callable[[], Awaitable[bool]] | None = None,
) -> Optional[ScrapedPage]:
    """Fetch a single URL and return a ScrapedPage, or None on failure."""
    cached = await get_cached_page(url)
    if cached:
        _bump_stat(stats, "pages_from_cache")
        page_metadata = cached.get("page_metadata") or {}
        title = cached.get("title") or title_hint
        cleaned_text = cached.get("cleaned_text") or ""
        regime, confidence = classify_page_evidence(
            url,
            title=title,
            cleaned_text=cleaned_text,
            metadata=page_metadata,
        )
        return ScrapedPage(
            url=url,
            title=title,
            cleaned_text=cleaned_text,
            from_cache=True,
            raw_html=cached.get("raw_html"),
            page_metadata=page_metadata,
            evidence_regime=regime,
            regime_confidence=confidence,
            fetch_method=cached.get("fetch_method") or "static",
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

    page = _build_scraped_page(url, title_hint, html, "static")
    if page is None:
        return None

    settings = get_settings()
    if (
        getattr(settings, "js_rendering_enabled", False)
        and reserve_js_budget is not None
        and page_likely_needs_js(
            html,
            page.cleaned_text,
            page.page_metadata,
            min_text_length=_MIN_TEXT_LENGTH,
        )
    ):
        if await reserve_js_budget():
            _bump_stat(stats, "js_render_attempts")
            rendered_html = await _fetch_with_js(url, getattr(settings, "js_render_timeout", timeout))
            if rendered_html:
                rendered = _build_scraped_page(url, title_hint, rendered_html, "js")
                if rendered and len(rendered.cleaned_text) >= len(page.cleaned_text):
                    page = rendered
                    _bump_stat(stats, "js_render_successes")
                else:
                    _bump_stat(stats, "js_render_unchanged")
            else:
                _bump_stat(stats, "js_render_failures")
        else:
            _bump_stat(stats, "js_render_budget_skips")

    if len(page.cleaned_text) < _MIN_TEXT_LENGTH:
        log.debug("Too little text extracted from %s (%d chars)", url, len(page.cleaned_text))
        return None

    await save_cached_page(
        url,
        page.title,
        page.cleaned_text,
        raw_html=page.raw_html,
        page_metadata=page.page_metadata,
        fetch_method=page.fetch_method,
    )

    return page


# ── Batch scrape ──────────────────────────────────────────────────────────────

async def scrape_pages(
    results: list[BraveResult],
    stats: dict[str, int] | None = None,
) -> list[ScrapedPage]:
    """
    Scrape a list of BraveResults concurrently, respecting a semaphore.
    Returns only successfully scraped pages.
    """
    settings = get_settings()
    sem = asyncio.Semaphore(settings.max_concurrent_scrapes)
    js_lock = asyncio.Lock()
    js_budget = {"used": 0}

    async def _reserve_js_budget() -> bool:
        max_pages = max(0, int(getattr(settings, "js_render_max_pages", 0)))
        if max_pages <= 0:
            return False
        async with js_lock:
            if js_budget["used"] >= max_pages:
                return False
            js_budget["used"] += 1
            return True

    async def _bounded(result: BraveResult) -> Optional[ScrapedPage]:
        async with sem:
            return await _fetch_and_parse(
                client,
                result.url,
                result.title,
                settings.scrape_timeout,
                stats=stats,
                reserve_js_budget=_reserve_js_budget,
            )

    async with httpx.AsyncClient(follow_redirects=True, max_redirects=5) as client:
        tasks = [_bounded(result) for result in results]
        raw = await asyncio.gather(*tasks, return_exceptions=False)

    pages = [page for page in raw if page is not None]
    for page in pages:
        _bump_stat(stats, f"regime_{page.evidence_regime}_pages")
        if page.fetch_method == "js":
            _bump_stat(stats, "pages_rendered_with_js")
    log.info("Scraped %d/%d pages successfully", len(pages), len(results))
    return pages


async def scrape_urls(urls: list[str], stats: dict[str, int] | None = None) -> list[ScrapedPage]:
    """Scrape a plain list of URLs (no BraveResult metadata needed)."""
    results = [BraveResult(url=url, title="") for url in urls]
    return await scrape_pages(results, stats=stats)
