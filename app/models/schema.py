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
    "organization_company",
    "place_venue",
    "software_project",
    "product_offering",
    "person_group",
    "generic_entity_list",
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
    query_family: str = "generic_entity_list"
    entity_type: str
    columns: List[str]                 # always starts with "name"; max 8
    search_angles: List[str] = Field(default_factory=list)  # derived from facets post-validation
    facets: List[SearchFacet] = Field(default_factory=list)

    @field_validator("query_family")
    @classmethod
    def _normalize_query_family(cls, v: str) -> str:
        v = (v or "").strip().lower().replace("-", "_").replace(" ", "_")
        return v if v in _CANONICAL_QUERY_FAMILIES else "generic_entity_list"


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
    raw_html: Optional[str] = None
    page_metadata: Dict[str, Any] = Field(default_factory=dict)
    evidence_regime: str = "unknown"
    regime_confidence: float = 0.0
    fetch_method: str = "static"


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


class RequirementSpec(BaseModel):
    """A structured requirement parsed from the free-text query — richer replacement for QueryRequirement."""
    id: str                           # short slug, e.g. "loc_0", "fund_1"
    label: str                        # human-readable, e.g. "Location: US"
    kind: str                         # categorical | location | numeric | semantic
    operator: str                     # equals | contains | greater_than | less_than | at_least | exists | matches_topic
    target_value: Optional[str] = None       # normalised value, e.g. "us", "10M", "startup"
    target_value_raw: Optional[str] = None   # raw as seen in query, e.g. "United States", "Series B"
    source_phrase: str                # exact substring of the query this came from
    priority: str = "medium"          # high | medium
    is_hard: bool = False             # if True, failure applies ranker penalty
    mapped_columns: List[str] = Field(default_factory=list)   # schema columns to check
    notes: Optional[str] = None


class RequirementEvidence(BaseModel):
    """Grounding for a requirement judgment."""
    source_url: Optional[str] = None
    source_title: Optional[str] = None
    evidence_snippet: Optional[str] = None


class RequirementMatch(BaseModel):
    """Per-row evaluation result for one RequirementSpec."""
    requirement_id: str
    label: str
    status: str                        # satisfied | not_satisfied | unknown
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    matched_value: Optional[str] = None       # value found in the row cell
    matched_column: Optional[str] = None      # which column name matched
    reason: Optional[str] = None              # short human-readable explanation
    evidence: Optional[RequirementEvidence] = None
    score_contribution: Optional[float] = None
    is_hard: bool = False


class RowRequirementsSummary(BaseModel):
    """Aggregated requirement evaluation for a single EntityRow."""
    requirements_total_count: int = 0
    requirements_satisfied_count: int = 0
    requirements_not_satisfied_count: int = 0
    requirements_unknown_count: int = 0
    satisfaction_ratio: float = 0.0   # satisfied / total requirements
    hard_requirements_satisfied_count: int = 0
    matches: List[RequirementMatch] = Field(default_factory=list)


class RankingSignal(BaseModel):
    """One transparent component of the final row ranking."""
    key: str
    label: str
    value: float = 0.0
    weight: float = 0.0
    weighted_value: float = 0.0


class RowRankingSummary(BaseModel):
    """Final ranking explanation attached to a row."""
    rank_position: Optional[int] = None
    base_score: float = 0.0
    final_score: float = 0.0
    hard_requirement_penalty: float = 0.0
    components: List[RankingSignal] = Field(default_factory=list)


class EntityRow(BaseModel):
    entity_id: str
    cells: Dict[str, Cell]          # column_name → Cell
    aggregate_confidence: float
    sources_count: int
    canonical_domain: Optional[str] = None
    requirement_summary: RowRequirementsSummary = Field(default_factory=RowRequirementsSummary)
    ranking_summary: RowRankingSummary = Field(default_factory=RowRankingSummary)


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
    pipeline_timings_ms: Dict[str, float] = Field(default_factory=dict)
    requirements: List[RequirementSpec] = Field(default_factory=list)
    requirements_parsed: int = 0


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
