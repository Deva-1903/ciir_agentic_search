"""
Brave Search API integration.

Runs multiple search angles in parallel, collects results, deduplicates URLs.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.schema import BraveResult
from app.utils.url import dedupe_urls, is_useful_url, normalize_url

log = get_logger(__name__)

_BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"


async def _search_one_angle(
    client: httpx.AsyncClient,
    angle: str,
    top_k: int,
    api_key: str,
) -> list[BraveResult]:
    """Execute a single Brave search query. Returns up to top_k results."""
    try:
        response = await client.get(
            _BRAVE_API_URL,
            params={"q": angle, "count": top_k, "search_lang": "en"},
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
            },
            timeout=15.0,
        )
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as exc:
        log.warning("Brave search HTTP error for %r: %s", angle, exc.response.status_code)
        return []
    except Exception as exc:
        log.warning("Brave search failed for %r: %s", angle, exc)
        return []

    results: list[BraveResult] = []
    for item in data.get("web", {}).get("results", []):
        url: Optional[str] = item.get("url")
        if not url or not is_useful_url(url):
            continue
        results.append(
            BraveResult(
                url=url,
                title=item.get("title") or "",
                snippet=item.get("description") or item.get("extra_snippets", [None])[0],
            )
        )

    log.debug("Angle %r → %d results", angle, len(results))
    return results


async def run_brave_search(
    angles: list[str],
    top_k: Optional[int] = None,
) -> list[BraveResult]:
    """
    Run all search angles in parallel.
    Returns a deduplicated list of BraveResult, ordered by first appearance.
    """
    settings = get_settings()
    if not settings.brave_api_key:
        raise RuntimeError("BRAVE_API_KEY is not set")

    k = top_k or settings.max_results_per_angle

    async with httpx.AsyncClient() as client:
        tasks = [_search_one_angle(client, angle, k, settings.brave_api_key) for angle in angles]
        all_results_nested = await asyncio.gather(*tasks)

    # Flatten and deduplicate
    seen_norm: set[str] = set()
    deduped: list[BraveResult] = []
    for batch in all_results_nested:
        for result in batch:
            norm = normalize_url(result.url)
            if norm not in seen_norm:
                seen_norm.add(norm)
                deduped.append(result)

    log.info(
        "Brave search: %d angles → %d unique URLs",
        len(angles),
        len(deduped),
    )
    return deduped
