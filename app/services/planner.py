"""
Schema planner: given a raw user query, infer
  - entity_type
  - columns (5-8, always includes "name")
  - facets: 3-5 typed retrieval facets (each with its own query + intent)

Facet-typed planning replaces plain paraphrase "search_angles". Each facet
states its retrieval intent and which columns it is expected to help fill, so
downstream stages (reranker, extractor) can reason about *why* each page was
retrieved. `search_angles` is still exposed for backward compatibility — it
is simply the list of facet queries.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.schema import PlannerOutput, SearchFacet
from app.services.llm import chat_json_validated

log = get_logger(__name__)

# ── Prompts ───────────────────────────────────────────────────────────────────

_SYSTEM = """You are a search retrieval planner. Given a user's topic query, produce a
structured plan for discovering entities on the web.

Return ONLY a JSON object with exactly these keys:

{
  "entity_type": "<singular noun for the entity type, e.g. startup, restaurant, tool>",
  "columns": ["name", "<col2>", "<col3>", ...],
  "facets": [
    {
      "type": "<one of: entity_list | official_source | editorial_review | attribute_specific | news_recent | comparison>",
      "query": "<natural search-engine query string>",
      "expected_fill_columns": ["<column>", ...],
      "rationale": "<short sentence on why this facet helps>"
    }
  ]
}

Rules:
- "name" must be the first column. Include 5–8 columns total. Prefer concrete,
  discoverable attributes over vague ones like "description" or "overview".
- Produce 3–5 facets. Each facet must have a distinct retrieval INTENT — do not
  just paraphrase the user query.
- Facet types explained:
    entity_list        → broad list / "top X" / roundup pages for candidate discovery
    official_source    → official homepages, about/contact/menu pages (high trust)
    editorial_review   → editorial, review, or curated-guide articles
    attribute_specific → targets a specific column (e.g. funding, rating, phone)
    news_recent        → recent news, press releases, announcements
    comparison         → comparative / "X vs Y" articles
- Every facet must declare `expected_fill_columns` — the columns it is expected
  to help fill. Only use column names from the `columns` list.
- `rationale` should be one short sentence.
- Output valid JSON only. No comments, no extra keys.
"""

_USER_TEMPLATE = """User query: {query}

Produce the JSON retrieval plan."""


# ── Hardcoded fallback if LLM fails ──────────────────────────────────────────

_FALLBACK_COLUMNS = ["name", "website", "description", "category", "location"]


def _fallback_plan(query: str) -> PlannerOutput:
    """Deterministic fallback used when the planner LLM call fails."""
    facets = [
        SearchFacet(
            type="entity_list",
            query=f"top {query} list",
            expected_fill_columns=["name", "website"],
            rationale="broad list pages surface candidate entities",
        ),
        SearchFacet(
            type="official_source",
            query=f"{query} official website",
            expected_fill_columns=["website", "location"],
            rationale="official pages are higher trust for core facts",
        ),
        SearchFacet(
            type="editorial_review",
            query=f"best {query} review",
            expected_fill_columns=["name", "category"],
            rationale="editorial reviews provide curated, vetted entries",
        ),
    ]
    return PlannerOutput(
        entity_type="entity",
        columns=list(_FALLBACK_COLUMNS),
        search_angles=[f.query for f in facets],
        facets=facets,
    )


# ── Public API ─────────────────────────────────────────────────────────────────

async def plan_schema(query: str) -> PlannerOutput:
    """Return a facet-typed schema plan for the given user query."""
    log.info("Planning schema for query: %r", query)
    try:
        settings = get_settings()
        result = await chat_json_validated(
            _SYSTEM,
            _USER_TEMPLATE.format(query=query),
            PlannerOutput,
            temperature=0.3,
            max_tokens=768,
            provider=settings.planner_provider,
        )
    except Exception as exc:
        log.warning("Planner LLM call failed (%s), using fallback plan.", exc)
        result = _fallback_plan(query)

    result.columns = _ensure_name_first(result.columns[:8])
    result.facets = _sanitize_facets(result.facets, result.columns)[:5]

    # Keep search_angles derived from facet queries for backward compatibility
    # with existing callers (brave_search, routes, gap_fill).
    result.search_angles = [f.query for f in result.facets][:5]

    # Safety net: if the LLM returned no facets, fall back.
    if not result.facets:
        log.warning("Planner returned no facets; using fallback plan.")
        result = _fallback_plan(query)

    log.info(
        "Plan: entity_type=%r columns=%s facets=%s",
        result.entity_type,
        result.columns,
        [(f.type, f.query) for f in result.facets],
    )
    return result


def _ensure_name_first(columns: list[str]) -> list[str]:
    cols = [c for c in columns if c.lower() != "name"]
    return ["name"] + cols


def _sanitize_facets(
    facets: list[SearchFacet],
    valid_columns: list[str],
) -> list[SearchFacet]:
    """Drop empty queries and restrict expected_fill_columns to schema columns."""
    valid = set(valid_columns)
    cleaned: list[SearchFacet] = []
    for facet in facets:
        q = (facet.query or "").strip()
        if not q:
            continue
        facet.query = q
        facet.expected_fill_columns = [
            c for c in facet.expected_fill_columns if c in valid
        ]
        cleaned.append(facet)
    return cleaned
