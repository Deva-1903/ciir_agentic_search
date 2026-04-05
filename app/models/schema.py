"""Pydantic models for the full pipeline: request, response, and internal types."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator


# ── Request ───────────────────────────────────────────────────────────────────

class SearchRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=500)


# ── Planner output ─────────────────────────────────────────────────────────────

# Allowed facet types. Kept open-ended (str) but the planner is prompted to use
# these canonical values so downstream code can reason about retrieval intent.
#   entity_list       — broad list/overview pages that surface candidates
#   official_source   — official homepages / about pages (high trust)
#   editorial_review  — curated editorial / review articles
#   attribute_specific— query targeting a particular column (funding, rating, …)
#   news_recent       — recent news / announcements
#   comparison        — comparative or "X vs Y" articles
#   other             — fallback for anything that doesn't fit
_CANONICAL_FACET_TYPES = {
    "entity_list",
    "official_source",
    "editorial_review",
    "attribute_specific",
    "news_recent",
    "comparison",
    "other",
}

_CANONICAL_QUERY_FAMILIES = {
    "local_business",
    "startup_company",
    "software_tool",
    "product_category",
    "organization",
    "fallback_generic",
}


class SearchFacet(BaseModel):
    """A typed retrieval facet. Each facet expresses one clear search intent."""
    type: str                                  # see _CANONICAL_FACET_TYPES
    query: str                                 # natural search-engine query
    expected_fill_columns: List[str] = Field(default_factory=list)
    rationale: str = ""

    @field_validator("type")
    @classmethod
    def _normalize_type(cls, v: str) -> str:
        v = (v or "").strip().lower().replace("-", "_").replace(" ", "_")
        return v if v in _CANONICAL_FACET_TYPES else "other"


class PlannerOutput(BaseModel):
    query_family: str = "fallback_generic"
    entity_type: str
    columns: List[str]                 # always starts with "name"; max 8
    search_angles: List[str] = Field(default_factory=list)  # derived from facets post-validation
    facets: List[SearchFacet] = Field(default_factory=list)

    @field_validator("query_family")
    @classmethod
    def _normalize_query_family(cls, v: str) -> str:
        v = (v or "").strip().lower().replace("-", "_").replace(" ", "_")
        return v if v in _CANONICAL_QUERY_FAMILIES else "fallback_generic"


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
    canonical_domain: Optional[str] = None


class SearchMetadata(BaseModel):
    original_query: Optional[str] = None
    normalized_query: Optional[str] = None
    query_family: Optional[str] = None
    search_angles: List[str]
    facets: List[SearchFacet] = Field(default_factory=list)
    urls_considered: int
    pages_scraped: int
    pages_after_rerank: Optional[int] = None
    rerank_scorer: Optional[str] = None
    entities_extracted: int
    entities_after_merge: int
    gap_fill_used: bool
    duration_seconds: float
    pipeline_counts: Dict[str, int] = Field(default_factory=dict)


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
