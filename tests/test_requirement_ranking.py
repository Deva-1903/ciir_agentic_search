"""Tests for requirement-aware ranking integration."""

from app.models.schema import Cell, EntityRow, PlannerOutput, RequirementMatch, RowRequirementsSummary
from app.services.ranker import rank_rows


def _cell(value: str, url: str, *, title: str = "Source", confidence: float = 0.9) -> Cell:
    return Cell(
        value=value,
        source_url=url,
        source_title=title,
        evidence_snippet=value,
        confidence=confidence,
    )


def _summary(total: int, satisfied: int, not_satisfied: int, unknown: int, *, hard_failures: int = 0) -> RowRequirementsSummary:
    matches: list[RequirementMatch] = []
    for idx in range(satisfied):
        matches.append(
            RequirementMatch(
                requirement_id=f"sat_{idx}",
                label=f"sat_{idx}",
                status="satisfied",
                confidence=0.9,
                is_hard=idx < hard_failures,
            )
        )
    for idx in range(not_satisfied):
        matches.append(
            RequirementMatch(
                requirement_id=f"fail_{idx}",
                label=f"fail_{idx}",
                status="not_satisfied",
                confidence=0.9,
                is_hard=idx < hard_failures,
            )
        )
    for idx in range(unknown):
        matches.append(
            RequirementMatch(
                requirement_id=f"unk_{idx}",
                label=f"unk_{idx}",
                status="unknown",
                confidence=0.0,
            )
        )
    return RowRequirementsSummary(
        requirements_total_count=total,
        requirements_satisfied_count=satisfied,
        requirements_not_satisfied_count=not_satisfied,
        requirements_unknown_count=unknown,
        satisfaction_ratio=round(satisfied / total, 3) if total else 0.0,
        hard_requirements_satisfied_count=max(0, satisfied - hard_failures),
        matches=matches,
    )


def _row(
    entity_id: str,
    *,
    summary: RowRequirementsSummary,
    source_url: str = "https://example.com",
    include_website: bool = True,
    sources_count: int = 2,
    aggregate_confidence: float = 0.9,
) -> EntityRow:
    cells = {
        "name": _cell(entity_id, source_url, confidence=0.95),
        "headquarters": _cell("New York, NY", source_url, confidence=0.9),
        "focus_area": _cell("AI search engine", source_url, confidence=0.87),
        "stage_or_status": _cell("startup", source_url, confidence=0.86),
        "funding": _cell("$12M raised", source_url, confidence=0.86),
    }
    if include_website:
        cells["website"] = _cell(source_url, source_url, confidence=0.95)
    return EntityRow(
        entity_id=entity_id,
        cells=cells,
        aggregate_confidence=aggregate_confidence,
        sources_count=sources_count,
        requirement_summary=summary,
    )


def _plan() -> PlannerOutput:
    return PlannerOutput(
        query_family="organization_company",
        entity_type="organization",
        columns=["name", "website", "headquarters", "focus_area", "stage_or_status", "funding"],
    )


class TestRequirementAwareRanking:
    def test_more_satisfied_requirements_rank_higher_when_quality_is_similar(self):
        plan = _plan()
        strong_match = _row("strong_match", summary=_summary(4, 3, 0, 1))
        weak_match = _row("weak_match", summary=_summary(4, 1, 0, 3))

        ranked = rank_rows([weak_match, strong_match], plan, query="search engine startups in the US with funding > 10M")

        assert ranked[0].entity_id == "strong_match"

    def test_failed_hard_requirement_is_penalized(self):
        plan = _plan()
        satisfied = _row("satisfied", summary=_summary(2, 2, 0, 0))
        failed_hard = _row("failed_hard", summary=_summary(2, 1, 1, 0, hard_failures=1))

        ranked = rank_rows([failed_hard, satisfied], plan, query="search engine startups in the US with funding > 10M")

        assert ranked[0].entity_id == "satisfied"

    def test_unknown_does_not_beat_satisfied(self):
        plan = _plan()
        confirmed = _row("confirmed", summary=_summary(4, 2, 0, 2))
        unknown = _row("unknown", summary=_summary(4, 0, 0, 4))

        ranked = rank_rows([unknown, confirmed], plan, query="search engine startups in the US with funding > 10M")

        assert ranked[0].entity_id == "confirmed"

    def test_requirement_score_does_not_overpower_source_quality(self):
        plan = _plan()
        weak_sources = _row(
            "weak_sources",
            summary=_summary(4, 4, 0, 0),
            source_url="https://crunchbase.com/organization/weak-sources",
            include_website=False,
            sources_count=1,
            aggregate_confidence=0.78,
        )
        strong_sources = _row(
            "strong_sources",
            summary=_summary(4, 3, 0, 1),
            source_url="https://strongsources.com",
            include_website=True,
            sources_count=3,
            aggregate_confidence=0.94,
        )

        ranked = rank_rows([weak_sources, strong_sources], plan, query="search engine startups in the US with funding > 10M")

        assert ranked[0].entity_id == "strong_sources"
