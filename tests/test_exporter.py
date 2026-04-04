"""Tests for JSON and CSV export."""

import csv
import io
import json
import pytest

from app.models.schema import Cell, EntityRow, SearchMetadata, SearchResponse
from app.services.exporter import to_csv, to_json


def _make_response() -> SearchResponse:
    row = EntityRow(
        entity_id="stripe",
        cells={
            "name": Cell(
                value="Stripe",
                source_url="https://techcrunch.com/stripe",
                source_title="TechCrunch",
                evidence_snippet="Stripe is a payments company",
                confidence=0.95,
            ),
            "website": Cell(
                value="https://stripe.com",
                source_url="https://stripe.com",
                source_title="Stripe",
                evidence_snippet="Official website",
                confidence=0.99,
            ),
        },
        aggregate_confidence=0.97,
        sources_count=2,
    )
    return SearchResponse(
        query_id="abc123",
        query="payment startups",
        entity_type="startup",
        columns=["name", "website", "headquarters"],
        rows=[row],
        metadata=SearchMetadata(
            search_angles=["payment startups"],
            urls_considered=10,
            pages_scraped=5,
            entities_extracted=3,
            entities_after_merge=1,
            gap_fill_used=False,
            duration_seconds=12.5,
        ),
    )


class TestToJson:
    def test_valid_json(self):
        response = _make_response()
        output = to_json(response)
        parsed = json.loads(output)
        assert parsed["query"] == "payment startups"
        assert len(parsed["rows"]) == 1

    def test_contains_cells(self):
        response = _make_response()
        parsed = json.loads(to_json(response))
        row = parsed["rows"][0]
        assert "name" in row["cells"]
        assert row["cells"]["name"]["value"] == "Stripe"
        assert "evidence_snippet" in row["cells"]["name"]
        assert "source_url" in row["cells"]["name"]


class TestToCsv:
    def _parse_csv(self, content: str) -> list[dict]:
        reader = csv.DictReader(io.StringIO(content))
        return list(reader)

    def test_has_header(self):
        response = _make_response()
        content = to_csv(response)
        assert "name" in content.split("\n")[0]

    def test_provenance_columns(self):
        response = _make_response()
        header = to_csv(response).split("\n")[0]
        assert "name_source_url" in header
        assert "website_source_url" in header

    def test_one_row_per_entity(self):
        response = _make_response()
        rows = self._parse_csv(to_csv(response))
        assert len(rows) == 1

    def test_cell_values_populated(self):
        response = _make_response()
        rows = self._parse_csv(to_csv(response))
        assert rows[0]["name"] == "Stripe"
        assert rows[0]["name_source_url"] == "https://techcrunch.com/stripe"

    def test_missing_cells_are_empty(self):
        response = _make_response()
        rows = self._parse_csv(to_csv(response))
        # headquarters was not extracted
        assert rows[0]["headquarters"] == ""
        assert rows[0]["headquarters_source_url"] == ""

    def test_confidence_column(self):
        response = _make_response()
        rows = self._parse_csv(to_csv(response))
        assert float(rows[0]["name_confidence"]) == pytest.approx(0.95)
