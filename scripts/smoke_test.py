#!/usr/bin/env python3
"""Quick smoke test: submit a query and trace pipeline counts."""
import httpx
import json
import sys
import time

BASE = "http://127.0.0.1:8000"
QUERY = sys.argv[1] if len(sys.argv) > 1 else "open source database tools"

def main():
    print(f"Query: {QUERY!r}")
    r = httpx.post(f"{BASE}/api/search", json={"query": QUERY}, timeout=10)
    print(f"Submit: HTTP {r.status_code}")
    data = r.json()
    job_id = data["job_id"]
    print(f"Job ID: {job_id}")

    for i in range(90):
        time.sleep(2)
        r = httpx.get(f"{BASE}/api/search/{job_id}", timeout=10)
        d = r.json()
        status = d["status"]
        phase = d.get("phase", "?")
        print(f"  Poll {i+1}: status={status} phase={phase}")
        if status in ("done", "failed"):
            if status == "done" and d.get("result"):
                res = d["result"]
                rows = res.get("rows", [])
                meta = res.get("metadata", {})
                print(f"  RESULT: entity_type={res.get('entity_type')} columns={res.get('columns')} rows={len(rows)}")
                print(f"  META: urls_considered={meta.get('urls_considered')} "
                      f"pages_scraped={meta.get('pages_scraped')} "
                      f"pages_after_rerank={meta.get('pages_after_rerank')} "
                      f"entities_extracted={meta.get('entities_extracted')} "
                      f"entities_after_merge={meta.get('entities_after_merge')} "
                      f"gap_fill={meta.get('gap_fill_used')} "
                      f"duration={meta.get('duration_seconds')}")
                counts = meta.get("pipeline_counts") or {}
                if counts:
                    print(f"  COUNTS: {json.dumps(counts, sort_keys=True)}")
                if rows:
                    for ri, row in enumerate(rows[:3]):
                        print(f"  Row {ri}: entity_id={row.get('entity_id')} "
                              f"sources={row.get('sources_count')} "
                              f"conf={row.get('aggregate_confidence')} "
                              f"cells={list(row.get('cells', {}).keys())}")
                else:
                    print("  >>> ZERO ROWS <<<")
            elif status == "failed":
                print(f"  ERROR: {d.get('error')}")
            break
    else:
        print("  TIMEOUT waiting for job")

if __name__ == "__main__":
    main()
