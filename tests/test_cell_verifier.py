"""Tests for cell-level name-alignment verification."""

from __future__ import annotations

from app.models.schema import Cell, EntityRow
from app.services import cell_verifier
from app.services.cell_verifier import verify_row_cells


def _cell(value, source_url, source_title, snippet, conf=0.9):
    return Cell(
        value=value,
        source_url=source_url,
        source_title=source_title,
        evidence_snippet=snippet,
        confidence=conf,
    )


def _row(name, extra_cells, name_conf=0.9):
    cells = {
        "name": _cell(name, "https://src.com", "Source", name, conf=name_conf),
    }
    cells.update(extra_cells)
    return EntityRow(
        entity_id="x",
        cells=cells,
        aggregate_confidence=0.9,
        sources_count=len({c.source_url for c in cells.values()}),
    )


def test_cell_with_entity_name_in_evidence_is_kept_at_full_confidence():
    row = _row("Lucali", {
        "address": _cell(
            "575 Henry St",
            "https://infatuation.com/lucali",
            "Lucali Review",
            "Lucali is at 575 Henry St in Carroll Gardens",
            conf=0.9,
        ),
    })
    verify_row_cells(row)
    assert row.cells["address"].confidence == 0.9


def test_cell_without_entity_name_in_evidence_is_penalized():
    row = _row("Espresso Pizzeria", {
        "phone": _cell(
            "718-555-1234",
            "https://random.com/list",
            "Brooklyn Pizza List",
            # Evidence clearly describes a different entity
            "F&F Pizzeria can be reached at 718-555-1234",
            conf=0.9,
        ),
    })
    verify_row_cells(row)
    assert row.cells["phone"].confidence < 0.9
    # Should equal 0.9 * 0.6 = 0.54
    assert abs(row.cells["phone"].confidence - 0.54) < 0.001


def test_cell_from_entity_own_domain_is_kept_even_without_name_mention():
    row = _row("Lucali", {
        "website": _cell(
            "https://lucali.com",
            "https://lucali.com/about",
            "About",
            "welcome",  # no name mention
            conf=0.9,
        ),
        "address": _cell(
            "575 Henry St",
            "https://lucali.com/contact",
            "Contact",
            "visit us at the address below",  # no name but same domain
            conf=0.85,
        ),
    })
    verify_row_cells(row)
    assert row.cells["address"].confidence == 0.85  # aligned by domain


def test_weak_signal_columns_are_skipped():
    # Short descriptive fields are too generic to verify against the entity
    # name — they are expected to be skipped regardless of vertical.
    row = _row("Lucali", {
        "category": _cell(
            "Pizza",
            "https://random.com",
            "List of pizza shops",
            "pizza",
            conf=0.9,
        ),
        "description": _cell(
            "A cozy local spot",
            "https://random.com",
            "List of pizza shops",
            "a cozy local spot",
            conf=0.9,
        ),
    })
    verify_row_cells(row)
    # category and description are in _SKIP_COLS and should not be penalized.
    assert row.cells["category"].confidence == 0.9
    assert row.cells["description"].confidence == 0.9


def test_row_aggregate_confidence_is_recomputed_when_penalized():
    row = _row("Espresso Pizzeria", {
        "phone": _cell(
            "718-555-1234",
            "https://random.com",
            "Some List",
            "F&F Pizzeria phone",
            conf=1.0,
        ),
    })
    original_agg = row.aggregate_confidence
    verify_row_cells(row)
    assert row.aggregate_confidence != original_agg


def test_row_without_name_is_skipped_gracefully():
    row = EntityRow(
        entity_id="x",
        cells={
            "address": _cell("x", "https://y.com", "T", "snippet", conf=0.9),
        },
        aggregate_confidence=0.9,
        sources_count=1,
    )
    stats = verify_row_cells(row)
    assert stats["checked"] == 0
    assert row.cells["address"].confidence == 0.9


def test_cell_verified_via_source_title():
    row = _row("Lucali", {
        "phone": _cell(
            "718-555-0000",
            "https://aggregator.com/123",
            "Lucali Brooklyn Contact Info",  # title mentions entity
            "reach them at the number below",
            conf=0.9,
        ),
    })
    verify_row_cells(row)
    assert row.cells["phone"].confidence == 0.9
