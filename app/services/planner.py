"""
Constrained schema planner — structural entity-kind families.

The planner intentionally separates:
  1. entity-kind classification (structural, not example-specific)
  2. schema template selection (deterministic per kind)
  3. facet generation (LLM-assisted when available, deterministic fallback)

Design principle: the families describe the *shape* of entities being
discovered, not specific verticals. A restaurant and a hiking trail are
both `place_venue` because they are physical locations with categories
and contact/visit info. A startup and a university are both
`organization_company` because they are organizations with a focus area
and operational footprint.

This keeps the planner expressive enough for typed retrieval facets while
avoiding fragile generic plans such as entity_type="entity" with vague
columns, and without rewarding specific verticals.
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


# ── Structural entity-kind signals ────────────────────────────────────────────
#
# Keywords here are intentionally structural, not example-specific.
# Each group contains high-level nouns that imply an entity shape.
# The classifier uses them as soft hints combined with query structure
# (e.g. "in <location>" phrases for places).

_ORG_SIGNALS = (
    # organizational / legal-entity nouns
    "company", "companies", "corporation", "corporations", "firm", "firms",
    "agency", "agencies", "organization", "organizations", "org", "orgs",
    "startup", "startups", "nonprofit", "nonprofits", "foundation", "foundations",
    "institution", "institutions", "association", "associations", "society",
    "societies", "enterprise", "enterprises", "group", "groups", "team", "teams",
    "lab", "labs", "studio", "studios", "publisher", "publishers",
    # organizational activity terms that strongly imply org shape
    "vc", "venture", "investor", "investors", "funded", "funding", "series",
    "founded", "founders", "ipo", "acquired", "operator", "operators",
)

_PLACE_SIGNALS = (
    # physical-place nouns (broad, not vertical-tied)
    "place", "places", "venue", "venues", "location", "locations", "spot", "spots",
    "destination", "destinations", "site", "sites", "landmark", "landmarks",
    "park", "parks", "trail", "trails", "route", "routes", "beach", "beaches",
    "museum", "museums", "gallery", "galleries", "market", "markets",
    "shop", "shops", "store", "stores", "outlet", "outlets",
    "restaurant", "restaurants", "cafe", "cafes", "bar", "bars", "pub", "pubs",
    "bakery", "bakeries", "diner", "diners",
    "hotel", "hotels", "hostel", "hostels", "inn", "inns", "lodge", "lodges",
    "resort", "resorts", "motel", "motels",
    "stadium", "stadiums", "arena", "arenas", "theater", "theatres", "theaters",
    "clinic", "clinics", "hospital", "hospitals", "school", "schools",
    "library", "libraries",
)

_SOFTWARE_SIGNALS = (
    "software", "program", "programs", "application", "applications",
    "app", "apps", "tool", "tools", "toolkit", "toolkits",
    "platform", "platforms", "framework", "frameworks", "library", "libraries",
    "package", "packages", "sdk", "sdks", "api", "apis", "cli",
    "database", "databases", "repo", "repos", "repository", "repositories",
    "engine", "engines", "module", "modules", "plugin", "plugins",
    "extension", "extensions", "script", "scripts", "runtime", "runtimes",
    "compiler", "compilers", "interpreter", "interpreters",
    "open source", "open-source", "opensource", "foss",
    "devtool", "devtools", "developer",
)

_PRODUCT_SIGNALS = (
    "product", "products", "brand", "brands", "model", "models",
    "item", "items", "goods", "gadget", "gadgets", "device", "devices",
    "appliance", "appliances", "equipment", "gear",
)

_PERSON_SIGNALS = (
    "person", "people", "individual", "individuals",
    "author", "authors", "writer", "writers", "researcher", "researchers",
    "scientist", "scientists", "engineer", "engineers", "developer", "developers",
    "founder", "founders", "ceo", "executive", "executives",
    "artist", "artists", "designer", "designers", "musician", "musicians",
    "professor", "professors", "expert", "experts", "speaker", "speakers",
    "contributor", "contributors", "maintainer", "maintainers",
)

_LOCATION_PREPOSITIONS = (" in ", " near ", " around ", " at ", " from ")

_GENERIC_ENTITY_TYPES = {
    "entity", "entities", "item", "items", "thing", "things",
    "business", "businesses", "organization", "organizations",
    "company", "companies", "product", "products",
    "tool", "tools", "place", "places",
}

# ── Schema templates (structural, reusable across verticals) ─────────────────

_SCHEMA_TEMPLATES = {
    "organization_company": _SchemaTemplate(
        query_family="organization_company",
        entity_type="organization",
        columns=[
            "name", "website", "headquarters",
            "focus_area", "product_or_service", "stage_or_status",
        ],
    ),
    "place_venue": _SchemaTemplate(
        query_family="place_venue",
        entity_type="place",
        columns=[
            "name", "website", "location",
            "category", "offering", "contact_or_booking",
        ],
    ),
    "software_project": _SchemaTemplate(
        query_family="software_project",
        entity_type="software project",
        columns=[
            "name", "website_or_repo", "primary_use_case",
            "license", "language_or_stack", "maintainer_or_org",
        ],
    ),
    "product_offering": _SchemaTemplate(
        query_family="product_offering",
        entity_type="product",
        columns=[
            "name", "website", "category",
            "key_feature", "price_or_availability", "maker_or_brand",
        ],
    ),
    "person_group": _SchemaTemplate(
        query_family="person_group",
        entity_type="person",
        columns=[
            "name", "affiliation", "role_or_title",
            "notable_work", "location", "website_or_profile",
        ],
    ),
    "generic_entity_list": _SchemaTemplate(
        query_family="generic_entity_list",
        entity_type="entity",
        columns=["name", "website", "description", "category", "location"],
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
- Keep the entity_type specific when possible. Avoid generic words like "entity" or "item".
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


# ── Classification ────────────────────────────────────────────────────────────

def _contains_signal(query_lc: str, signals: tuple[str, ...]) -> bool:
    # Match signal tokens on word boundaries where reasonable.
    for token in signals:
        if " " in token:
            if token in query_lc:
                return True
            continue
        # Word-boundary match for single words.
        if re.search(rf"(?<![a-z]){re.escape(token)}(?![a-z])", query_lc):
            return True
    return False


def _has_location_phrase(query_lc: str) -> bool:
    return any(marker in query_lc for marker in _LOCATION_PREPOSITIONS)


def classify_query_family(query: str) -> str:
    """Classify a query into a structural entity-kind family.

    Order matters: stronger structural signals win over weaker ones.
    Software signals are checked before organization because many software
    queries also mention "open source" or "platform". Place signals combined
    with a location phrase win over generic org signals.
    """
    q = query.lower()

    is_software = _contains_signal(q, _SOFTWARE_SIGNALS)
    is_place = _contains_signal(q, _PLACE_SIGNALS)
    is_org = _contains_signal(q, _ORG_SIGNALS)
    is_product = _contains_signal(q, _PRODUCT_SIGNALS)
    is_person = _contains_signal(q, _PERSON_SIGNALS)
    has_location = _has_location_phrase(q)

    # Software wins over a stray "platform/tool" being read as org.
    if is_software and not is_org:
        return "software_project"

    # Person signals are rarer but strong when present.
    if is_person and not (is_org or is_software):
        return "person_group"

    # A concrete venue noun plus a location phrase is a strong place signal.
    if is_place and (has_location or not is_org):
        return "place_venue"

    # Organizational signals → organization_company.
    if is_org:
        return "organization_company"

    # Software without an overriding org signal.
    if is_software:
        return "software_project"

    # Product signals.
    if is_product:
        return "product_offering"

    # A location phrase alone still hints at a place_venue query.
    if has_location:
        return "place_venue"

    return "generic_entity_list"


# ── Entity-type derivation ────────────────────────────────────────────────────

_INTENT_PREFIX_RE = re.compile(
    r"^(top|best|leading|most\s+promising|highest\s+rated|top\s+rated|"
    r"popular|great|notable|recommended|must[- ]?visit)\s+",
    flags=re.IGNORECASE,
)

_TRAILING_LOCATION_RE = re.compile(
    r"\s+(in|near|around|at|from)\s+.+$",
    flags=re.IGNORECASE,
)

_YEAR_SUFFIX_RE = re.compile(r"\s+(?:in\s+)?(?:19|20)\d{2}s?\s*$", flags=re.IGNORECASE)


def _strip_leading_intent(query: str) -> str:
    return _INTENT_PREFIX_RE.sub("", query.strip())


def _derive_entity_type(query: str, family: str) -> str:
    """Derive a short entity-type noun from the query topic.

    Extract the core noun phrase by stripping intent prefixes ("top", "best"),
    trailing location clauses, and year suffixes. Fall back to the family
    default if nothing usable remains.
    """
    topic = _strip_leading_intent(query)
    topic = _YEAR_SUFFIX_RE.sub("", topic)
    topic = _TRAILING_LOCATION_RE.sub("", topic).strip()

    # Pick the last 1–3 meaningful tokens as the entity phrase.
    tokens = [t for t in re.split(r"\s+", topic) if t]
    if not tokens:
        return _SCHEMA_TEMPLATES[family].entity_type

    phrase = " ".join(tokens[-3:]) if len(tokens) >= 3 else " ".join(tokens)
    phrase = phrase.strip().lower()
    if not phrase or phrase in _GENERIC_ENTITY_TYPES:
        return _SCHEMA_TEMPLATES[family].entity_type

    # Singularize trivially: drop trailing 's' from last word if it's plural-like.
    parts = phrase.split()
    last = parts[-1]
    if len(last) > 3 and last.endswith("s") and not last.endswith("ss"):
        parts[-1] = last[:-1]
    return " ".join(parts) or _SCHEMA_TEMPLATES[family].entity_type


def _template_for_query(query: str, family: str | None = None) -> _SchemaTemplate:
    chosen_family = family or classify_query_family(query)
    template = _SCHEMA_TEMPLATES[chosen_family]
    return _SchemaTemplate(
        query_family=template.query_family,
        entity_type=_derive_entity_type(query, template.query_family),
        columns=list(template.columns),
    )


# ── Deterministic facets (structural, not example-specific) ───────────────────

def _deterministic_facets(
    query: str,
    query_family: str,
    columns: list[str],
) -> list[SearchFacet]:
    """Produce a general facet set parametrized by entity-kind family.

    Facets express retrieval intent (list, official, editorial, attribute,
    news, comparison) rather than vertical-specific phrasing.
    """
    topic = _strip_leading_intent(query) or query
    fill = [c for c in columns if c != "name"][:3]

    facets: list[SearchFacet] = [
        SearchFacet(
            type="entity_list",
            query=f"top {topic}",
            expected_fill_columns=["name"] + fill[:2],
            rationale="list/roundup pages maximize candidate recall",
        ),
        SearchFacet(
            type="official_source",
            query=f"{topic} official website",
            expected_fill_columns=[c for c in columns if c in {"website", "website_or_repo", "website_or_profile"}][:1] or ["name"],
            rationale="official/home pages give canonical entity facts",
        ),
        SearchFacet(
            type="editorial_review",
            query=f"{topic} overview",
            expected_fill_columns=["name"] + fill[:1],
            rationale="editorial overviews separate candidates from noise",
        ),
    ]

    # Family-level attribute facet — parametrized, not hardcoded phrasing.
    attribute_fill = [c for c in columns if c not in {"name", "website", "website_or_repo", "website_or_profile"}][:2]
    if attribute_fill:
        attribute_hint = attribute_fill[0].replace("_", " ")
        facets.append(
            SearchFacet(
                type="attribute_specific",
                query=f"{topic} {attribute_hint}",
                expected_fill_columns=attribute_fill,
                rationale=f"attribute-focused pages fill the {attribute_hint} column",
            )
        )

    # Comparison facet for software/product; news facet for orgs; none otherwise.
    if query_family in {"software_project", "product_offering"}:
        facets.append(
            SearchFacet(
                type="comparison",
                query=f"{topic} comparison",
                expected_fill_columns=["name"] + fill[:1],
                rationale="comparison pages differentiate similar candidates",
            )
        )
    elif query_family == "organization_company":
        facets.append(
            SearchFacet(
                type="news_recent",
                query=f"{topic} news",
                expected_fill_columns=["name"] + fill[:1],
                rationale="recent news surfaces active candidates",
            )
        )

    return facets[:5]


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


async def _llm_facets(
    query: str,
    template: _SchemaTemplate,
    stats: dict[str, int] | None = None,
) -> _FacetPlan:
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
        usage_stats=stats,
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


async def plan_schema(query: str, stats: dict[str, int] | None = None) -> PlannerOutput:
    """Return a stable, family-constrained schema plan for the given query."""
    family = classify_query_family(query)
    template = _template_for_query(query, family)
    log.info("Planning schema for query=%r family=%s", query, family)

    try:
        llm_result = await _llm_facets(query, template, stats=stats)
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
