"""
Constrained schema planner.

The planner intentionally separates:
  1. query-family classification (deterministic + stable)
  2. schema template selection (deterministic + domain-appropriate)
  3. facet generation (LLM-assisted when available, deterministic fallback)

This keeps the planner expressive enough for typed retrieval facets while
avoiding fragile generic plans such as entity_type="entity" with vague columns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.schema import PlannerOutput, SearchFacet
from app.services.llm import chat_json_validated

log = get_logger(__name__)


@dataclass(frozen=True)
class _SchemaTemplate:
    query_family: str
    entity_type: str
    columns: list[str]


class _FacetPlan(BaseModel):
    entity_type: str = ""
    facets: list[SearchFacet] = Field(default_factory=list)


_FAMILY_KEYWORDS = {
    "local_business": (
        "restaurant", "restaurants", "pizza", "ramen", "coffee", "roaster", "roasters",
        "cafe", "cafes", "bar", "bars", "shop", "shops", "hostel", "hostels",
        "hotel", "hotels", "bakery", "bakeries", "trail", "trails", "near",
    ),
    "startup_company": (
        "startup", "startups", "series a", "series b", "founded", "founding",
        "funding", "seed", "venture", "vc", "ai startups",
    ),
    "software_tool": (
        "software", "tool", "tools", "app", "apps", "platform", "platforms",
        "developer", "devtools", "open source", "open-source", "database",
        "databases", "library", "libraries", "framework", "frameworks", "sdk",
    ),
    "product_category": (
        "product", "products", "brand", "brands", "headphones", "laptop",
        "laptops", "camera", "cameras", "sneakers", "phone", "phones",
    ),
    "organization": (
        "company", "companies", "organization", "organizations", "nonprofit",
        "foundation", "association", "hospital", "hospitals", "university",
        "universities", "clinic", "clinics",
    ),
}

_GENERIC_ENTITY_TYPES = {"entity", "business", "company", "organization", "item"}

_SCHEMA_TEMPLATES = {
    "local_business": _SchemaTemplate(
        query_family="local_business",
        entity_type="local business",
        columns=["name", "website", "address", "phone_number", "category", "rating"],
    ),
    "startup_company": _SchemaTemplate(
        query_family="startup_company",
        entity_type="startup",
        columns=["name", "website", "headquarters", "focus_area", "product_or_service", "funding_stage"],
    ),
    "software_tool": _SchemaTemplate(
        query_family="software_tool",
        entity_type="software tool",
        columns=["name", "website", "primary_use_case", "license", "language", "github_repo"],
    ),
    "product_category": _SchemaTemplate(
        query_family="product_category",
        entity_type="product",
        columns=["name", "website", "category", "price_range", "key_feature", "availability"],
    ),
    "organization": _SchemaTemplate(
        query_family="organization",
        entity_type="organization",
        columns=["name", "website", "location", "focus_area", "organization_type", "leadership"],
    ),
    "fallback_generic": _SchemaTemplate(
        query_family="fallback_generic",
        entity_type="entity",
        columns=["name", "website", "location", "category", "notable_attribute"],
    ),
}


_SYSTEM = """You are a search retrieval planner operating under a fixed schema family.

Return ONLY a JSON object with exactly these keys:
{
  "entity_type": "<specific singular noun matching the family>",
  "facets": [
    {
      "type": "<one of: entity_list | official_source | editorial_review | attribute_specific | news_recent | comparison>",
      "query": "<natural search-engine query string>",
      "expected_fill_columns": ["<column>", "..."],
      "rationale": "<short reason>"
    }
  ]
}

Rules:
- You are given a fixed query family and fixed schema columns. Do not invent a new family.
- Keep the entity_type specific when possible. Avoid generic words like "entity".
- Produce 4–5 facets.
- Every facet query must be materially different in retrieval intent.
- expected_fill_columns may only use the provided schema columns.
- Output valid JSON only.
"""

_USER_TEMPLATE = """User query: {query}
Query family: {query_family}
Default entity type: {entity_type}
Fixed schema columns: {columns}

Produce a retrieval plan with family-appropriate facets."""


def classify_query_family(query: str) -> str:
    """Map a raw query to a small stable family set."""
    q = query.lower()

    # Prefer software over organization when both are present.
    if any(token in q for token in _FAMILY_KEYWORDS["software_tool"]):
        return "software_tool"
    if any(token in q for token in _FAMILY_KEYWORDS["startup_company"]):
        return "startup_company"

    local_signals = any(token in q for token in _FAMILY_KEYWORDS["local_business"])
    has_location_phrase = any(marker in q for marker in (" in ", " near ", " around "))
    if local_signals or has_location_phrase:
        return "local_business"

    if any(token in q for token in _FAMILY_KEYWORDS["product_category"]):
        return "product_category"
    if any(token in q for token in _FAMILY_KEYWORDS["organization"]):
        return "organization"
    return "fallback_generic"


def _strip_leading_intent(query: str) -> str:
    q = query.strip()
    return re.sub(
        r"^(top|best|leading|most promising|highest rated|top rated)\s+",
        "",
        q,
        flags=re.IGNORECASE,
    )


def _derive_entity_type(query: str, family: str) -> str:
    q = query.lower()
    if family == "local_business":
        if "pizza" in q:
            return "pizza place"
        if "ramen" in q:
            return "ramen shop"
        if "coffee" in q or "roaster" in q:
            return "coffee roaster"
        if "hostel" in q:
            return "hostel"
        if "trail" in q:
            return "trail"
        if "restaurant" in q:
            return "restaurant"
        return "local business"
    if family == "startup_company":
        return "startup"
    if family == "software_tool":
        if "database" in q:
            return "database"
        if "app" in q or "apps" in q:
            return "software app"
        return "software tool"
    if family == "product_category":
        if "brand" in q or "brands" in q:
            return "brand"
        return "product"
    if family == "organization":
        if "company" in q or "companies" in q:
            return "company"
        if "university" in q or "universities" in q:
            return "university"
        if "hospital" in q or "hospitals" in q:
            return "hospital"
        return "organization"
    return "entity"


def _template_for_query(query: str, family: str | None = None) -> _SchemaTemplate:
    chosen_family = family or classify_query_family(query)
    template = _SCHEMA_TEMPLATES[chosen_family]
    return _SchemaTemplate(
        query_family=template.query_family,
        entity_type=_derive_entity_type(query, template.query_family),
        columns=list(template.columns),
    )


def _deterministic_facets(
    query: str,
    query_family: str,
    columns: list[str],
) -> list[SearchFacet]:
    topic = _strip_leading_intent(query)
    base = topic or query

    if query_family == "local_business":
        return [
            SearchFacet(
                type="entity_list",
                query=f"best {base}",
                expected_fill_columns=["name", "address", "rating"],
                rationale="list pages maximize candidate recall for local places",
            ),
            SearchFacet(
                type="official_source",
                query=f"{base} official website",
                expected_fill_columns=["website", "address", "phone_number"],
                rationale="official sites are best for canonical contact details",
            ),
            SearchFacet(
                type="editorial_review",
                query=f"{base} reviews",
                expected_fill_columns=["name", "category", "rating"],
                rationale="editorial roundups provide ranked candidate lists",
            ),
            SearchFacet(
                type="attribute_specific",
                query=f"{base} phone number address",
                expected_fill_columns=["address", "phone_number"],
                rationale="contact-focused pages fill actionable local fields",
            ),
        ]

    if query_family == "startup_company":
        return [
            SearchFacet(
                type="entity_list",
                query=f"top {base}",
                expected_fill_columns=["name", "website", "focus_area"],
                rationale="roundup lists surface many startup candidates quickly",
            ),
            SearchFacet(
                type="official_source",
                query=f"{base} official website",
                expected_fill_columns=["website", "headquarters", "product_or_service"],
                rationale="official sites are strongest for core company facts",
            ),
            SearchFacet(
                type="editorial_review",
                query=f"{base} company profile",
                expected_fill_columns=["name", "focus_area", "product_or_service"],
                rationale="profiles explain what each company does",
            ),
            SearchFacet(
                type="news_recent",
                query=f"{base} funding announcement",
                expected_fill_columns=["funding_stage", "name"],
                rationale="recent funding news often states company stage clearly",
            ),
        ]

    if query_family == "software_tool":
        return [
            SearchFacet(
                type="entity_list",
                query=f"best {base}",
                expected_fill_columns=["name", "website", "primary_use_case"],
                rationale="comparison pages are high-recall sources for tool candidates",
            ),
            SearchFacet(
                type="official_source",
                query=f"{base} official documentation",
                expected_fill_columns=["website", "primary_use_case", "language"],
                rationale="official docs are best for canonical product facts",
            ),
            SearchFacet(
                type="comparison",
                query=f"{base} comparison",
                expected_fill_columns=["name", "primary_use_case", "license"],
                rationale="comparison pages differentiate similar tools clearly",
            ),
            SearchFacet(
                type="attribute_specific",
                query=f"{base} github license",
                expected_fill_columns=["github_repo", "license", "language"],
                rationale="repo and license pages fill actionable software fields",
            ),
        ]

    if query_family == "organization":
        return [
            SearchFacet(
                type="entity_list",
                query=f"leading {base}",
                expected_fill_columns=["name", "website", "focus_area"],
                rationale="industry lists surface many organizations quickly",
            ),
            SearchFacet(
                type="official_source",
                query=f"{base} official website",
                expected_fill_columns=["website", "location", "leadership"],
                rationale="official pages provide canonical organization details",
            ),
            SearchFacet(
                type="editorial_review",
                query=f"{base} profile",
                expected_fill_columns=["name", "focus_area", "organization_type"],
                rationale="profiles help distinguish similar organizations",
            ),
            SearchFacet(
                type="news_recent",
                query=f"{base} announcement",
                expected_fill_columns=["name", "focus_area"],
                rationale="news helps surface recently active organizations",
            ),
        ]

    if query_family == "product_category":
        return [
            SearchFacet(
                type="entity_list",
                query=f"best {base}",
                expected_fill_columns=["name", "category", "key_feature"],
                rationale="ranked lists maximize candidate recall for products",
            ),
            SearchFacet(
                type="official_source",
                query=f"{base} official website",
                expected_fill_columns=["website", "availability", "price_range"],
                rationale="official sites best capture canonical purchase info",
            ),
            SearchFacet(
                type="comparison",
                query=f"{base} comparison",
                expected_fill_columns=["name", "key_feature", "price_range"],
                rationale="comparison pages help differentiate similar products",
            ),
            SearchFacet(
                type="attribute_specific",
                query=f"{base} price features",
                expected_fill_columns=["price_range", "key_feature"],
                rationale="attribute-focused pages improve fill rate for product fields",
            ),
        ]

    return [
        SearchFacet(
            type="entity_list",
            query=f"top {base}",
            expected_fill_columns=["name", "website"],
            rationale="broad list pages surface candidate entities",
        ),
        SearchFacet(
            type="official_source",
            query=f"{base} official website",
            expected_fill_columns=["website", "location"],
            rationale="official pages ground canonical entity facts",
        ),
        SearchFacet(
            type="editorial_review",
            query=f"{base} review",
            expected_fill_columns=["name", "category"],
            rationale="editorial pages provide cleaner candidate lists than raw search",
        ),
    ]


def _fallback_plan(query: str, family: str | None = None) -> PlannerOutput:
    """Deterministic fallback used when the planner LLM call fails."""
    template = _template_for_query(query, family)
    facets = _deterministic_facets(query, template.query_family, template.columns)
    return PlannerOutput(
        query_family=template.query_family,
        entity_type=template.entity_type,
        columns=list(template.columns),
        search_angles=[f.query for f in facets],
        facets=facets,
    )


async def _llm_facets(query: str, template: _SchemaTemplate) -> _FacetPlan:
    settings = get_settings()
    return await chat_json_validated(
        _SYSTEM,
        _USER_TEMPLATE.format(
            query=query,
            query_family=template.query_family,
            entity_type=template.entity_type,
            columns=", ".join(template.columns),
        ),
        _FacetPlan,
        temperature=0.2,
        max_tokens=640,
        provider=settings.planner_provider,
    )


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
        facet.expected_fill_columns = [c for c in facet.expected_fill_columns if c in valid]
        cleaned.append(facet)
    return cleaned


def _choose_entity_type(llm_entity_type: str, template_entity_type: str) -> str:
    candidate = (llm_entity_type or "").strip().lower()
    if not candidate or candidate in _GENERIC_ENTITY_TYPES:
        return template_entity_type
    return llm_entity_type.strip()


async def plan_schema(query: str) -> PlannerOutput:
    """Return a stable, family-constrained schema plan for the given query."""
    family = classify_query_family(query)
    template = _template_for_query(query, family)
    log.info("Planning schema for query=%r family=%s", query, family)

    try:
        llm_result = await _llm_facets(query, template)
        facets = _sanitize_facets(llm_result.facets, template.columns)[:5]
        entity_type = _choose_entity_type(llm_result.entity_type, template.entity_type)
        result = PlannerOutput(
            query_family=template.query_family,
            entity_type=entity_type,
            columns=_ensure_name_first(template.columns[:8]),
            facets=facets,
        )
    except Exception as exc:
        log.warning("Planner LLM call failed (%s), using deterministic plan.", exc)
        result = _fallback_plan(query, family)

    if not result.facets:
        log.warning("Planner returned no facets; using deterministic plan.")
        result = _fallback_plan(query, family)

    result.columns = _ensure_name_first(result.columns[:8])
    result.facets = _sanitize_facets(result.facets, result.columns)[:5]
    result.search_angles = [f.query for f in result.facets][:5]

    log.info(
        "Plan: family=%s entity_type=%r columns=%s facets=%s",
        result.query_family,
        result.entity_type,
        result.columns,
        [(f.type, f.query) for f in result.facets],
    )
    return result
