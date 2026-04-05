"""Tests for source-quality heuristics."""

from app.models.schema import Cell, EntityRow
from app.services.source_quality import classify_source, row_source_profile, row_source_quality


def _cell(value: str, source_url: str, source_title: str = "Source", confidence: float = 0.9) -> Cell:
    return Cell(
        value=value,
        source_url=source_url,
        source_title=source_title,
        evidence_snippet=value,
        confidence=confidence,
    )


def test_classify_source_prefers_official_domain_match():
    kind, score = classify_source(
        "https://www.lucali.com/menu",
        "Lucali Menu",
        official_website="https://lucali.com",
    )

    assert kind == "official"
    assert score == 1.0


def test_classify_source_penalizes_marketplace_category_pages():
    kind, score = classify_source(
        "https://www.ubereats.com/category/brooklyn-new-york-city/pizza",
        "THE 10 BEST PIZZA DELIVERY in Brooklyn",
    )

    assert kind == "marketplace"
    assert score < 0.2


def test_row_source_quality_uses_weighted_average_of_sources():
    # Row has one official cell (domain match) and one editorial-shaped cell.
    # Expected: weighted score well above 0.8, because the official cell is
    # heavily weighted and the editorial cell still has reasonable quality.
    row = EntityRow(
        entity_id="lucali",
        cells={
            "name": _cell("Lucali", "https://www.tastingtable.com/article/best-brooklyn-pizza", "Best Pizza In Brooklyn", 0.8),
            "website": _cell("https://lucali.com", "https://lucali.com/about", "Lucali About", 1.0),
        },
        aggregate_confidence=0.9,
        sources_count=2,
    )

    score = row_source_quality(row)
    profile = row_source_profile(row)

    assert score > 0.8
    assert profile["official"] == 1
    # Editorial-shaped URL on an unclassified domain is still recognised.
    assert profile["editorial"] == 1
