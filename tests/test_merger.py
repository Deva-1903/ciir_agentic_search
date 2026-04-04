"""Tests for entity merger: deduplication and cell selection logic."""

import pytest
from app.models.schema import CellDraft, EntityDraft, PlannerOutput
from app.services.merger import merge_entities


def _make_plan(cols=None):
    return PlannerOutput(
        entity_type="startup",
        columns=cols or ["name", "website", "headquarters"],
        search_angles=["test query"],
    )


def _make_draft(name, cells_dict, url="https://source.com", title="Source"):
    cells = {
        col: CellDraft(value=v, evidence_snippet=f"Evidence for {v}", confidence=0.9)
        for col, v in cells_dict.items()
    }
    return EntityDraft(entity_name=name, cells=cells, source_url=url, source_title=title)


class TestMergeEntities:
    def test_single_entity_passthrough(self):
        draft = _make_draft("Stripe", {"name": "Stripe", "website": "https://stripe.com"})
        plan = _make_plan()
        rows = merge_entities([draft], plan)
        assert len(rows) == 1
        assert rows[0].cells["name"].value == "Stripe"

    def test_deduplicates_same_name(self):
        d1 = _make_draft("Stripe", {"name": "Stripe", "website": "https://stripe.com"}, url="https://a.com")
        d2 = _make_draft("Stripe Inc", {"name": "Stripe Inc", "headquarters": "San Francisco"}, url="https://b.com")
        plan = _make_plan()
        rows = merge_entities([d1, d2], plan)
        assert len(rows) == 1
        row = rows[0]
        # Both cells present
        assert "website" in row.cells
        assert "headquarters" in row.cells
        assert row.sources_count == 2

    def test_keeps_distinct_entities(self):
        d1 = _make_draft("Stripe", {"name": "Stripe"})
        d2 = _make_draft("Plaid", {"name": "Plaid"})
        plan = _make_plan()
        rows = merge_entities([d1, d2], plan)
        assert len(rows) == 2

    def test_prefers_higher_confidence(self):
        # Two drafts for same entity, second has higher confidence for website
        cells1 = {"name": "Corp", "website": "http://bad.com"}
        cells2 = {"name": "Corp", "website": "https://corp.com"}
        d1 = _make_draft("Corp", cells1, url="https://src1.com")
        d1.cells["website"].confidence = 0.5

        d2 = _make_draft("Corp", cells2, url="https://src2.com")
        d2.cells["website"].confidence = 0.95

        plan = _make_plan()
        rows = merge_entities([d1, d2], plan)
        assert len(rows) == 1
        assert rows[0].cells["website"].value == "https://corp.com"

    def test_entity_id_is_slug(self):
        draft = _make_draft("OpenAI Inc", {"name": "OpenAI Inc"})
        plan = _make_plan()
        rows = merge_entities([draft], plan)
        assert " " not in rows[0].entity_id
        assert rows[0].entity_id.startswith("openai")

    def test_aggregate_confidence_computed(self):
        draft = _make_draft("Acme", {"name": "Acme", "website": "acme.com"})
        plan = _make_plan()
        rows = merge_entities([draft], plan)
        assert 0.0 < rows[0].aggregate_confidence <= 1.0

    def test_updates_lookup_after_merge_when_website_arrives_later(self):
        d1 = _make_draft("Alpha Robotics", {"name": "Alpha Robotics"}, url="https://a.com")
        d2 = _make_draft(
            "Alpha Robotics Inc",
            {"name": "Alpha Robotics Inc", "website": "https://alpha.com"},
            url="https://b.com",
        )
        d3 = _make_draft(
            "Alpha Labs",
            {"name": "Alpha Labs", "headquarters": "Brooklyn"},
            url="https://c.com",
        )
        d3.cells["website"] = CellDraft(
            value="https://alpha.com/about",
            evidence_snippet="https://alpha.com/about",
            confidence=0.95,
        )

        plan = _make_plan()
        rows = merge_entities([d1, d2, d3], plan)

        assert len(rows) == 1
        assert rows[0].sources_count == 3
        assert rows[0].cells["headquarters"].value == "Brooklyn"
