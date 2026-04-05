"""Final row verification before returning ranked results."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from app.core.config import get_settings
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

# ── Name-shape rejection patterns ─────────────────────────────────────────────

# Entity names that are clearly CTA or navigation text, not real entities.
# Matches phrases like "Order Online", "Book Now", "Sign Up Free", "Learn More".
#
# Design: be explicit about allowed verb + suffix combinations rather than
# using a generic `(?:\s+\w+)*` suffix, which would match proper nouns like
# "Order of the Phoenix" or "Book Club Cafe".
_CTA_SUFFIXES = (
    r"(?:\s+(?:now|free|online|here|today|us|all|more|it|them|"
    r"your|yours|in|out|up|started|forward|available|a\s+table|"
    r"a\s+demo|a\s+quote|a\s+seat|directions?|menu|details?|"
    r"the\s+(?:app|menu|pdf|guide)|for\s+free))?"
)
_CTA_PATTERN = re.compile(
    r"^(?:"
    r"order" + _CTA_SUFFIXES + r"|"
    r"buy" + _CTA_SUFFIXES + r"|"
    r"book" + _CTA_SUFFIXES + r"|"
    r"sign\s+up" + _CTA_SUFFIXES + r"|"
    r"get\s+started" + _CTA_SUFFIXES + r"|"
    r"learn\s+more" + _CTA_SUFFIXES + r"|"
    r"read\s+more" + _CTA_SUFFIXES + r"|"
    r"view\s+(?:all|more|menu|details?|directions?)" + r"|"
    r"see\s+(?:all|more|menu|details?)" + r"|"
    r"click\s+here|"
    r"contact\s+us|"
    r"call\s+(?:us|now)" + _CTA_SUFFIXES + r"|"
    r"reserve\s+(?:a\s+)?(?:table|seat|spot|now|online)|"
    r"explore\s+(?:more|now|all)|"
    r"watch\s+(?:now|the\s+video)|"
    r"download" + _CTA_SUFFIXES + r"|"
    r"subscribe" + _CTA_SUFFIXES + r"|"
    r"add\s+to\s+(?:cart|bag|wishlist)|"
    r"shop\s+(?:now|all)|"
    r"find\s+(?:out\s+more|us\s+on)|"
    r"access" + _CTA_SUFFIXES + r"|"
    r"visit\s+us|"
    r"check\s+(?:out|availability)|"
    r"browse\s+(?:all|more)|"
    r"try\s+(?:it\s+)?(?:free|now)|"
    r"get\s+(?:it|them|access|yours|directions?|the\s+app)|"
    r"apply\s+(?:now|online)|"
    r"register" + _CTA_SUFFIXES + r"|"
    r"see\s+directions?"
    r")$",
    re.IGNORECASE,
)

# Article-title patterns: "Best X in Y", "Top 10 X", "The 7 Best X", etc.
# These are list-article headings, not entity names.
_ARTICLE_TITLE_PATTERN = re.compile(
    r"^(?:the\s+)?(?:\d+\s+)?(?:best|top|worst|greatest|biggest|leading|most\s+\w+|"
    r"popular|famous|notable|must[-\s](?:try|visit|see|have)|highest[-\s]rated|"
    r"award[-\s]winning)\s+\w",
    re.IGNORECASE,
)

# Operational / hours / price strings that are definitely not entity names.
_OPERATIONAL_PATTERN = re.compile(
    r"^(?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*[\s,\-]+|"
    r"open\s+(?:daily|now|until|monday)|closed\s+(?:on|monday)|"
    r"hours?:|free\s+(?:delivery|shipping|parking)|"
    r"\$\d[\d.,]*(?:\s*[-–]\s*\$\d[\d.,]*)?$)",
    re.IGNORECASE,
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
    "organization_company": {"companies", "firms", "organizations", "providers", "services", "solutions", "startups"},
    "place_venue": {"bars", "cafes", "destinations", "places", "restaurants", "shops", "venues"},
    "software_project": {"apps", "frameworks", "libraries", "platforms", "projects", "repositories", "tools"},
    "product_offering": {"brands", "devices", "items", "offerings", "products"},
    "person_group": {"authors", "experts", "people", "researchers", "speakers"},
    "generic_entity_list": {"entities", "items", "things"},
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
    return len([col for col in row.cells if col not in {"name", "category", "description", "overview", "summary", "type"}])


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


def _looks_like_cta_or_operational(name: str) -> bool:
    """True when the entity name is clearly a CTA phrase or operational string."""
    stripped = name.strip()
    if not stripped:
        return False
    if _CTA_PATTERN.match(stripped):
        return True
    # Operational strings are typically short (≤6 words).
    if len(stripped.split()) <= 6 and _OPERATIONAL_PATTERN.match(stripped):
        return True
    return False


def _looks_like_article_title(name: str) -> bool:
    """True when the entity name matches a list-article heading pattern.

    Two-word and three-word names are excluded even if they start with
    superlative words, because real brands like "Best Buy" or venues like
    "Top Hat Lounge" legitimately start with those words.  Article titles
    are longer (≥4 words) or contain a number ("Top 10 ...").
    """
    stripped = name.strip()
    word_count = len(stripped.split())
    # Short names starting with superlatives are usually proper nouns, not
    # article titles.  Only flag if the name is long enough or has a number.
    if word_count < 4 and not re.search(r"\d", stripped):
        return False
    return bool(_ARTICLE_TITLE_PATTERN.match(stripped))


def _verify_row(row: EntityRow, plan: PlannerOutput, query: str) -> tuple[bool, str]:
    if is_row_obviously_bad(row, plan):
        return False, "not_viable"

    name_cell = row.cells.get("name")
    name_value = name_cell.value.strip() if name_cell else ""

    if name_value and _looks_like_cta_or_operational(name_value):
        return False, "cta_text"

    if name_value and _looks_like_article_title(name_value):
        return False, "article_title"

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
    Applies a final row cap to prevent bloated result tables.
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
    else:
        if rows:
            log.info("Verifier rejected all rows; falling back to original %d rows", len(rows))
        verified = list(rows)

    # Apply final row cap.  Strict ("best/top") queries get a tighter cap.
    settings = get_settings()
    strict_query = _query_is_strict(query)
    cap = settings.max_strict_query_rows if strict_query else settings.max_final_rows
    if len(verified) > cap:
        log.info("Capping final rows: %d → %d (strict_query=%s)", len(verified), cap, strict_query)
        verified = verified[:cap]

    return verified
