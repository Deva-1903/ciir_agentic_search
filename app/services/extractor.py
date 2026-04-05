"""
LLM-based structured entity extractor.

For each scraped page:
  1. Chunk the text if too long
  2. Send each chunk to the LLM with the schema (entity_type + columns)
  3. Parse structured JSON entities with per-cell evidence
  4. Merge entities found across chunks of the same page

The extractor never invents missing values: it omits unsupported fields.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.schema import (
    CellDraft,
    EntityDraft,
    ExtractionResult,
    PlannerOutput,
    ScrapedPage,
)
from app.services.field_validator import validate_and_normalize
from app.services.llm import chat_json
from app.utils.text import chunk_text, truncate

log = get_logger(__name__)

# ── Prompts ───────────────────────────────────────────────────────────────────

_SYSTEM = """You are a precise structured data extractor. Your job is to read web page content
and extract entities that are relevant to the user's search query.

Return ONLY a JSON object in this exact shape:

{
  "entities": [
    {
      "entity_name": "<name of the entity>",
      "cells": {
        "<column_name>": {
          "value": "<extracted value>",
          "evidence_snippet": "<short verbatim quote from the content that supports this value>",
          "confidence": <float 0.0–1.0>
        }
      }
    }
  ]
}

Critical rules:
1. ONLY extract entities relevant to the user query.
2. ONLY include cell values directly supported by the provided content.
   Do NOT use world knowledge to fill in gaps.
3. The "evidence_snippet" must be a short verbatim or near-verbatim excerpt (≤150 chars)
   from the page content that justifies the value.
4. If a column value is not mentioned in the content, omit that column entirely.
5. Confidence reflects how clearly the value is stated (0.9+ = explicit, 0.5–0.8 = implied).
6. Always include the "name" column if the entity name is findable.
7. If no relevant entities are found, return {"entities": []}.
8. Do NOT add any extra keys. Output valid JSON only.
"""

_USER_TEMPLATE = """User query: {query}
Entity type to extract: {entity_type}
Columns to look for: {columns}

Source URL: {source_url}
Page title: {page_title}

--- PAGE CONTENT START ---
{content}
--- PAGE CONTENT END ---

Extract all {entity_type} entities from this page that are relevant to the query.
Remember: omit any column not supported by the content."""


# ── Per-chunk extraction ───────────────────────────────────────────────────────

async def _extract_from_chunk(
    query: str,
    plan: PlannerOutput,
    page: ScrapedPage,
    chunk: str,
) -> list[EntityDraft]:
    """Run LLM extraction on a single text chunk."""
    user_msg = _USER_TEMPLATE.format(
        query=query,
        entity_type=plan.entity_type,
        columns=", ".join(plan.columns),
        source_url=page.url,
        page_title=page.title or "(unknown)",
        content=chunk,
    )

    settings = get_settings()
    try:
        raw = await chat_json(
            _SYSTEM,
            user_msg,
            temperature=0.1,
            max_tokens=4096,
            timeout=settings.extract_llm_timeout_seconds,
            attempts=settings.extract_llm_max_attempts,
        )
    except Exception as exc:
        log.warning("Extraction LLM call failed for %s: %s", page.url, exc)
        return []

    entities_raw = raw.get("entities", [])
    if not isinstance(entities_raw, list):
        return []

    drafts: list[EntityDraft] = []
    for item in entities_raw:
        if not isinstance(item, dict):
            continue
        entity_name: str = item.get("entity_name", "").strip()
        if not entity_name:
            continue

        cells_raw = item.get("cells", {})
        if not isinstance(cells_raw, dict):
            continue

        cells: dict[str, CellDraft] = {}
        for col, cell_data in cells_raw.items():
            if not isinstance(cell_data, dict):
                continue
            value = str(cell_data.get("value", "")).strip()
            snippet = str(cell_data.get("evidence_snippet", "")).strip()
            conf = float(cell_data.get("confidence", 0.5))
            conf = max(0.0, min(1.0, conf))
            if value and snippet:
                normalized, ok = validate_and_normalize(col, value)
                if not ok:
                    log.debug("Dropping malformed cell %s=%r (page=%s)", col, value, page.url)
                    continue
                cells[col] = CellDraft(
                    value=normalized,
                    evidence_snippet=truncate(snippet, 200),
                    confidence=conf,
                )

        # The model sometimes fills entity_name but omits the explicit "name" cell.
        # Promote the entity_name into the schema so downstream merge/ranking logic
        # can treat the row as a usable result.
        if "name" in plan.columns and "name" not in cells:
            cells["name"] = CellDraft(
                value=entity_name,
                evidence_snippet=truncate(entity_name, 200),
                confidence=0.75,
            )

        if cells:
            drafts.append(
                EntityDraft(
                    entity_name=entity_name,
                    cells=cells,
                    source_url=page.url,
                    source_title=page.title or None,
                )
            )

    return drafts


# ── Cross-chunk merge for same page ───────────────────────────────────────────

def _merge_within_page(all_drafts: list[EntityDraft]) -> list[EntityDraft]:
    """
    Merge EntityDrafts from multiple chunks of the same page.
    Two drafts are merged if their names are identical (case-insensitive).
    When merging cells, keep the higher-confidence cell per column.
    """
    from app.utils.dedupe import names_are_similar

    merged: list[EntityDraft] = []

    for draft in all_drafts:
        matched_idx: Optional[int] = None
        for i, existing in enumerate(merged):
            if names_are_similar(draft.entity_name, existing.entity_name):
                matched_idx = i
                break

        if matched_idx is None:
            merged.append(draft)
        else:
            existing = merged[matched_idx]
            for col, new_cell in draft.cells.items():
                if col not in existing.cells:
                    existing.cells[col] = new_cell
                elif new_cell.confidence > existing.cells[col].confidence:
                    existing.cells[col] = new_cell

    return merged


# ── Public API ─────────────────────────────────────────────────────────────────

async def extract_from_page(
    query: str,
    plan: PlannerOutput,
    page: ScrapedPage,
    llm_sem: asyncio.Semaphore | None = None,
) -> list[EntityDraft]:
    """Extract entities from a single scraped page."""
    settings = get_settings()
    chunks = chunk_text(
        page.cleaned_text,
        max_tokens=settings.chunk_token_limit,
        max_chunks=settings.max_chunks_per_page,
    )

    log.info("Extracting from %s (%d chunk(s))", page.url, len(chunks))

    async def _run_chunk(chunk: str) -> list[EntityDraft]:
        if llm_sem is None:
            return await _extract_from_chunk(query, plan, page, chunk)
        async with llm_sem:
            return await _extract_from_chunk(query, plan, page, chunk)

    if len(chunks) == 1:
        drafts = await _run_chunk(chunks[0])
    else:
        tasks = [_run_chunk(c) for c in chunks]
        results = await asyncio.gather(*tasks)
        drafts = [d for batch in results for d in batch]

    merged = _merge_within_page(drafts)
    log.info("  → %d entities from %s", len(merged), page.url)
    return merged


async def extract_from_pages(
    query: str,
    plan: PlannerOutput,
    pages: list[ScrapedPage],
) -> list[EntityDraft]:
    """Extract entities from all pages, bounded to N concurrent LLM calls."""
    settings = get_settings()
    llm_sem = asyncio.Semaphore(settings.max_concurrent_extractions)
    results = await asyncio.gather(
        *[extract_from_page(query, plan, page, llm_sem=llm_sem) for page in pages]
    )
    all_drafts = [d for batch in results for d in batch]
    log.info("Extraction complete: %d candidate entities from %d pages", len(all_drafts), len(pages))
    return all_drafts
