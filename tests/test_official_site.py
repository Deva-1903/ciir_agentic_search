"""Tests for heuristic official-site resolution."""

from app.models.schema import Cell, EntityRow, ScrapedPage
from app.services.official_site import resolve_official_sites


def _cell(value: str, url: str = "https://source.com/page") -> Cell:
    return Cell(
        value=value,
        source_url=url,
        source_title="Source",
        evidence_snippet=value,
        confidence=0.8,
    )


def test_resolve_official_site_prefers_matching_non_editorial_domain():
    row = EntityRow(
        entity_id="lucali",
        cells={"name": _cell("Lucali")},
        aggregate_confidence=0.8,
        sources_count=1,
    )
    pages = [
        ScrapedPage(
            url="https://www.theinfatuation.com/new-york/reviews/lucali",
            title="Lucali Review",
            cleaned_text="Lucali is one of the best pizza places in Brooklyn.",
        ),
        ScrapedPage(
            url="https://lucali.com/contact",
            title="Contact Lucali",
            cleaned_text="Lucali 575 Henry St Brooklyn NY 11231.",
        ),
    ]

    rows, resolved = resolve_official_sites([row], pages)

    assert resolved == 1
    assert rows[0].cells["website"].value == "https://lucali.com/"
    assert rows[0].canonical_domain == "lucali.com"


def test_resolve_official_site_keeps_existing_better_website():
    row = EntityRow(
        entity_id="lucali",
        cells={
            "name": _cell("Lucali"),
            "website": Cell(
                value="https://lucali.com/",
                source_url="https://lucali.com/",
                source_title="Lucali",
                evidence_snippet="Lucali",
                confidence=0.98,
            ),
        },
        aggregate_confidence=0.9,
        sources_count=2,
        canonical_domain="lucali.com",
    )
    pages = [
        ScrapedPage(
            url="https://lucali.com/about",
            title="About Lucali",
            cleaned_text="Lucali pizza restaurant in Brooklyn.",
        )
    ]

    rows, resolved = resolve_official_sites([row], pages)

    assert resolved == 0
    assert rows[0].cells["website"].value == "https://lucali.com/"
