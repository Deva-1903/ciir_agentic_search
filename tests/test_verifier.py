"""Tests for the final result verifier."""

from app.models.schema import Cell, EntityRow, PlannerOutput
from app.services.verifier import verify_rows


def _plan() -> PlannerOutput:
    return PlannerOutput(
        entity_type="restaurant",
        columns=["name", "address", "phone_number", "website", "rating", "cuisine_type", "price_range"],
        search_angles=["best pizza places in Brooklyn"],
    )


def _cell(value: str, source_url: str, title: str, confidence: float = 0.9) -> Cell:
    return Cell(
        value=value,
        source_url=source_url,
        source_title=title,
        evidence_snippet=value,
        confidence=confidence,
    )


def test_verify_rows_filters_marketplace_only_results_for_top_query():
    row = EntityRow(
        entity_id="delivery-row",
        cells={
            "name": _cell(
                "Little Pepperoni Pizzeria",
                "https://www.ubereats.com/category/brooklyn-new-york-city/pizza",
                "THE 10 BEST PIZZA DELIVERY in Brooklyn",
                0.75,
            ),
            "address": _cell(
                "399 Avenue P",
                "https://www.ubereats.com/category/brooklyn-new-york-city/pizza",
                "THE 10 BEST PIZZA DELIVERY in Brooklyn",
                0.9,
            ),
        },
        aggregate_confidence=0.825,
        sources_count=1,
    )

    result = verify_rows([row], _plan(), "top pizza places in Brooklyn")

    # Falls back to the original rows when every row would be rejected.
    assert [r.entity_id for r in result] == ["delivery-row"]


def test_verify_rows_keeps_editorial_and_officially_supported_row():
    row = EntityRow(
        entity_id="lucali",
        cells={
            "name": _cell("Lucali", "https://www.theinfatuation.com/new-york/reviews/lucali", "Lucali Review", 0.9),
            "address": _cell("575 Henry St", "https://www.theinfatuation.com/new-york/reviews/lucali", "Lucali Review", 0.95),
            "website": _cell("https://lucali.com", "https://lucali.com/contact", "Contact Lucali", 1.0),
        },
        aggregate_confidence=0.95,
        sources_count=2,
    )

    result = verify_rows([row], _plan(), "top pizza places in Brooklyn")

    assert [r.entity_id for r in result] == ["lucali"]


def test_verify_rows_drops_marketplace_row_when_stronger_option_exists():
    weak = EntityRow(
        entity_id="delivery-row",
        cells={
            "name": _cell(
                "Little Pepperoni Pizzeria",
                "https://www.ubereats.com/category/brooklyn-new-york-city/pizza",
                "THE 10 BEST PIZZA DELIVERY in Brooklyn",
                0.75,
            ),
            "address": _cell(
                "399 Avenue P",
                "https://www.ubereats.com/category/brooklyn-new-york-city/pizza",
                "THE 10 BEST PIZZA DELIVERY in Brooklyn",
                0.9,
            ),
        },
        aggregate_confidence=0.825,
        sources_count=1,
    )
    strong = EntityRow(
        entity_id="lucali",
        cells={
            "name": _cell("Lucali", "https://www.theinfatuation.com/new-york/reviews/lucali", "Lucali Review", 0.9),
            "address": _cell("575 Henry St", "https://www.theinfatuation.com/new-york/reviews/lucali", "Lucali Review", 0.95),
            "website": _cell("https://lucali.com", "https://lucali.com/contact", "Contact Lucali", 1.0),
        },
        aggregate_confidence=0.95,
        sources_count=2,
    )

    result = verify_rows([weak, strong], _plan(), "top pizza places in Brooklyn")

    assert [r.entity_id for r in result] == ["lucali"]


def test_verify_rows_filters_pseudo_entity_category_label_when_real_option_exists():
    pseudo = EntityRow(
        entity_id="ai-copilots-and-agents-for-psychiatry",
        cells={
            "name": _cell(
                "AI Copilots & Agents for Psychiatry",
                "https://example.com/healthcare-ai-categories",
                "Healthcare AI Categories",
                0.92,
            ),
        },
        aggregate_confidence=0.92,
        sources_count=1,
    )
    real = EntityRow(
        entity_id="hippocratic-ai",
        cells={
            "name": _cell("Hippocratic AI", "https://www.hippocraticai.com/", "Hippocratic AI", 0.93),
            "website": _cell("https://www.hippocraticai.com", "https://www.hippocraticai.com/", "Hippocratic AI", 0.96),
        },
        aggregate_confidence=0.945,
        sources_count=2,
        canonical_domain="hippocraticai.com",
    )

    startup_plan = PlannerOutput(
        query_family="startup_company",
        entity_type="startup",
        columns=["name", "website", "headquarters", "focus_area", "product_or_service", "funding_stage"],
        search_angles=["AI startups in healthcare"],
    )

    result = verify_rows([pseudo, real], startup_plan, "AI startups in healthcare")

    assert [r.entity_id for r in result] == ["hippocratic-ai"]


def test_verify_rows_keeps_real_ampersand_business_name():
    row = EntityRow(
        entity_id="cuts-slices",
        cells={
            "name": _cell("Cuts & Slices", "https://cutsandslicesnyc.com/", "Cuts & Slices", 0.95),
            "website": _cell("https://cutsandslicesnyc.com", "https://cutsandslicesnyc.com/", "Cuts & Slices", 0.97),
            "address": _cell("93 Howard Ave", "https://cutsandslicesnyc.com/", "Cuts & Slices", 0.9),
        },
        aggregate_confidence=0.94,
        sources_count=2,
        canonical_domain="cutsandslicesnyc.com",
    )

    result = verify_rows([row], _plan(), "top pizza places in Brooklyn")

    assert [r.entity_id for r in result] == ["cuts-slices"]
