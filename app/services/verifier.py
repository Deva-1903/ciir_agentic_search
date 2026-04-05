"""Final row verification before returning ranked results."""

from __future__ import annotations

from app.core.logging import get_logger
from app.models.schema import EntityRow, PlannerOutput
from app.services.ranker import is_row_obviously_bad
from app.services.source_quality import row_source_profile, row_source_quality

log = get_logger(__name__)

_HIGH_INTENT_TERMS = (
    "best",
    "top",
    "leading",
    "highest rated",
    "must visit",
    "must-visit",
)


def _query_is_strict(query: str) -> bool:
    q = query.lower()
    return any(term in q for term in _HIGH_INTENT_TERMS)


def _count_actionable_fields(row: EntityRow) -> int:
    return len([col for col in row.cells if col not in {"name", "cuisine_type", "category", "description", "overview", "summary", "type"}])


def _verify_row(row: EntityRow, plan: PlannerOutput, query: str) -> tuple[bool, str]:
    if is_row_obviously_bad(row, plan):
        return False, "not_viable"

    strict_query = _query_is_strict(query)
    source_quality = row_source_quality(row)
    profile = row_source_profile(row)
    actionable_fields = _count_actionable_fields(row)

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
