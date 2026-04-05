"""Final row verification before returning ranked results."""

from __future__ import annotations

from urllib.parse import urlparse

from app.core.logging import get_logger
from app.models.schema import EntityRow, PlannerOutput
from app.services.field_validator import normalize_website
from app.services.ranker import is_row_obviously_bad
from app.services.source_quality import row_source_profile, row_source_quality
from app.utils.text import normalize_name
from app.utils.url import extract_domain

log = get_logger(__name__)

_HIGH_INTENT_TERMS = (
    "best",
    "top",
    "leading",
    "highest rated",
    "must visit",
    "must-visit",
)

_PSEUDO_ENTITY_TERMS = {
    "agents",
    "businesses",
    "companies",
    "copilots",
    "places",
    "platforms",
    "products",
    "providers",
    "restaurants",
    "services",
    "solutions",
    "startups",
    "tools",
}
_PSEUDO_ENTITY_FAMILY_TERMS = {
    "local_business": {"bars", "cafes", "coffee shops", "pizza places", "restaurants"},
    "software_tool": {"apps", "platforms", "tools"},
    "startup_company": {"agents", "copilots", "companies", "platforms", "startups", "tools"},
}
_PSEUDO_ENTITY_CONNECTORS = (" for ", " in ", " & ", "/")
_PSEUDO_SOURCE_PATH_HINTS = (
    "/article",
    "/articles",
    "/blog",
    "/blogs",
    "/categories",
    "/category",
    "/companies",
    "/directory",
    "/industry",
    "/list",
    "/lists",
    "/news",
    "/post",
    "/posts",
    "/report",
    "/reports",
)
_PSEUDO_SOURCE_TITLE_HINTS = ("category", "directory", "industry", "companies", "top ", "best ")


def _query_is_strict(query: str) -> bool:
    q = query.lower()
    return any(term in q for term in _HIGH_INTENT_TERMS)


def _count_actionable_fields(row: EntityRow) -> int:
    return len([col for col in row.cells if col not in {"name", "cuisine_type", "category", "description", "overview", "summary", "type"}])


def _looks_like_pseudo_entity_name(name: str, plan: PlannerOutput) -> bool:
    normalized = normalize_name(name)
    if not normalized:
        return False

    tokens = normalized.split()
    if len(tokens) < 3:
        return False

    terms = set(_PSEUDO_ENTITY_TERMS)
    terms.update(_PSEUDO_ENTITY_FAMILY_TERMS.get(plan.query_family, set()))
    generic_hits = sum(1 for term in terms if term in normalized)
    has_connector = any(connector in normalized for connector in _PSEUDO_ENTITY_CONNECTORS)
    return has_connector and generic_hits >= 2


def _website_points_back_to_non_entity_source(row: EntityRow) -> bool:
    website = row.cells.get("website")
    if not website:
        return True

    normalized, ok = normalize_website(
        website.value,
        source_url=website.source_url,
        source_title=website.source_title,
        canonical_domain=row.canonical_domain,
    )
    if not ok:
        return True

    website_domain = extract_domain(normalized)
    source_domain = extract_domain(website.source_url)
    if not (website_domain and source_domain and website_domain == source_domain):
        return False

    source_path = urlparse(website.source_url).path.lower()
    source_title = (website.source_title or "").lower()
    return any(hint in source_path for hint in _PSEUDO_SOURCE_PATH_HINTS) or any(
        hint in source_title for hint in _PSEUDO_SOURCE_TITLE_HINTS
    )


def _looks_like_pseudo_entity(row: EntityRow, plan: PlannerOutput, profile: dict[str, int], actionable_fields: int) -> bool:
    name_cell = row.cells.get("name")
    if not name_cell or row.canonical_domain:
        return False

    if actionable_fields > 1:
        return False

    if not _looks_like_pseudo_entity_name(name_cell.value, plan):
        return False

    return _website_points_back_to_non_entity_source(row) or row.sources_count < 3


def _verify_row(row: EntityRow, plan: PlannerOutput, query: str) -> tuple[bool, str]:
    if is_row_obviously_bad(row, plan):
        return False, "not_viable"

    strict_query = _query_is_strict(query)
    source_quality = row_source_quality(row)
    profile = row_source_profile(row)
    actionable_fields = _count_actionable_fields(row)

    if _looks_like_pseudo_entity(row, plan, profile, actionable_fields):
        return False, "pseudo_entity"

    marketplace_only = (
        profile["marketplace"] > 0
        and profile["official"] == 0
        and profile["editorial"] == 0
        and profile["directory"] == 0
        and profile["unknown"] == 0
    )

    if strict_query and marketplace_only:
        return False, "marketplace_only"

    # Keep sparse but plausible rows alive and let ranking sort them.
    # Only hard-reject when the row is both weak and unsupported.
    if source_quality < 0.2 and actionable_fields == 0 and row.sources_count < 2:
        return False, "low_quality_sparse"

    return True, "ok"


def verify_rows(
    rows: list[EntityRow],
    plan: PlannerOutput,
    query: str,
) -> list[EntityRow]:
    """
    Filter weak rows using source and evidence heuristics.
    Falls back to the original set if every row would be removed.
    """
    verified: list[EntityRow] = []
    rejected: list[tuple[str, str]] = []

    for row in rows:
        keep, reason = _verify_row(row, plan, query)
        if keep:
            verified.append(row)
        else:
            rejected.append((row.entity_id, reason))

    if verified:
        if rejected:
            preview = ", ".join(f"{entity_id}:{reason}" for entity_id, reason in rejected[:5])
            log.info("Verifier kept %d/%d rows; rejected=%s", len(verified), len(rows), preview)
        else:
            log.info("Verifier kept all %d rows", len(rows))
        return verified

    if rows:
        log.info("Verifier rejected all rows; falling back to original %d rows", len(rows))
    return rows
