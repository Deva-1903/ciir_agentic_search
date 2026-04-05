"""Tests for the facet-typed planner."""

from __future__ import annotations

import pytest

from app.models.schema import PlannerOutput, SearchFacet
from app.services import planner


def test_search_facet_type_is_normalized_to_canonical():
    f = SearchFacet(type="Entity List", query="top pizza places")
    assert f.type == "entity_list"


def test_search_facet_type_unknown_falls_back_to_other():
    f = SearchFacet(type="random_made_up_kind", query="x")
    assert f.type == "other"


def test_sanitize_facets_drops_empty_queries_and_invalid_columns():
    facets = [
        SearchFacet(type="entity_list", query="", expected_fill_columns=["name"]),
        SearchFacet(
            type="official_source",
            query="company about page",
            expected_fill_columns=["name", "not_a_column", "website"],
        ),
    ]
    cleaned = planner._sanitize_facets(facets, ["name", "website"])
    assert len(cleaned) == 1
    assert cleaned[0].expected_fill_columns == ["name", "website"]


def test_fallback_plan_has_facets_and_search_angles():
    plan = planner._fallback_plan("AI startups")
    assert len(plan.facets) >= 3
    assert plan.search_angles == [f.query for f in plan.facets]
    assert plan.columns[0] == "name"


@pytest.mark.asyncio
async def test_plan_schema_uses_fallback_when_llm_fails(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(planner, "chat_json_validated", boom)
    plan = await planner.plan_schema("pizza places in Brooklyn")
    assert isinstance(plan, PlannerOutput)
    assert plan.facets, "fallback should produce facets"


def test_planner_output_validates_without_search_angles():
    """Regression test: LLM returns entity_type/columns/facets but NOT search_angles.
    PlannerOutput must validate (search_angles defaults to [])."""
    raw = {
        "entity_type": "startup",
        "columns": ["name", "website", "focus_area", "funding_stage"],
        "facets": [
            {
                "type": "entity_list",
                "query": "top AI startups in healthcare",
                "expected_fill_columns": ["name", "website"],
                "rationale": "discover candidates",
            }
        ],
    }
    plan = PlannerOutput.model_validate(raw)
    assert plan.entity_type == "startup"
    assert plan.search_angles == []
    assert len(plan.facets) == 1


def test_planner_output_validates_with_search_angles():
    """search_angles can still be provided explicitly (backward compat)."""
    raw = {
        "entity_type": "restaurant",
        "columns": ["name", "cuisine", "location"],
        "search_angles": ["best pizza NYC", "top pizza Brooklyn"],
        "facets": [],
    }
    plan = PlannerOutput.model_validate(raw)
    assert plan.search_angles == ["best pizza NYC", "top pizza Brooklyn"]
    assert plan.search_angles, "search_angles should mirror facet queries"
    assert plan.columns[0] == "name"


@pytest.mark.asyncio
async def test_plan_schema_parses_llm_facet_output(monkeypatch):
    async def fake_llm(system, user, model_class, **kwargs):
        return model_class(
            entity_type="startup",
            columns=["name", "website", "funding_stage"],
            search_angles=[],
            facets=[
                SearchFacet(
                    type="entity_list",
                    query="top AI healthcare startups 2025",
                    expected_fill_columns=["name", "website"],
                    rationale="list pages surface candidates",
                ),
                SearchFacet(
                    type="attribute_specific",
                    query="AI healthcare startup series A funding",
                    expected_fill_columns=["funding_stage", "bogus_col"],
                    rationale="funding-focused pages",
                ),
            ],
        )

    monkeypatch.setattr(planner, "chat_json_validated", fake_llm)
    plan = await planner.plan_schema("AI startups in healthcare")

    assert plan.columns[0] == "name"
    assert len(plan.facets) == 2
    # search_angles derives from facets
    assert plan.search_angles == [f.query for f in plan.facets]
    # invalid column names are stripped from expected_fill_columns
    assert "bogus_col" not in plan.facets[1].expected_fill_columns
    assert plan.facets[0].type == "entity_list"


@pytest.mark.asyncio
async def test_plan_schema_falls_back_when_llm_returns_no_facets(monkeypatch):
    async def fake_llm(system, user, model_class, **kwargs):
        return model_class(
            entity_type="startup",
            columns=["name", "website"],
            search_angles=[],
            facets=[],
        )

    monkeypatch.setattr(planner, "chat_json_validated", fake_llm)
    plan = await planner.plan_schema("AI startups")
    assert plan.facets, "should have fallen back to default facets"
