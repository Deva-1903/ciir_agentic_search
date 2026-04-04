"""Tests for targeted gap-fill quality safeguards."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.schema import BraveResult, Cell, CellDraft, EntityDraft, EntityRow, PlannerOutput, ScrapedPage
from app.services import gap_fill


def _cell(value: str, confidence: float = 0.9) -> Cell:
    return Cell(
        value=value,
        source_url="https://source.com",
        source_title="Source",
        evidence_snippet=value,
        confidence=confidence,
    )


@pytest.mark.asyncio
async def test_gap_fill_only_accepts_matching_entity(monkeypatch):
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        gap_fill,
        "get_settings",
        lambda: SimpleNamespace(
            gap_fill_max_entities=1,
            gap_fill_max_urls_per_entity=1,
        ),
    )

    async def fake_run_brave_search(queries: list[str], top_k: int):
        captured["queries"] = queries
        return [BraveResult(url="https://example.com/lucali", title="Lucali")]

    async def fake_scrape_pages(results: list[BraveResult]):
        return [ScrapedPage(url="https://example.com/lucali", title="Lucali", cleaned_text="content")]

    async def fake_extract_from_page(query: str, plan: PlannerOutput, page: ScrapedPage):
        captured["query"] = query
        captured["columns"] = plan.columns
        return [
            EntityDraft(
                entity_name="F&F Pizzeria",
                cells={
                    "website": CellDraft(
                        value="https://ffpizzeria.com",
                        evidence_snippet="https://ffpizzeria.com",
                        confidence=0.95,
                    )
                },
                source_url=page.url,
                source_title=page.title,
            ),
            EntityDraft(
                entity_name="Lucali",
                cells={
                    "website": CellDraft(
                        value="https://www.lucali.com",
                        evidence_snippet="https://www.lucali.com",
                        confidence=0.95,
                    )
                },
                source_url=page.url,
                source_title=page.title,
            ),
        ]

    monkeypatch.setattr(gap_fill, "run_brave_search", fake_run_brave_search)
    monkeypatch.setattr(gap_fill, "scrape_pages", fake_scrape_pages)
    monkeypatch.setattr(gap_fill, "extract_from_page", fake_extract_from_page)

    row = EntityRow(
        entity_id="lucali",
        cells={
            "name": _cell("Lucali"),
            "address": _cell("575 Henry St, Brooklyn, NY 11231"),
        },
        aggregate_confidence=0.95,
        sources_count=2,
    )
    plan = PlannerOutput(
        entity_type="restaurant",
        columns=["name", "address", "website"],
        search_angles=["top pizza places in Brooklyn"],
    )

    rows, gap_fill_used = await gap_fill.run_gap_fill([row], plan, "top pizza places in Brooklyn")

    assert gap_fill_used is True
    assert rows[0].cells["website"].value == "https://www.lucali.com"
    assert captured["query"] == "Lucali"
    assert captured["columns"] == ["name", "website"]
    assert any("official website" in query for query in captured["queries"])
