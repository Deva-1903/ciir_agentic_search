"""Pydantic models for the full pipeline: request, response, and internal types."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator


# ── Request ───────────────────────────────────────────────────────────────────

class SearchRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=500)


# ── Planner output ─────────────────────────────────────────────────────────────

class PlannerOutput(BaseModel):
    entity_type: str
    columns: List[str]          # always starts with "name"; max 8
    search_angles: List[str]    # max 5


# ── Brave search ───────────────────────────────────────────────────────────────

class BraveResult(BaseModel):
    url: str
    title: str
    snippet: Optional[str] = None


# ── Scraper ────────────────────────────────────────────────────────────────────

class ScrapedPage(BaseModel):
    url: str
    title: str
    cleaned_text: str
    from_cache: bool = False


# ── Extractor (per-page, pre-merge) ───────────────────────────────────────────

class CellDraft(BaseModel):
    """A single extracted cell, before merge."""
    value: str
    evidence_snippet: str
    confidence: float = Field(ge=0.0, le=1.0)


class EntityDraft(BaseModel):
    """One entity extracted from a single page."""
    entity_name: str
    cells: Dict[str, CellDraft]  # column_name → cell
    source_url: str
    source_title: Optional[str] = None


class ExtractionResult(BaseModel):
    """All entities extracted from a single page."""
    entities: List[EntityDraft]


# ── Merger / final output ──────────────────────────────────────────────────────

class Cell(BaseModel):
    """A final, provenance-attached cell in the result table."""
    value: str
    source_url: str
    source_title: Optional[str] = None
    evidence_snippet: str
    confidence: float = Field(ge=0.0, le=1.0)


class EntityRow(BaseModel):
    entity_id: str
    cells: Dict[str, Cell]          # column_name → Cell
    aggregate_confidence: float
    sources_count: int


class SearchMetadata(BaseModel):
    search_angles: List[str]
    urls_considered: int
    pages_scraped: int
    entities_extracted: int
    entities_after_merge: int
    gap_fill_used: bool
    duration_seconds: float


class SearchResponse(BaseModel):
    query_id: str
    query: str
    entity_type: str
    columns: List[str]
    rows: List[EntityRow]
    metadata: SearchMetadata


# ── Job status (for async polling) ────────────────────────────────────────────

class JobStatus(BaseModel):
    job_id: str
    status: str          # pending | running | done | failed
    phase: Optional[str] = None
    result: Optional[SearchResponse] = None
    error: Optional[str] = None


# ── Export helpers ─────────────────────────────────────────────────────────────

class ExportRequest(BaseModel):
    query_id: str
