"""
Entity merger: collapse EntityDrafts from many pages into canonical EntityRows.

Strategy:
  1. Normalize entity name + look for website/domain overlap.
  2. Use rapidfuzz token_set_ratio for fuzzy name matching.
  3. For each column, keep the cell with the highest confidence score.
  4. Track how many distinct source URLs contributed evidence.
"""

from __future__ import annotations

import re
import uuid
from typing import Optional

from app.core.logging import get_logger
from app.models.schema import Cell, EntityDraft, EntityRow, PlannerOutput
from app.utils.dedupe import find_matching_entity_idx
from app.utils.text import normalize_name
from app.utils.url import extract_domain

log = get_logger(__name__)


def _slug(name: str) -> str:
    """Create a stable, URL-safe entity ID from a name."""
    s = normalize_name(name)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^a-z0-9\-]", "", s)
    return s or str(uuid.uuid4())[:8]


def _pick_better_cell(existing: Cell, candidate: Cell) -> Cell:
    """Return whichever cell has stronger evidence."""
    # Prefer higher confidence
    if candidate.confidence > existing.confidence + 0.05:
        return candidate
    # Tie-break: prefer longer evidence snippet (more informative)
    if candidate.confidence >= existing.confidence - 0.05:
        if len(candidate.evidence_snippet) > len(existing.evidence_snippet):
            return candidate
    return existing


def _website_from_cells(cells: dict[str, Cell]) -> Optional[str]:
    """Extract website URL from cells dict if present."""
    for key in ("website", "url", "official_website", "homepage"):
        if key in cells:
            return cells[key].value
    return None


def _draft_website(draft: EntityDraft) -> Optional[str]:
    """Get website value from a draft's cells."""
    for key in ("website", "url", "official_website", "homepage"):
        if key in draft.cells:
            return draft.cells[key].value
    return None


# ── Main merge ────────────────────────────────────────────────────────────────

class _MergeState:
    """Mutable accumulator for one canonical entity during merging."""

    def __init__(self, draft: EntityDraft) -> None:
        self.canonical_name: str = draft.entity_name
        self.cells: dict[str, Cell] = {
            col: Cell(
                value=cd.value,
                source_url=draft.source_url,
                source_title=draft.source_title,
                evidence_snippet=cd.evidence_snippet,
                confidence=cd.confidence,
            )
            for col, cd in draft.cells.items()
        }
        self.source_urls: set[str] = {draft.source_url}

    def absorb(self, draft: EntityDraft) -> None:
        """Merge a new draft into this canonical entity."""
        self.source_urls.add(draft.source_url)

        for col, new_cd in draft.cells.items():
            new_cell = Cell(
                value=new_cd.value,
                source_url=draft.source_url,
                source_title=draft.source_title,
                evidence_snippet=new_cd.evidence_snippet,
                confidence=new_cd.confidence,
            )
            if col not in self.cells:
                self.cells[col] = new_cell
            else:
                self.cells[col] = _pick_better_cell(self.cells[col], new_cell)

    def to_entity_row(self, columns: list[str]) -> EntityRow:
        confs = [c.confidence for c in self.cells.values()]
        agg_conf = round(sum(confs) / len(confs), 3) if confs else 0.0

        # Reorder cells to match schema column order
        ordered: dict[str, Cell] = {}
        for col in columns:
            if col in self.cells:
                ordered[col] = self.cells[col]
        # Append any extra cells not in schema (shouldn't normally happen)
        for col, cell in self.cells.items():
            if col not in ordered:
                ordered[col] = cell

        return EntityRow(
            entity_id=_slug(self.canonical_name),
            cells=ordered,
            aggregate_confidence=agg_conf,
            sources_count=len(self.source_urls),
        )

    @property
    def _website(self) -> Optional[str]:
        return _website_from_cells(self.cells)

    @property
    def _name_str(self) -> str:
        return self.cells.get("name", Cell(
            value=self.canonical_name,
            source_url="",
            evidence_snippet="",
            confidence=0.0,
        )).value


def merge_entities(drafts: list[EntityDraft], plan: PlannerOutput) -> list[EntityRow]:
    """
    Merge all EntityDrafts into canonical EntityRows.
    Returns unsorted rows (sorting is done by ranker.py).
    """
    states: list[_MergeState] = []
    lookup: list[dict] = []  # parallel list for dedupe matching

    for draft in drafts:
        draft_website = _draft_website(draft)

        idx = find_matching_entity_idx(
            draft.entity_name,
            draft_website,
            lookup,
        )

        if idx is not None:
            states[idx].absorb(draft)
            lookup[idx] = {
                "name": states[idx]._name_str,
                "website": states[idx]._website,
            }
        else:
            states.append(_MergeState(draft))
            lookup.append({
                "name": states[-1]._name_str,
                "website": states[-1]._website,
            })

    rows = [s.to_entity_row(plan.columns) for s in states]
    log.info("Merged %d drafts → %d canonical entities", len(drafts), len(rows))
    return rows
