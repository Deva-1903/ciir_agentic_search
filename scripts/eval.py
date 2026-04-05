#!/usr/bin/env python3
"""
Lightweight evaluation harness for Agentic Search.

Usage:
    # Full run against a running server (default http://127.0.0.1:8000):
    python scripts/eval.py

    # Point at a different server:
    python scripts/eval.py --base-url http://localhost:9000

    # Run only food queries:
    python scripts/eval.py --category food

    # Ablation: disable reranker via env var injection (requires restart):
    python scripts/eval.py --tag no-rerank

    # Use a custom query file:
    python scripts/eval.py --queries path/to/queries.json

The script expects the Agentic Search server to be running. It submits each
query via POST /api/search, polls until completion, then computes per-query
and aggregate metrics. Results are written to data/eval_<tag>_<timestamp>.json
and a summary CSV to data/eval_<tag>_<timestamp>.csv.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

try:
    import httpx
except ImportError:
    sys.exit(
        "httpx is required for the eval harness. Install with: pip install httpx"
    )

# ── Defaults ──────────────────────────────────────────────────────────────────

_DEFAULT_BASE_URL = "http://127.0.0.1:8000"
_DEFAULT_QUERIES = Path(__file__).resolve().parent.parent / "docs" / "eval_queries.json"
_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data"
_POLL_INTERVAL = 3  # seconds
_POLL_TIMEOUT = 180  # seconds — max wait per query

# Columns considered "actionable" (non-decorative).
_ACTIONABLE_COLS = {
    "website", "homepage", "url", "phone", "phone_number", "email",
    "address", "location", "rating", "price", "price_range", "funding",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _submit_and_poll(client: httpx.Client, base_url: str, query: str) -> dict:
    """Submit a search query and poll until done or timeout."""
    resp = client.post(
        urljoin(base_url, "/api/search"),
        json={"query": query},
        timeout=30,
    )
    resp.raise_for_status()
    job = resp.json()
    job_id = job["job_id"]

    deadline = time.monotonic() + _POLL_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL)
        poll = client.get(
            urljoin(base_url, f"/api/search/{job_id}"),
            timeout=15,
        )
        poll.raise_for_status()
        data = poll.json()
        status = data.get("status", "")
        if status == "done":
            return data
        if status == "failed":
            return {"status": "failed", "error": data.get("error", "unknown")}

    return {"status": "timeout", "error": f"Timed out after {_POLL_TIMEOUT}s"}


def _compute_metrics(result: dict) -> dict[str, Any]:
    """Compute evaluation metrics from a completed search result."""
    if result.get("status") != "done" or not result.get("result"):
        return {
            "status": result.get("status", "unknown"),
            "error": result.get("error", ""),
            "rows_returned": 0,
            "columns": [],
            "avg_cells_per_row": 0.0,
            "fill_rate": 0.0,
            "actionable_rate": 0.0,
            "avg_actionable_fields": 0.0,
            "official_site_rate": 0.0,
            "multi_source_rate": 0.0,
            "avg_aggregate_confidence": 0.0,
            "avg_source_diversity": 0.0,
            "duration_seconds": 0.0,
        }

    sr = result["result"]
    rows = sr.get("rows", [])
    columns = sr.get("columns", [])
    meta = sr.get("metadata", {})

    num_rows = len(rows)
    num_cols = max(len(columns), 1)

    # Per-row stats
    cells_per_row = []
    actionable_counts = []
    official_site_flags = []
    multi_source_flags = []
    agg_confidences = []
    diversities = []

    for row in rows:
        cells = row.get("cells", {})
        filled = len(cells)
        cells_per_row.append(filled)

        # Actionable columns filled
        actionable = sum(
            1 for col in cells if col.lower() in _ACTIONABLE_COLS
        )
        actionable_counts.append(actionable)
        official_site_flags.append(1 if row.get("canonical_domain") else 0)

        # Multi-source: >1 distinct source URL across cells
        source_urls = {
            c.get("source_url", "") for c in cells.values() if isinstance(c, dict)
        }
        multi_source_flags.append(1 if len(source_urls) > 1 else 0)

        agg_confidences.append(row.get("aggregate_confidence", 0.0))

        # Source diversity approximation: fraction of unique domains
        domains = set()
        for c in cells.values():
            if isinstance(c, dict):
                url = c.get("source_url", "")
                if url:
                    try:
                        from urllib.parse import urlparse
                        domains.add(urlparse(url).netloc.lower())
                    except Exception:
                        pass
        total_cells = max(len(cells), 1)
        if domains:
            max_share = max(
                sum(1 for c in cells.values()
                    if isinstance(c, dict) and urlparse(c.get("source_url", "")).netloc.lower() == d)
                for d in domains
            ) / total_cells
            diversities.append(1 - max_share)
        else:
            diversities.append(0.0)

    return {
        "status": "done",
        "error": "",
        "rows_returned": num_rows,
        "columns": columns,
        "avg_cells_per_row": round(statistics.mean(cells_per_row), 2) if cells_per_row else 0.0,
        "fill_rate": round(
            statistics.mean(c / num_cols for c in cells_per_row), 3
        ) if cells_per_row else 0.0,
        "actionable_rate": round(
            sum(1 for a in actionable_counts if a > 0) / max(num_rows, 1), 3
        ),
        "avg_actionable_fields": round(
            statistics.mean(actionable_counts), 2
        ) if actionable_counts else 0.0,
        "official_site_rate": round(
            statistics.mean(official_site_flags), 3
        ) if official_site_flags else 0.0,
        "multi_source_rate": round(
            statistics.mean(multi_source_flags), 3
        ) if multi_source_flags else 0.0,
        "avg_aggregate_confidence": round(
            statistics.mean(agg_confidences), 3
        ) if agg_confidences else 0.0,
        "avg_source_diversity": round(
            statistics.mean(diversities), 3
        ) if diversities else 0.0,
        "duration_seconds": meta.get("duration_seconds", 0.0),
        "pages_scraped": meta.get("pages_scraped", 0),
        "pages_after_rerank": meta.get("pages_after_rerank"),
        "rerank_scorer": meta.get("rerank_scorer"),
        "entities_extracted": meta.get("entities_extracted", 0),
        "entities_after_merge": meta.get("entities_after_merge", 0),
        "gap_fill_used": meta.get("gap_fill_used", False),
        "query_family": meta.get("query_family", ""),
        "normalized_query": meta.get("normalized_query", ""),
        "pipeline_counts": meta.get("pipeline_counts", {}),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Agentic Search evaluation harness")
    parser.add_argument("--base-url", default=_DEFAULT_BASE_URL, help="Server base URL")
    parser.add_argument("--queries", default=str(_DEFAULT_QUERIES), help="Path to eval queries JSON")
    parser.add_argument("--category", default=None, help="Filter queries by category")
    parser.add_argument("--tag", default="full", help="Tag for this eval run (e.g. no-rerank)")
    args = parser.parse_args()

    queries_path = Path(args.queries)
    if not queries_path.exists():
        sys.exit(f"Query file not found: {queries_path}")

    with open(queries_path) as f:
        queries = json.load(f)

    if args.category:
        queries = [q for q in queries if q.get("category") == args.category]

    if not queries:
        sys.exit("No queries to evaluate after filtering.")

    print(f"Eval run: tag={args.tag}  queries={len(queries)}  server={args.base_url}")
    print("-" * 72)

    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    results = []
    client = httpx.Client()

    for i, q in enumerate(queries, 1):
        qid = q["id"]
        query_text = q["query"]
        print(f"[{i}/{len(queries)}] {qid}: {query_text}")

        t0 = time.monotonic()
        try:
            raw_result = _submit_and_poll(client, args.base_url, query_text)
        except Exception as exc:
            raw_result = {"status": "error", "error": str(exc)}

        elapsed = round(time.monotonic() - t0, 1)
        metrics = _compute_metrics(raw_result)
        metrics["query_id"] = qid
        metrics["query"] = query_text
        metrics["category"] = q.get("category", "")
        metrics["wall_time"] = elapsed

        status_icon = "OK" if metrics["status"] == "done" else "FAIL"
        print(
            f"  {status_icon}  rows={metrics['rows_returned']}  "
            f"fill={metrics['fill_rate']:.0%}  "
            f"actionable={metrics['actionable_rate']:.0%}  "
            f"official={metrics['official_site_rate']:.0%}  "
            f"multi_src={metrics['multi_source_rate']:.0%}  "
            f"conf={metrics['avg_aggregate_confidence']:.2f}  "
            f"diversity={metrics['avg_source_diversity']:.2f}  "
            f"time={elapsed}s"
        )
        results.append(metrics)

    client.close()

    # ── Aggregate summary ─────────────────────────────────────────────────────
    done = [r for r in results if r["status"] == "done"]
    agg = {}
    if done:
        agg = {
            "queries_total": len(results),
            "queries_succeeded": len(done),
            "queries_failed": len(results) - len(done),
            "avg_rows": round(statistics.mean(r["rows_returned"] for r in done), 1),
            "avg_fill_rate": round(statistics.mean(r["fill_rate"] for r in done), 3),
            "avg_actionable_rate": round(statistics.mean(r["actionable_rate"] for r in done), 3),
            "avg_actionable_fields": round(statistics.mean(r["avg_actionable_fields"] for r in done), 2),
            "avg_official_site_rate": round(statistics.mean(r["official_site_rate"] for r in done), 3),
            "avg_multi_source_rate": round(statistics.mean(r["multi_source_rate"] for r in done), 3),
            "avg_confidence": round(statistics.mean(r["avg_aggregate_confidence"] for r in done), 3),
            "avg_diversity": round(statistics.mean(r["avg_source_diversity"] for r in done), 3),
            "avg_duration": round(statistics.mean(r["duration_seconds"] for r in done), 1),
            "median_duration": round(statistics.median(r["duration_seconds"] for r in done), 1),
        }

    print("\n" + "=" * 72)
    print("AGGREGATE SUMMARY")
    print("=" * 72)
    for k, v in agg.items():
        print(f"  {k:30s} = {v}")

    # ── Write outputs ─────────────────────────────────────────────────────────
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    tag = args.tag.replace(" ", "_")

    json_path = _OUTPUT_DIR / f"eval_{tag}_{ts}.json"
    with open(json_path, "w") as f:
        json.dump({"tag": tag, "timestamp": ts, "aggregate": agg, "results": results}, f, indent=2)
    print(f"\nJSON report: {json_path}")

    csv_path = _OUTPUT_DIR / f"eval_{tag}_{ts}.csv"
    if results:
        fieldnames = [
            "query_id", "query", "category", "status", "rows_returned",
            "avg_cells_per_row", "fill_rate", "actionable_rate", "avg_actionable_fields",
            "official_site_rate",
            "multi_source_rate", "avg_aggregate_confidence",
            "avg_source_diversity", "duration_seconds", "wall_time",
            "pages_scraped", "pages_after_rerank", "rerank_scorer",
            "entities_extracted", "entities_after_merge", "gap_fill_used",
            "query_family", "normalized_query",
        ]
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(results)
        print(f"CSV  report: {csv_path}")

    print("\nDone.")


if __name__ == "__main__":
    main()
