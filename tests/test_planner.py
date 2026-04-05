"""Tests for the constrained planner."""

from __future__ import annotations

import pytest

from app.models.schema import SearchFacet
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


def test_classify_query_family_local_business():
    assert planner.classify_query_family("top pizza places in Brooklyn") == "local_business"


def test_classify_query_family_startup():
    assert planner.classify_query_family("AI startups in healthcare") == "startup_company"


def test_classify_query_family_software_tool():
    assert planner.classify_query_family("top open source databases for production use") == "software_tool"


def test_fallback_plan_uses_template_columns_and_family():
    plan = planner._fallback_plan("AI startups in healthcare")
    assert plan.query_family == "startup_company"
    assert plan.entity_type == "startup"
    assert plan.columns == ["name", "website", "headquarters", "focus_area", "product_or_service", "funding_stage"]
    assert plan.search_angles == [f.query for f in plan.facets]


@pytest.mark.asyncio
async def test_plan_schema_uses_deterministic_plan_when_llm_fails(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(planner, "chat_json_validated", boom)
    plan = await planner.plan_schema("pizza places in Brooklyn")
    assert plan.query_family == "local_business"
    assert plan.entity_type == "pizza place"
    assert "description" not in plan.columns
    assert plan.facets, "fallback should produce deterministic facets"


@pytest.mark.asyncio
async def test_plan_schema_prefers_template_over_generic_entity_type(monkeypatch):
    async def fake_llm(system, user, model_class, **kwargs):
        return model_class(
            entity_type="entity",
            facets=[
                SearchFacet(
                    type="entity_list",
                    query="top pizza places in Brooklyn",
                    expected_fill_columns=["name", "address"],
                    rationale="discover candidates",
                )
            ],
        )

    monkeypatch.setattr(planner, "chat_json_validated", fake_llm)
    plan = await planner.plan_schema("top pizza places in Brooklyn")

    assert plan.query_family == "local_business"
    assert plan.entity_type == "pizza place"
    assert plan.columns == ["name", "website", "address", "phone_number", "category", "rating"]


@pytest.mark.asyncio
async def test_plan_schema_parses_llm_facet_output_and_keeps_template_schema(monkeypatch):
    async def fake_llm(system, user, model_class, **kwargs):
        return model_class(
            entity_type="startup",
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

    assert plan.query_family == "startup_company"
    assert plan.entity_type == "startup"
    assert plan.columns == ["name", "website", "headquarters", "focus_area", "product_or_service", "funding_stage"]
    assert len(plan.facets) == 2
    assert plan.search_angles == [f.query for f in plan.facets]
    assert "bogus_col" not in plan.facets[1].expected_fill_columns


@pytest.mark.asyncio
async def test_plan_schema_falls_back_when_llm_returns_no_facets(monkeypatch):
    async def fake_llm(system, user, model_class, **kwargs):
        return model_class(entity_type="startup", facets=[])

    monkeypatch.setattr(planner, "chat_json_validated", fake_llm)
    plan = await planner.plan_schema("AI startups")
    assert plan.facets, "should have fallen back to deterministic facets"
