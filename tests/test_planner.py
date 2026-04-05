"""Tests for the constrained planner (structural entity-kind families)."""

from __future__ import annotations

import pytest

from app.models.schema import SearchFacet
from app.services import planner


def test_search_facet_type_is_normalized_to_canonical():
    f = SearchFacet(type="Entity List", query="top restaurants")
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


def test_classify_query_family_place_venue_from_location_phrase():
    # A concrete venue noun + location phrase → place_venue (structurally).
    assert planner.classify_query_family("top pizza restaurants in Brooklyn") == "place_venue"


def test_classify_query_family_organization_company_for_startups():
    # "startups" is an organizational signal, not a vertical-only tag.
    assert planner.classify_query_family("AI startups in healthcare") == "organization_company"


def test_classify_query_family_software_project_for_databases():
    assert planner.classify_query_family("top open source databases for production use") == "software_project"


def test_classify_query_family_product_offering_for_brands():
    assert planner.classify_query_family("best noise cancelling headphone brands") == "product_offering"


def test_classify_query_family_person_group():
    assert planner.classify_query_family("notable climate researchers in 2024") == "person_group"


def test_classify_query_family_generic_entity_list_fallback():
    # No structural signal → generic_entity_list.
    assert planner.classify_query_family("interesting phenomena worth knowing") == "generic_entity_list"


def test_fallback_plan_uses_template_columns_for_organization_family():
    plan = planner._fallback_plan("AI startups in healthcare")
    assert plan.query_family == "organization_company"
    assert plan.columns == [
        "name", "website", "headquarters",
        "focus_area", "product_or_service", "stage_or_status",
    ]
    assert plan.search_angles == [f.query for f in plan.facets]


def test_fallback_plan_uses_place_venue_template_for_location_query():
    plan = planner._fallback_plan("top pizza restaurants in Brooklyn")
    assert plan.query_family == "place_venue"
    assert plan.columns == [
        "name", "website", "location",
        "category", "offering", "contact_or_booking",
    ]


def test_fallback_plan_uses_software_template():
    plan = planner._fallback_plan("top open source databases")
    assert plan.query_family == "software_project"
    assert plan.columns == [
        "name", "website_or_repo", "primary_use_case",
        "license", "language_or_stack", "maintainer_or_org",
    ]


@pytest.mark.asyncio
async def test_plan_schema_uses_deterministic_plan_when_llm_fails(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(planner, "chat_json_validated", boom)
    plan = await planner.plan_schema("pizza places in Brooklyn")
    assert plan.query_family == "place_venue"
    assert "description" not in plan.columns
    assert plan.facets, "fallback should produce deterministic facets"


@pytest.mark.asyncio
async def test_plan_schema_prefers_template_over_generic_entity_type(monkeypatch):
    async def fake_llm(system, user, model_class, **kwargs):
        return model_class(
            entity_type="entity",  # generic — should be rejected
            facets=[
                SearchFacet(
                    type="entity_list",
                    query="top pizza places in Brooklyn",
                    expected_fill_columns=["name", "location"],
                    rationale="discover candidates",
                )
            ],
        )

    monkeypatch.setattr(planner, "chat_json_validated", fake_llm)
    plan = await planner.plan_schema("top pizza restaurants in Brooklyn")

    assert plan.query_family == "place_venue"
    # Entity type falls back to the template default when LLM returns "entity".
    assert plan.entity_type != "entity"
    assert plan.columns == [
        "name", "website", "location",
        "category", "offering", "contact_or_booking",
    ]


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
                    query="AI healthcare startup stage",
                    expected_fill_columns=["stage_or_status", "bogus_col"],
                    rationale="stage-focused pages",
                ),
            ],
        )

    monkeypatch.setattr(planner, "chat_json_validated", fake_llm)
    plan = await planner.plan_schema("AI startups in healthcare")

    assert plan.query_family == "organization_company"
    assert plan.entity_type == "startup"
    assert plan.columns == [
        "name", "website", "headquarters",
        "focus_area", "product_or_service", "stage_or_status",
    ]
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
