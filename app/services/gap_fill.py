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
    "address": "address",
    "phone": "phone number",
    "phone_number": "phone number",
    "website": "official website",
    "url": "official website",
    "homepage": "official website",
    "rating": "reviews rating",
    "price": "price range",
    "price_range": "price range",
    "location": "location",
    "headquarters": "headquarters",
    "founded": "founded",
    "year_founded": "year founded",
    "founders": "founders",
    "funding_stage": "funding stage",
    "funding_round": "funding round",
    "funding_amount": "funding amount",
    "amount_raised": "amount raised",
    "investors": "investors",
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


async def run_gap_fill(
    rows: list[EntityRow],
    plan: PlannerOutput,
    query: str,
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
            pages.extend(await scrape_urls(official_urls))

        # Only scrape a small number of additional search-result pages
        limited = brave_results[: settings.gap_fill_max_urls_per_entity]
        if limited:
            pages.extend(await scrape_pages(limited))

        # Preserve order while deduplicating URLs.
        deduped_pages: list = []
        seen_urls: set[str] = set()
        for page in pages:
            if page.url in seen_urls:
                continue
            seen_urls.add(page.url)
            deduped_pages.append(page)
        pages = deduped_pages

        for page in pages:
            drafts = await extract_from_page(focused_query, gap_plan, page)
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
