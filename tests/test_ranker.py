"""Tests for the entity ranker."""

import pytest
from app.models.schema import Cell, EntityRow, PlannerOutput
from app.services.ranker import find_sparse_rows, prune_rows, rank_rows


def _plan(cols=None):
    return PlannerOutput(
        entity_type="startup",
        columns=cols or ["name", "website", "headquarters", "funding_stage"],
        search_angles=["test"],
    )


def _cell(v="val", conf=0.9):
    return Cell(
        value=v,
        source_url="https://src.com",
        source_title="Source",
        evidence_snippet="evidence",
        confidence=conf,
    )


def _row(entity_id, cells_dict, sources=1):
    return EntityRow(
        entity_id=entity_id,
        cells={k: _cell(v) for k, v in cells_dict.items()},
        aggregate_confidence=0.9,
        sources_count=sources,
    )


class TestRankRows:
    def test_complete_row_ranked_first(self):
        plan = _plan()
        complete = _row("a", {"name": "A", "website": "w", "headquarters": "h", "funding_stage": "f"}, sources=3)
        sparse   = _row("b", {"name": "B"})
        ranked = rank_rows([sparse, complete], plan)
        assert ranked[0].entity_id == "a"

    def test_more_sources_ranks_higher(self):
        plan = _plan(["name", "website"])
        r1 = _row("many", {"name": "X", "website": "w"}, sources=5)
        r2 = _row("few",  {"name": "Y", "website": "w"}, sources=1)
        ranked = rank_rows([r2, r1], plan)
        assert ranked[0].entity_id == "many"

    def test_returns_all_rows(self):
        plan = _plan()
        rows = [_row(str(i), {"name": f"E{i}"}) for i in range(10)]
        ranked = rank_rows(rows, plan)
        assert len(ranked) == 10

    def test_better_sources_rank_higher(self):
        plan = _plan(["name", "address", "website"])
        weak = EntityRow(
            entity_id="weak",
            cells={
                "name": Cell(
                    value="Little Pepperoni",
                    source_url="https://www.ubereats.com/category/brooklyn-new-york-city/pizza",
                    source_title="THE 10 BEST PIZZA DELIVERY in Brooklyn",
                    evidence_snippet="Little Pepperoni",
                    confidence=0.75,
                ),
                "address": Cell(
                    value="399 Avenue P",
                    source_url="https://www.ubereats.com/category/brooklyn-new-york-city/pizza",
                    source_title="THE 10 BEST PIZZA DELIVERY in Brooklyn",
                    evidence_snippet="399 Avenue P",
                    confidence=0.9,
                ),
            },
            aggregate_confidence=0.82,
            sources_count=1,
        )
        strong = EntityRow(
            entity_id="strong",
            cells={
                "name": Cell(
                    value="Lucali",
                    source_url="https://www.theinfatuation.com/new-york/reviews/lucali",
                    source_title="Lucali Review",
                    evidence_snippet="Lucali",
                    confidence=0.9,
                ),
                "address": Cell(
                    value="575 Henry St",
                    source_url="https://www.theinfatuation.com/new-york/reviews/lucali",
                    source_title="Lucali Review",
                    evidence_snippet="575 Henry St",
                    confidence=0.95,
                ),
                "website": Cell(
                    value="https://lucali.com",
                    source_url="https://lucali.com/about",
                    source_title="Lucali About",
                    evidence_snippet="https://lucali.com",
                    confidence=1.0,
                ),
            },
            aggregate_confidence=0.95,
            sources_count=2,
        )

        ranked = rank_rows([weak, strong], plan)
        assert ranked[0].entity_id == "strong"


class TestPruneRows:
    def test_prunes_generic_name_plus_one_weak_field_rows(self):
        plan = _plan(["name", "address", "cuisine_type", "rating"])
        weak = _row("weak", {"name": "Pizza Place", "cuisine_type": "Pizza"})
        actionable = _row("good", {"name": "Lucali", "address": "575 Henry St"})

        pruned = prune_rows([weak, actionable], plan)

        assert [row.entity_id for row in pruned] == ["good"]

    def test_falls_back_to_original_rows_when_everything_is_pruned(self):
        plan = _plan(["name", "cuisine_type", "rating"])
        weak = _row("weak", {"name": "Pizza Place", "cuisine_type": "Pizza"})

        pruned = prune_rows([weak], plan)

        assert [row.entity_id for row in pruned] == ["weak"]


class TestFindSparseRows:
    def test_finds_most_sparse(self):
        plan = _plan()
        full   = _row("full",   {"name": "A", "website": "w", "headquarters": "h", "funding_stage": "s"})
        sparse = _row("sparse", {"name": "B", "website": "w"})
        result = find_sparse_rows([full, sparse], plan, top_n=1)
        assert result[0].entity_id == "sparse"

    def test_requires_name_cell(self):
        plan = _plan()
        no_name = _row("noname", {"website": "w"})
        no_name.cells.pop("name", None)
        # Actually EntityRow won't have name here since _row sets website key
        # Let's build one with no name key
        no_name2 = EntityRow(
            entity_id="x",
            cells={"website": _cell("w")},
            aggregate_confidence=0.5,
            sources_count=1,
        )
        result = find_sparse_rows([no_name2], plan, top_n=3)
        assert len(result) == 0

    def test_top_n_limit(self):
        plan = _plan()
        rows = [_row(str(i), {"name": f"E{i}"}) for i in range(10)]
        result = find_sparse_rows(rows, plan, top_n=3)
        assert len(result) <= 3

    def test_skips_low_information_rows(self):
        plan = _plan(["name", "address", "cuisine_type", "rating"])
        weak = _row("weak", {"name": "Pizza Place", "cuisine_type": "Pizza"})
        actionable = _row("good", {"name": "Lucali", "address": "575 Henry St"})

        result = find_sparse_rows([weak, actionable], plan, top_n=3)

        assert [row.entity_id for row in result] == ["good"]
