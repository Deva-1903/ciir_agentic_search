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
import time as _time
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
from app.services.deterministic_extractors import extract_deterministic_entities
from app.services.field_validator import validate_and_normalize
from app.services.llm import chat_json
from app.utils.text import chunk_text, truncate

log = get_logger(__name__)

# ── Provider cooldown (in-process circuit breaker) ────────────────────────────
# After a provider raises an exception, skip it for this many seconds.
# Prevents all concurrent chunks from hammering a rate-limited provider.
_PROVIDER_COOLDOWN_SECONDS = 60.0
_provider_failure_time: dict[str, float] = {}


def _provider_on_cooldown(provider: str) -> bool:
    last_fail = _provider_failure_time.get(provider, 0.0)
    return (_time.monotonic() - last_fail) < _PROVIDER_COOLDOWN_SECONDS


def _record_provider_failure(provider: str) -> None:
    _provider_failure_time[provider] = _time.monotonic()
    log.warning("Provider %s entered cooldown for %.0fs", provider, _PROVIDER_COOLDOWN_SECONDS)

# ── Prompts ───────────────────────────────────────────────────────────────────

_FILL_SYSTEM = """You are a precise structured data extractor. Your job is to read web page content
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

_FILL_USER_TEMPLATE = """User query: {query}
Entity type to extract: {entity_type}
Columns to look for: {columns}

Source URL: {source_url}
Page title: {page_title}

--- PAGE CONTENT START ---
{content}
--- PAGE CONTENT END ---

Extract all {entity_type} entities from this page that are relevant to the query.
Remember: omit any column not supported by the content."""

_DISCOVERY_SYSTEM = """You are a high-recall entity discovery extractor.

Your job is to scan web page content and return ALL plausible entity candidates
relevant to the user query. Candidate discovery prioritizes recall first.

Return ONLY a JSON object in this exact shape:

{
  "entities": [
    {
      "entity_name": "<candidate entity name>",
      "cells": {
        "<column_name>": {
          "value": "<supported value>",
          "evidence_snippet": "<short quote from the page>",
          "confidence": <float 0.0-1.0>
        }
      }
    }
  ]
}

Critical rules:
1. Return EVERY plausible candidate entity on the page that matches the query.
2. Prioritize entity names over completeness. A candidate with only a supported
   name is still useful.
3. Include lightweight fields such as website, address/location, phone, or
   category only when clearly attached to that entity on the page.
4. Never collapse the page to a single "best" entity if multiple candidates are listed.
5. Do not invent values. Omit unsupported columns.
6. Output valid JSON only.
"""

_DISCOVERY_USER_TEMPLATE = """User query: {query}
Candidate entity type: {entity_type}
Discovery columns: {columns}

Source URL: {source_url}
Page title: {page_title}

--- PAGE CONTENT START ---
{content}
--- PAGE CONTENT END ---

Extract ALL relevant {entity_type} candidates from this page.
Preserve every plausible candidate name you can ground in the content."""


def _bump_stat(stats: dict[str, int] | None, key: str, amount: int = 1) -> None:
    """Increment an extraction stats counter if stats tracking is enabled."""
    if stats is None:
        return
    stats[key] = stats.get(key, 0) + amount


def _provider_is_configured(settings, provider: str) -> bool:
    """Return True when the named provider appears usable for extraction."""
    provider_config = getattr(settings, "provider_config", None)
    if callable(provider_config):
        try:
            api_key, _, _ = provider_config(provider)
            return bool(api_key)
        except Exception:
            pass

    if provider == "openai":
        return bool(getattr(settings, "openai_api_key", ""))
    if provider == "groq":
        return bool(getattr(settings, "groq_api_key", ""))
    return provider == getattr(settings, "extractor_provider", None)


def _extractor_provider_order(settings) -> list[str]:
    """Return primary extractor provider followed by one configured fallback."""
    primary = getattr(settings, "extractor_provider", "openai")
    order: list[str] = []
    for provider in (primary, "openai", "groq"):
        if provider in order:
            continue
        if _provider_is_configured(settings, provider):
            order.append(provider)
    return order or [primary]


def build_candidate_discovery_plan(plan: PlannerOutput) -> PlannerOutput:
    """Return a lighter-weight schema used for candidate discovery.

    Discovery-first extraction is recall-oriented: it tries to surface every
    plausible entity with a name and a couple of lightweight anchoring fields.
    We take the first 4 schema columns (name plus the three most fundamental
    fields from the plan template) rather than favouring any vertical's
    column names.
    """
    if not plan.columns:
        columns = ["name"]
    else:
        # `name` is always first in the schema; keep up to 4 columns total.
        columns = list(plan.columns[:4])
        if "name" not in columns:
            columns = ["name"] + [c for c in columns if c != "name"]

    return PlannerOutput(
        query_family=plan.query_family,
        entity_type=plan.entity_type,
        columns=columns,
        search_angles=list(plan.search_angles),
        facets=list(plan.facets),
    )


def _prompt_for_mode(mode: str) -> tuple[str, str]:
    if mode == "discovery":
        return _DISCOVERY_SYSTEM, _DISCOVERY_USER_TEMPLATE
    return _FILL_SYSTEM, _FILL_USER_TEMPLATE


# ── Per-chunk extraction ───────────────────────────────────────────────────────

async def _extract_from_chunk(
    query: str,
    plan: PlannerOutput,
    page: ScrapedPage,
    chunk: str,
    mode: str = "fill",
    stats: dict[str, int] | None = None,
) -> list[EntityDraft]:
    """Run LLM extraction on a single text chunk."""
    system_prompt, user_template = _prompt_for_mode(mode)
    user_msg = user_template.format(
        query=query,
        entity_type=plan.entity_type,
        columns=", ".join(plan.columns),
        source_url=page.url,
        page_title=page.title or "(unknown)",
        content=chunk,
    )

    settings = get_settings()
    raw: dict | None = None
    provider_order = _extractor_provider_order(settings)
    primary_provider = provider_order[0]

    for idx, provider in enumerate(provider_order):
        if _provider_on_cooldown(provider):
            _bump_stat(stats, "provider_skipped_cooldown")
            log.info("Skipping provider %s — on cooldown after recent failure", provider)
            continue
        _bump_stat(stats, "llm_calls_attempted")
        try:
            raw = await chat_json(
                system_prompt,
                user_msg,
                temperature=0.1,
                max_tokens=4096,
                timeout=settings.extract_llm_timeout_seconds,
                attempts=settings.extract_llm_max_attempts,
                provider=provider,
                usage_stats=stats,
            )
            if idx > 0 or provider != primary_provider:
                _bump_stat(stats, "provider_fallback_successes")
                log.warning(
                    "Extraction recovered for %s via fallback provider %s after %s failed",
                    page.url,
                    provider,
                    primary_provider,
                )
            break
        except Exception as exc:
            _record_provider_failure(provider)
            if idx + 1 < len(provider_order):
                next_provider = provider_order[idx + 1]
                _bump_stat(stats, "provider_fallback_attempts")
                log.warning(
                    "Extraction LLM call failed for %s via %s: %s. Trying fallback provider %s.",
                    page.url,
                    provider,
                    exc,
                    next_provider,
                )
                continue
            log.warning("Extraction LLM call failed for %s via %s: %s", page.url, provider, exc)
            return []

    if raw is None:
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
                normalized, ok = validate_and_normalize(
                    col,
                    value,
                    source_url=page.url,
                    source_title=page.title or None,
                )
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


def _non_name_cells(draft: EntityDraft) -> int:
    return sum(1 for col in draft.cells if col != "name")


def _deterministic_result_is_sufficient(
    page: ScrapedPage,
    plan: PlannerOutput,
    mode: str,
    drafts: list[EntityDraft],
) -> bool:
    if not drafts:
        return False

    regime = page.evidence_regime
    best_non_name = max((_non_name_cells(draft) for draft in drafts), default=0)
    target_non_name = max(1, len([col for col in plan.columns if col != "name"]))
    coverage = best_non_name / target_non_name

    if mode == "discovery":
        if regime in {"directory_listing", "local_business_listing"} and len(drafts) >= 2:
            return True
        return best_non_name >= 2

    if regime == "software_repo_or_docs":
        return coverage >= 0.4 or any(
            {"website_or_repo", "license", "maintainer_or_org"} & set(draft.cells)
            for draft in drafts
        )

    if regime in {"official_site", "local_business_listing"}:
        return coverage >= 0.4 or any(
            {"website", "url", "homepage", "location", "phone", "contact_or_booking"} & set(draft.cells)
            for draft in drafts
        )

    return coverage >= 0.5


# ── Public API ─────────────────────────────────────────────────────────────────

async def extract_from_page(
    query: str,
    plan: PlannerOutput,
    page: ScrapedPage,
    llm_sem: asyncio.Semaphore | None = None,
    mode: str = "fill",
    stats: dict[str, int] | None = None,
) -> list[EntityDraft]:
    """Extract entities from a single scraped page."""
    _bump_stat(stats, f"regime_{page.evidence_regime}_extraction_pages")
    settings = get_settings()
    _bump_stat(stats, "pages_seen")
    deterministic_drafts = extract_deterministic_entities(query, plan, page, mode=mode)
    if deterministic_drafts:
        _bump_stat(stats, "deterministic_pages_with_entities")
        _bump_stat(stats, "deterministic_entities", len(deterministic_drafts))
        log.info(
            "Deterministic extractor found %d entities for %s (regime=%s, mode=%s)",
            len(deterministic_drafts),
            page.url,
            page.evidence_regime,
            mode,
        )
        if _deterministic_result_is_sufficient(page, plan, mode, deterministic_drafts):
            merged = _merge_within_page(deterministic_drafts)
            _bump_stat(stats, "pages_routed_deterministic")
            _bump_stat(stats, "entities_extracted", len(merged))
            if merged:
                _bump_stat(stats, "pages_with_entities")
            return merged

    chunks = chunk_text(
        page.cleaned_text,
        max_tokens=settings.chunk_token_limit,
        max_chunks=settings.max_chunks_per_page,
    )
    _bump_stat(stats, "chunks_seen", len(chunks))

    if deterministic_drafts:
        _bump_stat(stats, "pages_routed_hybrid")
    else:
        _bump_stat(stats, "pages_routed_llm")

    log.info(
        "Extracting from %s (%d chunk(s), regime=%s, mode=%s)",
        page.url,
        len(chunks),
        page.evidence_regime,
        mode,
    )

    async def _run_chunk(chunk: str) -> list[EntityDraft]:
        if llm_sem is None:
            return await _extract_from_chunk(query, plan, page, chunk, mode=mode, stats=stats)
        async with llm_sem:
            return await _extract_from_chunk(query, plan, page, chunk, mode=mode, stats=stats)

    if len(chunks) == 1:
        drafts = await _run_chunk(chunks[0])
    else:
        tasks = [_run_chunk(c) for c in chunks]
        results = await asyncio.gather(*tasks)
        drafts = [d for batch in results for d in batch]

    merged = _merge_within_page(deterministic_drafts + drafts)
    _bump_stat(stats, "entities_extracted", len(merged))
    if merged:
        _bump_stat(stats, "pages_with_entities")
    log.info("  → %d entities from %s", len(merged), page.url)
    return merged


async def extract_from_pages(
    query: str,
    plan: PlannerOutput,
    pages: list[ScrapedPage],
    mode: str = "fill",
    stats: dict[str, int] | None = None,
) -> list[EntityDraft]:
    """Extract entities from all pages, bounded to N concurrent LLM calls."""
    settings = get_settings()
    llm_sem = asyncio.Semaphore(settings.max_concurrent_extractions)
    results = await asyncio.gather(
        *[extract_from_page(query, plan, page, llm_sem=llm_sem, mode=mode, stats=stats) for page in pages]
    )
    all_drafts = [d for batch in results for d in batch]
    log.info(
        "Extraction complete: %d candidate entities from %d pages (mode=%s)",
        len(all_drafts),
        len(pages),
        mode,
    )
    return all_drafts
