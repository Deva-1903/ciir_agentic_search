"""Heuristic official-site resolution for discovered candidates."""

from __future__ import annotations

from urllib.parse import urlparse

from app.core.logging import get_logger
from app.models.schema import Cell, EntityRow, ScrapedPage
from app.services.field_validator import normalize_website
from app.services.merger import _pick_better_cell
from app.services.source_quality import classify_source
from app.utils.text import normalize_name, truncate
from app.utils.url import extract_domain

log = get_logger(__name__)

# Structural "this is an entity's canonical page" hints. Generic across
# verticals — about/contact/home/docs are the common official-page markers.
_OFFICIAL_HINTS = ("official", "about", "contact", "home", "docs", "overview")
_WEBSITE_COLS = {
    "website", "url", "official_website", "homepage", "link",
    "website_or_repo", "website_or_profile", "site",
}
_LISTING_PATH_HINTS = ("/category", "/categories", "/directory", "/industry", "/list", "/lists", "/tag", "/tags")
_LISTING_QUERY_HINTS = ("category=", "categories=", "industry=", "industries=", "tag=")
_LISTING_TITLE_HINTS = ("directory", "industry", "category", "list of", "top ", "best ")


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


def _set_website_cell(row: EntityRow, website: Cell) -> None:
    row.cells["website"] = website


def _sanitize_existing_website(row: EntityRow, canonical_domain: str | None = None) -> None:
    existing = row.cells.get("website")
    if not existing:
        return

    normalized, ok = normalize_website(
        existing.value,
        source_url=existing.source_url,
        source_title=existing.source_title,
        canonical_domain=canonical_domain or row.canonical_domain,
    )
    if not ok:
        row.cells.pop("website", None)
        return

    if normalized != existing.value:
        _set_website_cell(
            row,
            Cell(
                value=normalized,
                source_url=existing.source_url,
                source_title=existing.source_title,
                evidence_snippet=existing.evidence_snippet,
                confidence=existing.confidence,
            ),
        )


def _mentions_entity(page: ScrapedPage, normalized_entity_name: str) -> bool:
    title_match = normalized_entity_name in normalize_name(page.title or "")
    head_text = normalize_name((page.cleaned_text or "")[:500])
    return title_match or normalized_entity_name in head_text


def _looks_like_listing_page(page: ScrapedPage) -> bool:
    parsed = urlparse(page.url)
    path_l = parsed.path.lower()
    query_l = parsed.query.lower()
    title_l = (page.title or "").lower()
    return (
        any(hint in path_l for hint in _LISTING_PATH_HINTS)
        or any(hint in query_l for hint in _LISTING_QUERY_HINTS)
        or any(hint in title_l for hint in _LISTING_TITLE_HINTS)
    )


def _page_score_for_entity(row: EntityRow, page: ScrapedPage) -> float:
    name_cell = row.cells.get("name")
    if not name_cell:
        return 0.0

    normalized_entity_name = normalize_name(name_cell.value)
    if not normalized_entity_name:
        return 0.0

    if not _mentions_entity(page, normalized_entity_name):
        return 0.0
    if _looks_like_listing_page(page):
        return 0.0

    official_hint_url = f"https://{row.canonical_domain}/" if row.canonical_domain else None
    kind, quality = classify_source(
        page.url,
        page.title,
        official_hint_url,
        source_regime=getattr(page, "evidence_regime", None),
    )
    if getattr(page, "evidence_regime", "unknown") in {"directory_listing", "editorial_article", "marketplace_aggregator"}:
        return 0.0
    if kind in {"editorial", "marketplace"}:
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
        _sanitize_existing_website(row)
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
            normalized_existing, ok = normalize_website(
                existing.value,
                source_url=existing.source_url,
                source_title=existing.source_title,
                canonical_domain=domain,
            )
            if not ok or extract_domain(normalized_existing) != domain or normalized_existing != homepage:
                _set_website_cell(row, website_cell)
            else:
                if normalized_existing != existing.value:
                    existing = Cell(
                        value=normalized_existing,
                        source_url=existing.source_url,
                        source_title=existing.source_title,
                        evidence_snippet=existing.evidence_snippet,
                        confidence=existing.confidence,
                    )
                _set_website_cell(row, _pick_better_cell(existing, website_cell))
        else:
            _set_website_cell(row, website_cell)

        if row.canonical_domain != domain:
            row.canonical_domain = domain
            resolved += 1

    if resolved:
        log.info("Official-site resolver set canonical domains for %d rows", resolved)
    return rows, resolved
