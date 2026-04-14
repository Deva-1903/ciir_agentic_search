"""
Ranker: score and sort EntityRows.

The ranker remains a transparent weighted sum, but now includes a few
intent-aware features:
  - field importance by query family
  - local / geographic fit when the query implies a location
  - freshness when the query asks for recency
  - official-source preference by family
  - structured-source preference by family
  - lightweight reputation signals when available
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.schema import EntityRow, PlannerOutput, RankingSignal, RowRankingSummary
from app.services.source_quality import (
    row_evidence_regime_profile,
    row_source_profile,
    row_source_quality,
)
from app.utils.url import extract_domain

log = get_logger(__name__)

_WEIGHTS = {
    "completeness": 0.13,
    "field_importance": 0.10,
    "avg_confidence": 0.13,
    "source_support": 0.04,
    "actionable": 0.04,
    "source_quality": 0.16,
    "source_diversity": 0.05,
    "local_fit": 0.06,
    "freshness": 0.04,
    "reputation": 0.04,
    "official_fit": 0.04,
    "structured_fit": 0.02,
    "requirement_satisfaction": 0.15,
}

_COMPONENT_LABELS = {
    "completeness": "Schema coverage",
    "field_importance": "Important fields filled",
    "avg_confidence": "Cell confidence",
    "source_support": "Source support",
    "actionable": "Actionable detail",
    "source_quality": "Evidence quality",
    "source_diversity": "Source diversity",
    "local_fit": "Location fit",
    "freshness": "Freshness",
    "reputation": "Reputation",
    "official_fit": "Official-site fit",
    "structured_fit": "Structured-source fit",
    "requirement_satisfaction": "Requirement match",
}

_WEAK_SIGNAL_COLS = {
    "category",
    "description",
    "industry",
    "notes",
    "overview",
    "summary",
    "tagline",
    "tags",
    "type",
}

_GENERIC_NAME_TERMS = {
    "business",
    "company",
    "entity",
    "item",
    "organization",
    "place",
    "platform",
    "product",
    "service",
    "thing",
    "tool",
}

_FIELD_IMPORTANCE = {
    "organization_company": {
        "website": 0.28,
        "headquarters": 0.2,
        "focus_area": 0.18,
        "product_or_service": 0.2,
        "stage_or_status": 0.14,
    },
    "place_venue": {
        "website": 0.18,
        "location": 0.32,
        "category": 0.16,
        "offering": 0.14,
        "contact_or_booking": 0.2,
    },
    "software_project": {
        "website_or_repo": 0.3,
        "primary_use_case": 0.22,
        "license": 0.16,
        "language_or_stack": 0.14,
        "maintainer_or_org": 0.18,
    },
    "product_offering": {
        "website": 0.24,
        "category": 0.18,
        "key_feature": 0.24,
        "price_or_availability": 0.14,
        "maker_or_brand": 0.2,
    },
    "person_group": {
        "affiliation": 0.28,
        "role_or_title": 0.24,
        "notable_work": 0.2,
        "location": 0.14,
        "website_or_profile": 0.14,
    },
}

_LOCATION_RE = re.compile(r"\b(?:in|near|around|at|from)\s+([a-z0-9 ,.'-]+)$", re.IGNORECASE)
_FRESHNESS_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_FRESHNESS_HINTS = ("current", "fresh", "latest", "new", "recent", "today")


def _is_actionable_col(col: str) -> bool:
    normalized = col.lower()
    return normalized != "name" and normalized not in _WEAK_SIGNAL_COLS


def _normalized_row_name(row: EntityRow) -> str:
    name_cell = row.cells.get("name")
    if not name_cell:
        return ""
    return extract_domain(name_cell.value) or name_cell.value.strip().lower()


def _row_name_is_generic(row: EntityRow, plan: PlannerOutput) -> bool:
    name_cell = row.cells.get("name")
    if not name_cell:
        return True

    normalized_name = name_cell.value.strip().lower()
    normalized_entity_type = plan.entity_type.strip().lower()
    return normalized_name in _GENERIC_NAME_TERMS or normalized_name == normalized_entity_type


def _source_diversity(row: EntityRow) -> float:
    if not row.cells:
        return 0.0
    counts: dict[str, int] = {}
    for cell in row.cells.values():
        domain = extract_domain(cell.source_url) or "__unknown__"
        counts[domain] = counts.get(domain, 0) + 1
    total = sum(counts.values())
    max_share = max(counts.values())
    return round(1.0 - (max_share / total), 3)


def _get_weights() -> dict[str, float]:
    settings = get_settings()
    weights = dict(_WEIGHTS)
    weights["source_diversity"] = settings.source_diversity_weight
    return weights


def _field_importance_score(row: EntityRow, plan: PlannerOutput) -> float:
    configured = _FIELD_IMPORTANCE.get(plan.query_family)
    if not configured:
        non_name = [col for col in plan.columns if col != "name"]
        if not non_name:
            return 0.0
        filled = sum(1 for col in non_name if col in row.cells)
        return filled / len(non_name)
    return round(sum(weight for col, weight in configured.items() if col in row.cells), 3)


def _extract_location_phrase(query: str | None) -> str:
    if not query:
        return ""
    match = _LOCATION_RE.search(query.strip())
    return match.group(1).strip().lower() if match else ""


def _token_overlap(a: str, b: str) -> float:
    left = {token for token in re.findall(r"[a-z0-9]+", a.lower()) if len(token) > 1}
    right = {token for token in re.findall(r"[a-z0-9]+", b.lower()) if len(token) > 1}
    if not left or not right:
        return 0.0
    return len(left & right) / len(left)


def _local_fit(row: EntityRow, plan: PlannerOutput, query: str | None) -> float:
    if plan.query_family not in {"organization_company", "person_group", "place_venue"}:
        return 0.5

    location_phrase = _extract_location_phrase(query)
    if not location_phrase:
        return 0.5

    candidates = []
    for col in ("location", "address", "headquarters"):
        cell = row.cells.get(col)
        if cell:
            candidates.append(cell.value)

    if not candidates:
        return 0.2 if plan.query_family == "place_venue" else 0.4

    best = max(_token_overlap(location_phrase, candidate) for candidate in candidates)
    if best >= 0.8:
        return 1.0
    if best >= 0.5:
        return 0.8
    if best >= 0.25:
        return 0.55
    return 0.2


def _query_wants_freshness(query: str | None) -> bool:
    if not query:
        return False
    q = query.lower()
    return bool(_FRESHNESS_RE.search(q)) or any(hint in q for hint in _FRESHNESS_HINTS)


def _freshness_score(row: EntityRow, query: str | None) -> float:
    if not _query_wants_freshness(query):
        return 0.5

    current_year = datetime.now(timezone.utc).year
    years: list[int] = []
    for cell in row.cells.values():
        for source_text in (cell.source_url, cell.source_title or ""):
            for match in re.finditer(r"\b(19|20)\d{2}\b", source_text):
                try:
                    years.append(int(match.group(0)))
                except ValueError:
                    continue

    if not years:
        return 0.45

    freshest = max(years)
    if freshest >= current_year:
        return 1.0
    if freshest == current_year - 1:
        return 0.85
    if freshest >= current_year - 2:
        return 0.7
    return 0.45


def _reputation_score(row: EntityRow) -> float:
    if any(col in row.cells for col in ("rating", "score")):
        return 1.0
    profile = row_source_profile(row)
    if row.sources_count >= 3:
        return 0.85
    if profile["editorial"] >= 1 and row.sources_count >= 2:
        return 0.75
    return 0.5


def _official_fit(row: EntityRow, plan: PlannerOutput) -> float:
    profile = row_source_profile(row)
    regimes = row_evidence_regime_profile(row)

    if plan.query_family == "software_project":
        if regimes["software_repo_or_docs"] > 0 or profile["official"] > 0:
            return 1.0
        return 0.4

    if plan.query_family in {"organization_company", "place_venue", "product_offering", "person_group"}:
        if profile["official"] > 0 or row.canonical_domain:
            return 1.0
        if regimes["official_site"] > 0:
            return 0.78
        return 0.35

    return 0.5


def _structured_fit(row: EntityRow, plan: PlannerOutput) -> float:
    regimes = row_evidence_regime_profile(row)

    if plan.query_family == "software_project":
        return 1.0 if regimes["software_repo_or_docs"] > 0 else 0.4

    if plan.query_family == "place_venue":
        if regimes["local_business_listing"] > 0:
            return 1.0
        if regimes["directory_listing"] > 0:
            return 0.75
        return 0.4

    if plan.query_family in {"organization_company", "product_offering"}:
        if regimes["directory_listing"] > 0 or regimes["editorial_article"] > 0:
            return 0.8
        return 0.45

    return 0.5


def _requirement_score(row: EntityRow) -> float:
    """Score [0, 1] based on satisfied/unknown/not_satisfied counts. Unknown counts 0.5."""
    summ = row.requirement_summary
    if summ.requirements_total_count == 0:
        return 1.0
    sat = summ.requirements_satisfied_count
    unk = summ.requirements_unknown_count
    not_sat = summ.requirements_not_satisfied_count
    total = summ.requirements_total_count
    return round((sat * 1.0 + unk * 0.5 + not_sat * 0.0) / total, 3)


def _hard_requirement_penalty(row: EntityRow) -> float:
    """Subtract up to 0.3 from final score for clearly failed hard requirements."""
    failed_hard = sum(
        1 for m in row.requirement_summary.matches
        if m.status == "not_satisfied" and m.is_hard
    )
    return min(failed_hard * 0.1, 0.3)


def score_breakdown(row: EntityRow, plan: PlannerOutput, query: str | None = None) -> dict[str, float]:
    num_columns = max(len(plan.columns), 1)
    completeness = len(row.cells) / num_columns

    confs = [cell.confidence for cell in row.cells.values()]
    avg_confidence = sum(confs) / len(confs) if confs else 0.0

    source_support = math.log2(1 + row.sources_count) / math.log2(11)
    source_support = min(source_support, 1.0)

    breakdown = {
        "completeness": round(completeness, 3),
        "field_importance": round(_field_importance_score(row, plan), 3),
        "avg_confidence": round(avg_confidence, 3),
        "source_support": round(source_support, 3),
        "actionable": float(any(_is_actionable_col(col) for col in row.cells)),
        "source_quality": row_source_quality(row),
        "source_diversity": _source_diversity(row),
        "local_fit": round(_local_fit(row, plan, query), 3),
        "freshness": round(_freshness_score(row, query), 3),
        "reputation": round(_reputation_score(row), 3),
        "official_fit": round(_official_fit(row, plan), 3),
        "structured_fit": round(_structured_fit(row, plan), 3),
        "requirement_satisfaction": _requirement_score(row),
    }
    return breakdown


def _score(row: EntityRow, plan: PlannerOutput, query: str | None = None) -> float:
    breakdown = score_breakdown(row, plan, query)
    weights = _get_weights()
    base = sum(weights[key] * breakdown[key] for key in weights)
    penalty = _hard_requirement_penalty(row)
    return round(base - penalty, 4)


def ranking_summary(row: EntityRow, plan: PlannerOutput, query: str | None = None) -> RowRankingSummary:
    """Return a typed explanation of how the ranker scored this row."""
    breakdown = score_breakdown(row, plan, query)
    weights = _get_weights()
    components = [
        RankingSignal(
            key=key,
            label=_COMPONENT_LABELS.get(key, key.replace("_", " ").title()),
            value=round(breakdown[key], 3),
            weight=round(weights[key], 3),
            weighted_value=round(weights[key] * breakdown[key], 4),
        )
        for key in weights
    ]
    components.sort(key=lambda component: component.weighted_value, reverse=True)
    base = round(sum(component.weighted_value for component in components), 4)
    penalty = round(_hard_requirement_penalty(row), 4)
    return RowRankingSummary(
        base_score=base,
        final_score=round(base - penalty, 4),
        hard_requirement_penalty=penalty,
        components=components,
    )


def is_row_viable(row: EntityRow, plan: PlannerOutput) -> bool:
    if "name" not in row.cells:
        return False

    non_name_cols = [col for col in row.cells if col != "name"]
    if _row_name_is_generic(row, plan) and len(non_name_cols) < 2:
        return False

    if not non_name_cols:
        return row.sources_count >= 2 or bool(row.canonical_domain)

    if len(row.cells) >= 2:
        return True

    return any(_is_actionable_col(col) for col in non_name_cols)


def is_row_obviously_bad(row: EntityRow, plan: PlannerOutput) -> bool:
    if "name" not in row.cells:
        return True

    non_name_cols = [col for col in row.cells if col != "name"]
    if _row_name_is_generic(row, plan) and not any(_is_actionable_col(col) for col in non_name_cols):
        return True

    if not non_name_cols and row.sources_count < 2 and not row.canonical_domain:
        return True
    return False


def prune_rows(rows: list[EntityRow], plan: PlannerOutput) -> list[EntityRow]:
    pruned = [row for row in rows if not is_row_obviously_bad(row, plan)]
    if pruned:
        removed = len(rows) - len(pruned)
        if removed:
            log.info("Pruned %d obviously bad rows", removed)
        return pruned

    if rows:
        log.info("Skipped pruning because it would remove all %d rows", len(rows))
    return rows


def rank_rows(
    rows: list[EntityRow],
    plan: PlannerOutput,
    query: str | None = None,
) -> list[EntityRow]:
    scored: list[tuple[EntityRow, float]] = []
    for row in rows:
        row.ranking_summary = ranking_summary(row, plan, query=query)
        scored.append((row, row.ranking_summary.final_score))
    scored.sort(key=lambda item: item[1], reverse=True)
    for idx, (row, _) in enumerate(scored, start=1):
        row.ranking_summary.rank_position = idx
    result = [row for row, _ in scored]
    log.info("Ranked %d rows", len(result))
    return result


def find_sparse_rows(
    rows: list[EntityRow],
    plan: PlannerOutput,
    top_n: int = 3,
    query: str | None = None,
) -> list[EntityRow]:
    candidates = [row for row in rows if "name" in row.cells and not is_row_obviously_bad(row, plan)]

    def _missing(row: EntityRow) -> int:
        return len(plan.columns) - len(row.cells)

    candidates.sort(key=lambda row: (_missing(row), _score(row, plan, query=query)), reverse=True)
    return candidates[:top_n]
