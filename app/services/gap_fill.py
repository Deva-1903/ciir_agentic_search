"""
Targeted gap-fill: enrich sparse entities by running focused queries.

For each sparse entity:
  1. Build targeted queries for missing columns (e.g. "CompanyX headquarters")
  2. Run Brave search
  3. Scrape 1–2 pages
  4. Extract only the missing attributes
  5. Merge new cells back into the existing EntityRow

Bounds:
  - max 3 entities (configurable)
  - max 2 URLs per entity
  - 1 round only (no recursive refinement)
"""

from __future__ import annotations

from app.models.schema import Cell, EntityRow, PlannerOutput
from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.brave_search import run_brave_search
from app.services.extractor import extract_from_page
from app.services.merger import _pick_better_cell
from app.services.ranker import find_sparse_rows
from app.services.scraper import scrape_pages, scrape_urls
from app.utils.dedupe import names_are_similar

log = get_logger(__name__)

_COLUMN_QUERY_HINTS = {
    # Website / canonical URL columns.
    "website": "official website",
    "url": "official website",
    "homepage": "official website",
    "site": "official website",
    "website_or_repo": "official website repository",
    "website_or_profile": "official website profile",
    # Contact / geographic columns.
    "address": "address",
    "location": "location",
    "headquarters": "headquarters",
    "phone": "phone number",
    "phone_number": "phone number",
    "telephone": "phone number",
    "contact_or_booking": "contact booking",
    # Structural attribute columns (reusable across verticals).
    "category": "category",
    "offering": "offering services",
    "focus_area": "focus area",
    "product_or_service": "product service",
    "stage_or_status": "status stage",
    "funding": "funding raised",
    "founded": "founded year",
    "employees": "employees headcount",
    "primary_use_case": "use case",
    "license": "license",
    "language_or_stack": "language stack technology",
    "maintainer_or_org": "maintainer organization",
    "key_feature": "features",
    "price_or_availability": "price availability",
    "maker_or_brand": "brand manufacturer",
    "affiliation": "affiliation",
    "role_or_title": "role title",
    "notable_work": "notable work",
    # Rating / review columns.
    "rating": "reviews rating",
    "score": "score",
}

_GAP_FILL_REGIME_PRIORITY = {
    "organization_company": {
        "official_site": 4,
        "editorial_article": 3,
        "directory_listing": 2,
        "unknown": 1,
        "software_repo_or_docs": 1,
        "local_business_listing": 0,
        "marketplace_aggregator": -2,
    },
    "place_venue": {
        "official_site": 4,
        "local_business_listing": 4,
        "directory_listing": 2,
        "editorial_article": 1,
        "unknown": 1,
        "software_repo_or_docs": 0,
        "marketplace_aggregator": -2,
    },
    "software_project": {
        "software_repo_or_docs": 5,
        "official_site": 3,
        "editorial_article": 2,
        "directory_listing": 1,
        "unknown": 1,
        "local_business_listing": 0,
        "marketplace_aggregator": -2,
    },
    "product_offering": {
        "official_site": 4,
        "editorial_article": 2,
        "directory_listing": 2,
        "unknown": 1,
        "software_repo_or_docs": 1,
        "local_business_listing": 1,
        "marketplace_aggregator": -1,
    },
    "person_group": {
        "official_site": 3,
        "editorial_article": 3,
        "directory_listing": 1,
        "unknown": 1,
        "software_repo_or_docs": 0,
        "local_business_listing": 0,
        "marketplace_aggregator": -2,
    },
    "generic_entity_list": {
        "official_site": 2,
        "editorial_article": 2,
        "directory_listing": 2,
        "software_repo_or_docs": 2,
        "local_business_listing": 2,
        "unknown": 1,
        "marketplace_aggregator": -2,
    },
}


def _build_gap_queries(entity_name: str, missing_cols: list[str], query: str) -> list[str]:
    """Generate focused search queries for the missing attributes."""
    queries: list[str] = []
    seen: set[str] = set()

    def _add(q: str) -> None:
        normalized = q.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            queries.append(normalized)

    _add(f"\"{entity_name}\" {query}")
    _add(f"\"{entity_name}\" official website")

    for col in missing_cols:
        hint = _COLUMN_QUERY_HINTS.get(col, col.replace("_", " "))
        _add(f"\"{entity_name}\" {hint}")
        if len(queries) >= 3:
            break

    return queries[:3]


def _make_gap_plan(plan: PlannerOutput, missing_cols: list[str]) -> PlannerOutput:
    columns = ["name"] + [col for col in missing_cols if col != "name"]
    return PlannerOutput(
        entity_type=plan.entity_type,
        columns=columns,
        search_angles=[],
    )


def _missing_cols(row: EntityRow, plan: PlannerOutput) -> list[str]:
    return [col for col in plan.columns if col not in row.cells]


def _official_urls_for_row(row: EntityRow) -> list[str]:
    website = row.cells.get("website") or row.cells.get("homepage") or row.cells.get("url")
    if not website or not website.value:
        return []

    base = website.value.rstrip("/")
    urls = [website.value]
    for suffix in ("/about", "/contact"):
        candidate = f"{base}{suffix}"
        if candidate not in urls:
            urls.append(candidate)
    return urls[:2]


def _page_fill_priority(page, plan: PlannerOutput) -> tuple[int, float]:
    weights = _GAP_FILL_REGIME_PRIORITY.get(plan.query_family, _GAP_FILL_REGIME_PRIORITY["generic_entity_list"])
    return (
        weights.get(getattr(page, "evidence_regime", "unknown"), 0),
        float(getattr(page, "regime_confidence", 0.0)),
    )


async def _scrape_urls_maybe_with_stats(urls: list[str], stats: dict[str, int] | None):
    try:
        return await scrape_urls(urls, stats=stats)
    except TypeError:
        return await scrape_urls(urls)


async def _scrape_pages_maybe_with_stats(results, stats: dict[str, int] | None):
    try:
        return await scrape_pages(results, stats=stats)
    except TypeError:
        return await scrape_pages(results)


async def _extract_page_maybe_with_stats(query: str, plan: PlannerOutput, page, stats: dict[str, int] | None):
    try:
        return await extract_from_page(query, plan, page, stats=stats)
    except TypeError:
        return await extract_from_page(query, plan, page)


async def run_gap_fill(
    rows: list[EntityRow],
    plan: PlannerOutput,
    query: str,
    stats: dict[str, int] | None = None,
) -> tuple[list[EntityRow], bool]:
    """
    Attempt to fill sparse cells in top N rows.

    Returns (updated_rows, gap_fill_used).
    Modifies rows in-place; returned list is same object.
    """
    settings = get_settings()
    sparse = find_sparse_rows(rows, plan, top_n=settings.gap_fill_max_entities)

    if not sparse:
        return rows, False

    gap_fill_used = False

    for row in sparse:
        missing = _missing_cols(row, plan)
        if not missing:
            continue

        entity_name = row.cells.get("name")
        if not entity_name:
            continue

        entity_name_str = entity_name.value
        log.info(
            "Gap-fill: %s  missing=%s",
            entity_name_str,
            missing,
        )

        gap_queries = _build_gap_queries(entity_name_str, missing, query)
        brave_results = await run_brave_search(
            gap_queries, top_k=settings.gap_fill_max_urls_per_entity
        )
        gap_plan = _make_gap_plan(plan, missing)
        focused_query = entity_name_str

        pages = []
        official_urls = _official_urls_for_row(row)
        if official_urls:
            pages.extend(await _scrape_urls_maybe_with_stats(official_urls, stats))

        # Only scrape a small number of additional search-result pages
        limited = brave_results[: settings.gap_fill_max_urls_per_entity]
        if limited:
            pages.extend(await _scrape_pages_maybe_with_stats(limited, stats))

        # Preserve order while deduplicating URLs.
        deduped_pages: list = []
        seen_urls: set[str] = set()
        for page in pages:
            if page.url in seen_urls:
                continue
            seen_urls.add(page.url)
            deduped_pages.append(page)
        pages = deduped_pages

        pages.sort(key=lambda page: _page_fill_priority(page, plan), reverse=True)

        for page in pages:
            if getattr(page, "evidence_regime", "unknown") == "marketplace_aggregator":
                continue
            drafts = await _extract_page_maybe_with_stats(focused_query, gap_plan, page, stats)
            for draft in drafts:
                if not names_are_similar(draft.entity_name, entity_name_str):
                    continue

                # Only accept cells for missing columns
                for col, cell_draft in draft.cells.items():
                    if col not in missing:
                        continue
                    new_cell = Cell(
                        value=cell_draft.value,
                        source_url=page.url,
                        source_title=page.title or None,
                        evidence_snippet=cell_draft.evidence_snippet,
                        confidence=cell_draft.confidence,
                    )
                    if col not in row.cells:
                        row.cells[col] = new_cell
                        gap_fill_used = True
                    else:
                        row.cells[col] = _pick_better_cell(row.cells[col], new_cell)

            missing = _missing_cols(row, plan)
            if not missing:
                break

        # Recompute aggregate confidence after gap fill
        confs = [c.confidence for c in row.cells.values()]
        if confs:
            row.aggregate_confidence = round(sum(confs) / len(confs), 3)

    return rows, gap_fill_used
