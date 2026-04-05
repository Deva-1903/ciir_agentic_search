"""Tests for scripts/eval.py _compute_metrics."""

import sys
from pathlib import Path

# Make scripts/ importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from eval import _compute_metrics


def _make_result(rows, columns, metadata=None):
    """Build a minimal completed search result dict."""
    return {
        "status": "done",
        "result": {
            "rows": rows,
            "columns": columns,
            "metadata": metadata or {},
        },
    }


def _cell(source_url="https://a.com", value="v"):
    return {"value": value, "source_url": source_url, "confidence": 0.8}


class TestComputeMetricsFailedResult:
    def test_failed_status_returns_zeros(self):
        m = _compute_metrics({"status": "failed", "error": "boom"})
        assert m["status"] == "failed"
        assert m["rows_returned"] == 0
        assert m["fill_rate"] == 0.0

    def test_missing_result_key(self):
        m = _compute_metrics({"status": "done"})
        assert m["rows_returned"] == 0


class TestComputeMetricsHappyPath:
    def test_single_row_full_fill(self):
        rows = [
            {
                "cells": {
                    "name": _cell(),
                    "website": _cell("https://b.com"),
                },
                "aggregate_confidence": 0.85,
            }
        ]
        m = _compute_metrics(_make_result(rows, ["name", "website"]))
        assert m["status"] == "done"
        assert m["rows_returned"] == 1
        assert m["avg_cells_per_row"] == 2.0
        assert m["fill_rate"] == 1.0  # 2/2

    def test_actionable_rate(self):
        rows = [
            {
                "cells": {"name": _cell(), "website": _cell()},
                "aggregate_confidence": 0.9,
            },
            {
                "cells": {"name": _cell()},
                "aggregate_confidence": 0.5,
            },
        ]
        m = _compute_metrics(_make_result(rows, ["name", "website"]))
        assert m["actionable_rate"] == 0.5  # 1 of 2 rows has actionable
        assert m["avg_actionable_fields"] == 0.5

    def test_multi_source_detection(self):
        rows = [
            {
                "cells": {
                    "name": _cell("https://a.com"),
                    "website": _cell("https://b.com"),
                },
                "aggregate_confidence": 0.7,
            },
        ]
        m = _compute_metrics(_make_result(rows, ["name", "website"]))
        assert m["multi_source_rate"] == 1.0

    def test_single_source_row(self):
        rows = [
            {
                "cells": {
                    "name": _cell("https://a.com"),
                    "website": _cell("https://a.com"),
                },
                "aggregate_confidence": 0.7,
            },
        ]
        m = _compute_metrics(_make_result(rows, ["name", "website"]))
        assert m["multi_source_rate"] == 0.0

    def test_source_diversity_multi_domain(self):
        rows = [
            {
                "cells": {
                    "name": _cell("https://a.com"),
                    "phone": _cell("https://b.com"),
                },
                "aggregate_confidence": 0.8,
            },
        ]
        m = _compute_metrics(_make_result(rows, ["name", "phone"]))
        assert m["avg_source_diversity"] == 0.5  # 1 - (1/2)

    def test_metadata_passthrough(self):
        meta = {
            "duration_seconds": 12.3,
            "pages_scraped": 8,
            "gap_fill_used": True,
            "query_family": "local_business",
            "normalized_query": "best pizza places in Brooklyn",
        }
        m = _compute_metrics(_make_result(
            [{"cells": {"name": _cell()}, "aggregate_confidence": 0.5, "canonical_domain": "lucali.com"}],
            ["name"],
            metadata=meta,
        ))
        assert m["duration_seconds"] == 12.3
        assert m["pages_scraped"] == 8
        assert m["gap_fill_used"] is True
        assert m["official_site_rate"] == 1.0
        assert m["query_family"] == "local_business"
        assert m["normalized_query"] == "best pizza places in Brooklyn"
