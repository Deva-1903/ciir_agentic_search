"""Tests for requirement parsing and row-level evaluation."""

from app.models.schema import Cell, EntityRow, PlannerOutput, RequirementSpec
from app.services.requirement_parser import parse_requirements_deterministic, prepare_requirements
from app.services.requirement_scorer import build_requirement_summary, evaluate_requirement


def _cell(value: str, *, url: str = "https://example.com", title: str = "Example", confidence: float = 0.9) -> Cell:
    return Cell(
        value=value,
        source_url=url,
        source_title=title,
        evidence_snippet=value,
        confidence=confidence,
    )


def _row(cells: dict[str, str]) -> EntityRow:
    return EntityRow(
        entity_id="row-1",
        cells={key: _cell(value) for key, value in cells.items()},
        aggregate_confidence=0.9,
        sources_count=2,
    )


class TestRequirementParsing:
    def test_prepare_requirements_preserves_numeric_constraint_and_augments_plan(self):
        plan = PlannerOutput(
            query_family="organization_company",
            entity_type="organization",
            columns=[
                "name",
                "website",
                "headquarters",
                "focus_area",
                "product_or_service",
                "stage_or_status",
            ],
        )

        requirements, augmented_plan = prepare_requirements(
            "search engine startups in the US with funding > 10M",
            normalized_query="search engine startups in the US with funding 10 M",
            plan=plan,
        )

        assert augmented_plan is not None
        assert augmented_plan.columns == [
            "name",
            "website",
            "headquarters",
            "focus_area",
            "product_or_service",
            "stage_or_status",
            "funding",
        ]

        labels = {req.label for req in requirements}
        assert labels == {
            "Location: US",
            "Funding > 10M",
            "Stage: startup",
            "Topic: search engine",
        }

        funding = next(req for req in requirements if req.label == "Funding > 10M")
        assert funding.mapped_columns == ["funding"]

        location = next(req for req in requirements if req.kind == "location")
        assert location.mapped_columns == ["headquarters"]

    def test_parses_location_requirement(self):
        requirements = parse_requirements_deterministic("research organizations based in Europe")

        assert len(requirements) == 1
        requirement = requirements[0]
        assert requirement.kind == "location"
        assert requirement.target_value == "eu"
        assert requirement.source_phrase == "based in Europe"

    def test_parses_categorical_requirement(self):
        requirements = parse_requirements_deterministic("open source observability tools")

        assert any(req.label == "License: open-source" for req in requirements)

    def test_skips_vague_query(self):
        requirements = parse_requirements_deterministic("best companies to watch")

        assert requirements == []


class TestRequirementEvaluation:
    def test_requirement_summary_counts_and_nested_evidence(self):
        row = EntityRow(
            entity_id="perplexity",
            cells={
                "name": _cell("Perplexity", url="https://perplexity.ai", confidence=0.95),
                "headquarters": _cell("San Francisco, California", url="https://perplexity.ai/about", confidence=0.94),
                "focus_area": _cell("AI search engine", url="https://perplexity.ai", confidence=0.9),
                "stage_or_status": _cell("startup", url="https://perplexity.ai", confidence=0.9),
                "funding": _cell("$100M raised", url="https://news.example.com/perplexity", title="Funding news", confidence=0.88),
            },
            aggregate_confidence=0.91,
            sources_count=3,
        )

        specs = [
            RequirementSpec(
                id="loc_0",
                label="Location: US",
                kind="location",
                operator="contains",
                target_value="us",
                target_value_raw="US",
                source_phrase="in the US",
                mapped_columns=["headquarters"],
                is_hard=True,
            ),
            RequirementSpec(
                id="fund_0",
                label="Funding > 10M",
                kind="numeric",
                operator="greater_than",
                target_value="10M",
                target_value_raw="10M",
                source_phrase="funding > 10M",
                mapped_columns=["funding"],
                is_hard=True,
            ),
            RequirementSpec(
                id="stage_0",
                label="Stage: startup",
                kind="categorical",
                operator="contains",
                target_value="startup",
                target_value_raw="startups",
                source_phrase="startups",
                mapped_columns=["stage_or_status"],
            ),
            RequirementSpec(
                id="topic_0",
                label="Topic: search engine",
                kind="semantic",
                operator="matches_topic",
                target_value="search engine",
                target_value_raw="search engine",
                source_phrase="search engine startups",
                mapped_columns=["focus_area", "product_or_service"],
            ),
        ]

        summary = build_requirement_summary(specs, row)

        assert summary.requirements_total_count == 4
        assert summary.requirements_satisfied_count == 4
        assert summary.requirements_not_satisfied_count == 0
        assert summary.requirements_unknown_count == 0
        assert summary.satisfaction_ratio == 1.0
        assert all(match.evidence is not None for match in summary.matches)
        assert all(match.score_contribution == 0.25 for match in summary.matches)

    def test_numeric_requirement_handles_kmb_and_unknown(self):
        spec = RequirementSpec(
            id="fund_0",
            label="Funding > 1M",
            kind="numeric",
            operator="greater_than",
            target_value="1M",
            target_value_raw="1M",
            source_phrase="funding > 1M",
            mapped_columns=["funding"],
        )

        satisfied = evaluate_requirement(spec, _row({"funding": "$2.5M raised"}))
        not_satisfied = evaluate_requirement(spec, _row({"funding": "$900K raised"}))
        unknown = evaluate_requirement(spec, _row({"funding": "well funded"}))

        assert satisfied.status == "satisfied"
        assert not_satisfied.status == "not_satisfied"
        assert unknown.status == "unknown"

    def test_location_requirement_normalizes_states_and_eu_countries(self):
        us_spec = RequirementSpec(
            id="loc_0",
            label="Location: US",
            kind="location",
            operator="contains",
            target_value="us",
            target_value_raw="US",
            source_phrase="in the US",
            mapped_columns=["headquarters"],
        )
        eu_spec = RequirementSpec(
            id="loc_1",
            label="Location: Europe",
            kind="location",
            operator="contains",
            target_value="eu",
            target_value_raw="Europe",
            source_phrase="based in Europe",
            mapped_columns=["headquarters"],
        )

        us_match = evaluate_requirement(us_spec, _row({"headquarters": "New York, NY"}))
        eu_match = evaluate_requirement(eu_spec, _row({"headquarters": "Berlin, Germany"}))

        assert us_match.status == "satisfied"
        assert eu_match.status == "satisfied"

    def test_semantic_requirement_supports_satisfied_failed_and_unknown(self):
        spec = RequirementSpec(
            id="topic_0",
            label="Topic: search engine",
            kind="semantic",
            operator="matches_topic",
            target_value="search engine",
            target_value_raw="search engine",
            source_phrase="search engine startups",
            mapped_columns=["focus_area"],
        )

        satisfied = evaluate_requirement(spec, _row({"focus_area": "AI search engine"}))
        not_satisfied = evaluate_requirement(spec, _row({"focus_area": "customer support automation"}))
        unknown = evaluate_requirement(spec, _row({"website": "https://example.com"}))

        assert satisfied.status == "satisfied"
        assert not_satisfied.status == "not_satisfied"
        assert unknown.status == "unknown"
