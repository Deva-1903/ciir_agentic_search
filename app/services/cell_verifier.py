"""
Cell-level entity-alignment verification.

Row-level verification answers "is this row worth keeping at all". It cannot
catch the case where a row for entity A absorbs a cell whose evidence actually
describes entity B (the "Espresso Pizzeria got F&F Pizzeria's phone" failure
mode from Iteration 7).

This module does cheap rule-based checks on each non-name cell:

  1. Does the evidence snippet reference the target entity name
     (exact or fuzzy) at a reasonable threshold?
  2. Failing that, does the source title or URL path clearly identify
     the target entity?
  3. Failing that, is the cell coming from the entity's own declared
     website domain (official source — automatically aligned)?

If none hold, the cell is considered weakly aligned. We do NOT hard-delete
weakly aligned cells; instead we penalize their confidence. This preserves
borderline correct extractions while letting the ranker deprioritize them.
"""

from __future__ import annotations

from rapidfuzz import fuzz

from app.core.logging import get_logger
from app.models.schema import Cell, EntityRow
from app.utils.dedupe import domains_match
from app.utils.text import normalize_name

log = get_logger(__name__)

# Columns we never try to re-verify — the name cell defines the target entity,
# weak-signal fields are short and rarely contain the entity name.
_SKIP_COLS = {"name", "cuisine_type", "category", "type", "description", "overview", "summary"}

# Columns that typically hold the entity's own URL (used as an "official source"
# signal). If a cell's source_url shares domain with one of these, we consider
# the cell aligned by provenance.
_WEBSITE_COLS = {"website", "url", "official_website", "homepage", "link"}

# Fuzzy threshold on normalized entity-name tokens appearing in evidence / title.
_ALIGN_FUZZ_THRESHOLD = 80

# Penalty multiplier for cells that fail name alignment but are kept anyway.
_WEAK_ALIGN_PENALTY = 0.6


def _entity_name(row: EntityRow) -> str:
    name_cell = row.cells.get("name")
    return name_cell.value if name_cell else ""


def _official_site(row: EntityRow) -> str | None:
    for col in _WEBSITE_COLS:
        cell = row.cells.get(col)
        if cell and cell.value:
            return cell.value
    return None


def _text_mentions_name(text: str, normalized_name: str) -> bool:
    """Return True if *text* plausibly references the entity name."""
    if not text or not normalized_name:
        return False
    t = text.lower()
    # Exact substring (after normalization) is the strongest signal.
    if normalized_name in normalize_name(text):
        return True
    # Short names can false-match; require full-name fuzzy threshold.
    score = fuzz.partial_ratio(normalized_name, t)
    return score >= _ALIGN_FUZZ_THRESHOLD


def _cell_is_aligned(
    cell: Cell,
    entity_name: str,
    official_site: str | None,
) -> bool:
    normalized_name = normalize_name(entity_name)
    if not normalized_name:
        return True  # can't verify without a name; don't punish

    # Rule 1: evidence snippet mentions the entity name.
    if _text_mentions_name(cell.evidence_snippet, normalized_name):
        return True
    # Rule 2: source title mentions the entity name.
    if cell.source_title and _text_mentions_name(cell.source_title, normalized_name):
        return True
    # Rule 3: cell comes from the entity's own website domain.
    if official_site and domains_match(cell.source_url, official_site):
        return True
    return False


def verify_row_cells(row: EntityRow) -> dict:
    """
    Apply cell-level alignment checks to a single row in-place.
    Weakly-aligned cells have their confidence multiplied by _WEAK_ALIGN_PENALTY.
    Returns a small stats dict for observability.
    """
    entity_name = _entity_name(row)
    if not entity_name:
        return {"penalized": 0, "checked": 0}

    official_site = _official_site(row)
    penalized = 0
    checked = 0

    for col, cell in list(row.cells.items()):
        if col.lower() in _SKIP_COLS:
            continue
        checked += 1
        if _cell_is_aligned(cell, entity_name, official_site):
            continue
        # Weakly aligned — drop confidence, keep value + provenance visible.
        new_conf = round(cell.confidence * _WEAK_ALIGN_PENALTY, 3)
        row.cells[col] = cell.model_copy(update={"confidence": new_conf})
        penalized += 1

    if penalized:
        # Recompute row aggregate confidence.
        confs = [c.confidence for c in row.cells.values()]
        if confs:
            row.aggregate_confidence = round(sum(confs) / len(confs), 3)

    return {"penalized": penalized, "checked": checked}


def verify_rows_cells(rows: list[EntityRow]) -> list[EntityRow]:
    """Apply cell-level alignment checks to every row."""
    total_penalized = 0
    total_checked = 0
    for row in rows:
        stats = verify_row_cells(row)
        total_penalized += stats["penalized"]
        total_checked += stats["checked"]
    if total_checked:
        log.info(
            "Cell verifier: penalized %d/%d non-name cells for weak name alignment",
            total_penalized,
            total_checked,
        )
    return rows
