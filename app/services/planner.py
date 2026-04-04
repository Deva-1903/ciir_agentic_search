"""
Schema planner: given a raw user query, infer
  - entity_type
  - columns (5-8, always includes "name")
  - search_angles (3-5 diversified queries)
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.models.schema import PlannerOutput
from app.services.llm import chat_json_validated

log = get_logger(__name__)

# ── Prompts ───────────────────────────────────────────────────────────────────

_SYSTEM = """You are a search schema planner. Your job is to analyze a user's topic query
and produce a structured plan for discovering entities on the web.

Return ONLY a JSON object with exactly these keys:

{
  "entity_type": "<singular noun for the type of entity, e.g. startup, restaurant, tool, person>",
  "columns": ["name", "<col2>", "<col3>", ...],
  "search_angles": ["<query1>", "<query2>", ...]
}

Rules:
- "name" must always be the first column.
- Include 5–8 columns total. Choose attributes that are discoverable on the web and
  specific to the entity type. Avoid vague columns like "description" or "overview".
- Include 3–5 search angles. Make them diverse (e.g. combine list-type queries with
  official-site queries, funding queries, news queries) to maximize recall.
- Use natural search engine queries as search angles (not topic phrases).
- Do not add comments or extra keys. Output valid JSON only.
"""

_USER_TEMPLATE = """User query: {query}

Produce the JSON schema plan."""


# ── Hardcoded fallback if LLM fails ──────────────────────────────────────────

_FALLBACK: dict = {
    "entity_type": "entity",
    "columns": ["name", "website", "description", "category", "location"],
    "search_angles": ["{query}", "best {query}", "{query} list", "{query} top companies"],
}


# ── Public API ─────────────────────────────────────────────────────────────────

async def plan_schema(query: str) -> PlannerOutput:
    """Return a schema plan for the given user query."""
    log.info("Planning schema for query: %r", query)
    try:
        result = await chat_json_validated(
            _SYSTEM,
            _USER_TEMPLATE.format(query=query),
            PlannerOutput,
            temperature=0.3,
            max_tokens=512,
        )
    except Exception as exc:
        log.warning("Planner LLM call failed (%s), using fallback schema.", exc)
        fallback = {k: v for k, v in _FALLBACK.items()}
        fallback["search_angles"] = [
            a.format(query=query) for a in fallback["search_angles"]
        ]
        result = PlannerOutput(**fallback)

    # Clamp lengths
    result.columns = _ensure_name_first(result.columns[:8])
    result.search_angles = result.search_angles[:5]

    log.info(
        "Schema: entity_type=%r  columns=%s  angles=%d",
        result.entity_type,
        result.columns,
        len(result.search_angles),
    )
    return result


def _ensure_name_first(columns: list[str]) -> list[str]:
    cols = [c for c in columns if c.lower() != "name"]
    return ["name"] + cols
