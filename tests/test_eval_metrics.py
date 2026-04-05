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
            "query_family": "place_venue",
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
        assert m["query_family"] == "place_venue"
        assert m["normalized_query"] == "best pizza places in Brooklyn"

    def test_labeled_entity_and_field_metrics(self):
        rows = [
            {
                "cells": {
                    "name": {
                        "value": "Prometheus",
                        "source_url": "https://github.com/prometheus/prometheus",
                        "evidence_snippet": "Prometheus",
                        "confidence": 0.8,
                    },
                    "website_or_repo": {
                        "value": "https://github.com/prometheus/prometheus",
                        "source_url": "https://github.com/prometheus/prometheus",
                        "evidence_snippet": "https://github.com/prometheus/prometheus",
                        "confidence": 0.8,
                    },
                    "license": {
                        "value": "Apache 2.0",
                        "source_url": "https://github.com/prometheus/prometheus",
                        "evidence_snippet": "Licensed under Apache 2.0",
                        "confidence": 0.9,
                    },
                },
                "aggregate_confidence": 0.92,
            },
            {
                "cells": {
                    "name": {
                        "value": "Grafana",
                        "source_url": "https://grafana.com/oss/",
                        "evidence_snippet": "Grafana",
                        "confidence": 0.8,
                    },
                    "website_or_repo": {
                        "value": "https://grafana.com/oss/",
                        "source_url": "https://grafana.com/oss/",
                        "evidence_snippet": "https://grafana.com/oss/",
                        "confidence": 0.8,
                    },
                },
                "aggregate_confidence": 0.88,
            },
        ]
        query_spec = {
            "expected_entities": [
                {
                    "name": "Prometheus",
                    "aliases": ["Prometheus.io"],
                    "key_fields": {
                        "website_or_repo": ["github.com/prometheus/prometheus"],
                        "license": ["Apache 2.0"],
                    },
                },
                {
                    "name": "Grafana",
                    "key_fields": {
                        "website_or_repo": ["grafana.com"],
                    },
                },
            ]
        }

        m = _compute_metrics(_make_result(rows, ["name", "website_or_repo", "license"]), query_spec)

        assert m["has_labels"] is True
        assert m["entity_precision"] == 1.0
        assert m["entity_recall"] == 1.0
        assert m["field_accuracy"] == 1.0
        assert m["citation_presence_rate"] == 1.0

    def test_labeled_metrics_penalize_missing_entities_and_fields(self):
        rows = [
            {
                "cells": {
                    "name": _cell("https://example.com", "Prometheus"),
                },
                "aggregate_confidence": 0.6,
            }
        ]
        query_spec = {
            "expected_entities": [
                {"name": "Prometheus", "key_fields": ["website_or_repo"]},
                {"name": "Grafana", "key_fields": ["website_or_repo"]},
            ]
        }

        m = _compute_metrics(_make_result(rows, ["name", "website_or_repo"]), query_spec)

        assert m["entity_precision"] == 1.0
        assert m["entity_recall"] == 0.5
        assert m["field_accuracy"] == 0.0
        assert m["citation_presence_rate"] == 0.0
