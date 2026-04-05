"""Heuristic official-site resolution for discovered candidates."""

from __future__ import annotations

from urllib.parse import urlparse

from app.core.logging import get_logger
from app.models.schema import Cell, EntityRow, ScrapedPage
from app.services.merger import _pick_better_cell
from app.services.source_quality import classify_source
from app.utils.text import normalize_name, truncate
from app.utils.url import extract_domain

log = get_logger(__name__)

_OFFICIAL_HINTS = ("official", "about", "contact", "menu", "locations", "location")
_WEBSITE_COLS = {"website", "url", "official_website", "homepage", "link"}


def _canonical_homepage(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc
    return f"{scheme}://{netloc}/" if netloc else url


def _current_website(row: EntityRow) -> str | None:
    for col in _WEBSITE_COLS:
        cell = row.cells.get(col)
        if cell and cell.value:
            return cell.value
    return None


def _mentions_entity(page: ScrapedPage, normalized_entity_name: str) -> bool:
    title_match = normalized_entity_name in normalize_name(page.title or "")
    head_text = normalize_name((page.cleaned_text or "")[:500])
    return title_match or normalized_entity_name in head_text


def _page_score_for_entity(row: EntityRow, page: ScrapedPage) -> float:
    name_cell = row.cells.get("name")
    if not name_cell:
        return 0.0

    normalized_entity_name = normalize_name(name_cell.value)
    if not normalized_entity_name:
        return 0.0

    if not _mentions_entity(page, normalized_entity_name):
        return 0.0

    kind, quality = classify_source(page.url, page.title, _current_website(row))
    if kind in {"editorial", "directory", "marketplace"}:
        return 0.0

    score = quality
    title_l = (page.title or "").lower()
    path_l = urlparse(page.url).path.lower()

    if any(hint in title_l or hint in path_l for hint in _OFFICIAL_HINTS):
        score += 0.15
    if path_l in {"", "/"}:
        score += 0.05
    if len(path_l.strip("/").split("/")) <= 1:
        score += 0.05
    return min(score, 1.0)


def resolve_official_sites(
    rows: list[EntityRow],
    pages: list[ScrapedPage],
) -> tuple[list[EntityRow], int]:
    """
    Attach best-guess official website/homepage to discovered rows.

    The resolver is intentionally heuristic and conservative:
    - only non-editorial / non-directory / non-marketplace domains are eligible
    - the page title or top-of-page text must mention the entity name
    - official/about/contact/menu/location cues are preferred
    """
    resolved = 0

    for row in rows:
        best_page: ScrapedPage | None = None
        best_score = 0.0
        for page in pages:
            score = _page_score_for_entity(row, page)
            if score > best_score:
                best_page = page
                best_score = score

        if best_page is None or best_score < 0.7:
            continue

        homepage = _canonical_homepage(best_page.url)
        domain = extract_domain(homepage)
        if not domain:
            continue

        website_cell = Cell(
            value=homepage,
            source_url=best_page.url,
            source_title=best_page.title or None,
            evidence_snippet=truncate(best_page.title or homepage, 200),
            confidence=round(best_score, 3),
        )

        existing = row.cells.get("website")
        if existing:
            row.cells["website"] = _pick_better_cell(existing, website_cell)
        else:
            row.cells["website"] = website_cell

        if row.canonical_domain != domain:
            row.canonical_domain = domain
            resolved += 1

    if resolved:
        log.info("Official-site resolver set canonical domains for %d rows", resolved)
    return rows, resolved
