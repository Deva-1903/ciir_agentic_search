"""
Ranker: score and sort EntityRows.

Score components (all normalised to [0,1]):
  - completeness   : fraction of schema columns that have a cell value
  - avg_confidence : mean confidence across all cells
  - source_support : log-scaled number of source URLs (more = better)
  - actionable     : bonus for having at least one actionable non-name field
  - source_quality : heuristic evidence quality of contributing sources

Final score = weighted sum. Simple, explainable, no magic.
"""

from __future__ import annotations

import math

from app.core.logging import get_logger
from app.models.schema import EntityRow, PlannerOutput
from app.services.source_quality import row_source_quality
from app.utils.url import extract_domain
from app.core.config import get_settings

log = get_logger(__name__)

_WEIGHTS = {
    "completeness": 0.25,
    "avg_confidence": 0.20,
    "source_support": 0.08,
    "actionable": 0.07,
    "source_quality": 0.32,
    "source_diversity": 0.08,
}

_WEAK_SIGNAL_COLS = {
    "category",
    "cuisine_type",
    "description",
    "industry",
    "overview",
    "summary",
    "type",
}


def _is_actionable_col(col: str) -> bool:
    normalized = col.lower()
    return normalized != "name" and normalized not in _WEAK_SIGNAL_COLS


def _source_diversity(row: EntityRow) -> float:
    """
    Fraction of cells NOT contributed by the single most-dominant domain.
    1.0 = every cell from a different domain; 0.0 = all cells from one domain.
    """
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
    """Return weights with source_diversity from config (supports ablation)."""
    settings = get_settings()
    w = dict(_WEIGHTS)
    w["source_diversity"] = settings.source_diversity_weight
    return w


def _score(row: EntityRow, num_columns: int) -> float:
    completeness = len(row.cells) / max(num_columns, 1)

    confs = [c.confidence for c in row.cells.values()]
    avg_conf = sum(confs) / len(confs) if confs else 0.0

    # log2(1 + sources) / log2(1 + 10) normalised against 10 sources max
    source_support = math.log2(1 + row.sources_count) / math.log2(11)
    source_support = min(source_support, 1.0)

    actionable = float(
        any(_is_actionable_col(col) for col in row.cells)
    )
    source_quality = row_source_quality(row)
    source_diversity = _source_diversity(row)

    w = _get_weights()
    return (
        w["completeness"] * completeness
        + w["avg_confidence"] * avg_conf
        + w["source_support"] * source_support
        + w["actionable"] * actionable
        + w["source_quality"] * source_quality
        + w["source_diversity"] * source_diversity
    )


def is_row_viable(row: EntityRow, plan: PlannerOutput) -> bool:
    """Return True if a row has enough grounded detail to be useful."""
    if "name" not in row.cells:
        return False

    non_name_cols = [col for col in row.cells if col != "name"]
    if not non_name_cols:
        return False

    if len(row.cells) >= 3:
        return True

    return any(_is_actionable_col(col) for col in non_name_cols)


def prune_rows(rows: list[EntityRow], plan: PlannerOutput) -> list[EntityRow]:
    """
    Drop low-information rows when possible.
    Falls back to the original set if pruning would remove everything.
    """
    pruned = [row for row in rows if is_row_viable(row, plan)]
    if pruned:
        removed = len(rows) - len(pruned)
        if removed:
            log.info("Pruned %d low-information rows", removed)
        return pruned

    if rows:
        log.info("Skipped pruning because it would remove all %d rows", len(rows))
    return rows


def rank_rows(rows: list[EntityRow], plan: PlannerOutput) -> list[EntityRow]:
    """Return rows sorted by score descending."""
    num_cols = len(plan.columns)
    scored = [(row, _score(row, num_cols)) for row in rows]
    scored.sort(key=lambda x: x[1], reverse=True)
    result = [row for row, _ in scored]
    log.info("Ranked %d rows", len(result))
    return result


def find_sparse_rows(
    rows: list[EntityRow],
    plan: PlannerOutput,
    top_n: int = 3,
) -> list[EntityRow]:
    """
    Return up to top_n rows with the most missing columns.
    Used by gap_fill.py to decide which entities to enrich.
    Only considers rows that already have a 'name' cell.
    """
    num_cols = len(plan.columns)
    candidates = [r for r in rows if is_row_viable(r, plan)]

    def _missing(row: EntityRow) -> int:
        return num_cols - len(row.cells)

    candidates.sort(key=lambda row: (_missing(row), _score(row, num_cols)), reverse=True)
    return candidates[:top_n]
