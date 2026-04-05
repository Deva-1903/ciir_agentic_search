# AgenticSearch

> Provenance-first entity discovery

AgenticSearch takes a free-text topic query and returns a ranked table of real-world entities with evidence attached to every cell. It is built for reviewer inspection: the UI shows the retrieval plan, phase progress, trust signals, and cell-level provenance, and the API/export paths preserve the same evidence.

For the full iteration log and engineering decisions, see [BUILD_JOURNAL.md](./BUILD_JOURNAL.md).

## What This Does

- Discovers entities for broad, open-ended queries across places, organizations, software projects, products, and people.
- Plans a constrained schema, searches multiple retrieval angles, detects page evidence regimes, and uses deterministic extractors before LLM fallback.
- Merges duplicates, resolves official sites, fills missing fields, verifies cells, ranks rows, and returns structured output with provenance.

## Live Demo

**[https://agentic-search-uij3k.ondigitalocean.app/](https://agentic-search-uij3k.ondigitalocean.app/)**

No setup required — enter any query and explore the results, provenance, and run stats directly.

## Reviewer Path

1. Open the [live demo](https://agentic-search-uij3k.ondigitalocean.app/) or start the app locally at `http://localhost:8000`.
2. Run 2-3 sample queries from the list below.
3. Click cells in the UI to inspect `source_url`, `evidence_snippet`, and `confidence`, or export JSON/CSV.
4. Read [BUILD_JOURNAL.md](./BUILD_JOURNAL.md) for the deeper engineering story and iteration history.

## Try The Demo First

### Quick run

```bash
uv pip install -e ".[dev]"
cp .env.example .env
# set BRAVE_API_KEY and OPENAI_API_KEY in .env
uvicorn app.main:app --reload --port 8000
```

Then open [http://localhost:8000](http://localhost:8000).

### Required config

| Variable               | Required | Purpose                               |
| ---------------------- | -------- | ------------------------------------- |
| `BRAVE_API_KEY`        | Yes      | Web search                            |
| `OPENAI_API_KEY`       | Yes      | Default planner and extractor path    |
| `GROQ_API_KEY`         | No       | Optional alternate/fallback extractor |
| `JS_RENDERING_ENABLED` | No       | Enables selective JS fallback         |
| `JS_RENDER_MAX_PAGES`  | No       | Caps rendered pages per query         |

The app stores its SQLite cache/job database at `/tmp/agentic_search.db`.

## Why This Is Strong

- **Provenance-first output**: every returned cell carries a source URL, evidence snippet, and confidence score.
- **Constrained planning**: the planner selects a structural query family and schema instead of drifting into vague generic tables.
- **Evidence-regime adaptation**: pages are classified as official sites, directories, editorial articles, local-business listings, software repo/docs pages, marketplaces, or unknown.
- **Deterministic before LLM**: repo/docs pages, official pages, and list pages use deterministic or semi-deterministic extractors before falling back to the LLM.
- **Late quality controls**: merge/canonicalization, official-site resolution, gap-fill, cell verification, verifier filtering, and intent-aware ranking all operate on grounded evidence.
- **Generalizable filtering**: CTA/nav text, article-title-shaped names, and LLM placeholder values ("N/A", "Unknown") are rejected before output. Row counts are capped for "best/top" queries.
- **Provider reliability**: a per-provider in-process cooldown prevents churn between a timed-out primary provider and a rate-limited fallback within the same run.
- **Reviewer-facing UX**: the UI exposes the retrieval plan, phase tracker, trust badges, run stats, and evidence modal instead of hiding the pipeline behind a single table.

## Build Journal

[BUILD_JOURNAL.md](./BUILD_JOURNAL.md) is the canonical engineering log for this repo. It contains the iteration history, design changes, attribution, and deeper implementation notes that were intentionally kept out of this README.

## Quickstart

### Requirements

- Python 3.11+
- `uv` recommended, though `pip` also works

### Install

```bash
# with uv
uv pip install -e ".[dev]"

# or with pip
pip install -e ".[dev]"
```

### Run tests

```bash
pytest tests/ -v
```

### Optional settings

- `PLANNER_PROVIDER` and `EXTRACTOR_PROVIDER` choose the provider path.
- `JS_RENDERING_ENABLED=true` turns on the selective JS fallback.
- `JS_RENDER_MAX_PAGES` and `JS_RENDER_TIMEOUT` bound that fallback aggressively.

## Deploy To DigitalOcean App Platform

This repo is prepared for **source-based** deployment on DigitalOcean App Platform. That is the recommended path here: the app is a standard FastAPI service, the frontend is served by the same process, and Docker would add unnecessary moving parts for a reviewer demo.

Deployment files:

- [`app.yaml`](./app.yaml): App Platform spec for a single web service
- [`requirements.txt`](./requirements.txt): runtime dependency list for DigitalOcean's Python buildpack
- [`runtime.txt`](./runtime.txt): pins a stable Python 3.11 runtime for deploys

### Recommended App Platform flow

1. Push this repo to GitHub.
2. In DigitalOcean App Platform, create a new app from that GitHub repo.
3. Either:
   - use [`app.yaml`](./app.yaml) with `doctl apps create --spec app.yaml`, or
   - mirror the same settings manually in the DigitalOcean UI.
4. Set the required environment variables in the DigitalOcean UI:
   - `BRAVE_API_KEY`
   - `OPENAI_API_KEY`
5. Keep these runtime settings:
   - Start command: `sh -c 'uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}'`
   - HTTP port: `8080`
   - Health check path: `/api/health`
6. Redeploy by pushing to the configured GitHub branch if `deploy_on_push` is enabled.

### Notes for this deploy

- The app already exposes a health endpoint at `/api/health`.
- **Single-instance requirement**: Job state is stored in local SQLite at `/tmp/agentic_search.db`. This is per-container. Keep `instance_count: 1` (already set in `app.yaml`) and avoid running rolling deploys while searches are in flight. If a container restarts mid-search, the polling client will receive a 404 and display a clear "server restarted, please search again" message.
- `GROQ_API_KEY`, `OPENAI_BASE_URL`, and JS-render settings are optional. They are not required for the default reviewer demo path.

## Example Queries

- `leading cybersecurity companies in the United States`
- `best modern art museums in Mexico City`
- `top open source observability platforms`
- `digital preservation organizations for research data`
- `notable AI safety researchers`
- `best ergonomic standing desk brands`

## Output And Provenance

The system returns a structured table plus metadata about the run. Each cell is grounded:

- `value`
- `source_url`
- `source_title`
- `evidence_snippet`
- `confidence`

Representative response shape:

```json
{
  "query": "top open source observability platforms",
  "entity_type": "observability platform",
  "columns": [
    "name",
    "website_or_repo",
    "primary_use_case",
    "license",
    "language_or_stack",
    "maintainer_or_org"
  ],
  "rows": [
    {
      "entity_id": "prometheus",
      "cells": {
        "name": {
          "value": "Prometheus",
          "source_url": "https://github.com/prometheus/prometheus",
          "source_title": "prometheus/prometheus",
          "evidence_snippet": "Prometheus",
          "confidence": 0.9
        },
        "website_or_repo": {
          "value": "https://github.com/prometheus/prometheus",
          "source_url": "https://github.com/prometheus/prometheus",
          "source_title": "prometheus/prometheus",
          "evidence_snippet": "https://github.com/prometheus/prometheus",
          "confidence": 0.92
        }
      },
      "aggregate_confidence": 0.91,
      "sources_count": 2,
      "canonical_domain": "github.com"
    }
  ],
  "metadata": {
    "query_family": "software_project",
    "pages_scraped": 12,
    "pages_after_rerank": 10,
    "entities_extracted": 34,
    "gap_fill_used": true,
    "pipeline_counts": {
      "pages_routed_deterministic": 4,
      "pages_routed_hybrid": 3,
      "pages_routed_llm": 3
    }
  }
}
```

In the UI, clicking any populated cell opens the evidence modal with the same provenance fields and trust cues.

## High-Level Architecture

The pipeline is intentionally incremental rather than agent-spaghetti:

1. **Normalize query**: light cleanup before retrieval.
2. **Plan schema**: choose a constrained query family, columns, and multi-angle search facets.
3. **Search**: run Brave queries in parallel and deduplicate URLs.
4. **Scrape**: fetch pages, keep cleaned text plus lightweight HTML metadata, detect evidence regimes, and optionally use a tiny-budget JS fallback.
5. **Rerank**: keep the most query-relevant pages before extraction.
6. **Extract**: use deterministic parsers first, then LLM extraction when needed.
7. **Merge**: deduplicate entities and canonicalize cells.
8. **Resolve official sites + gap-fill**: attach better canonical sites and fill sparse fields with focused follow-up retrieval.
9. **Verify**: run cell-level alignment checks and late row filtering.
10. **Rank + return**: apply intent-aware ranking and return JSON, CSV, and the reviewer-facing UI.

Core implemented capabilities in the current codebase:

- provenance-first entity discovery
- constrained query-family planning
- multi-angle retrieval
- evidence-regime detection
- deterministic extractors before LLM fallback
- merge/canonicalization
- official-site resolution
- gap-fill
- cell verification
- intent-aware ranking
- selective JS fallback
- evaluation harness
- reviewer-facing UI

## Evaluation

The repo includes a lightweight evaluation harness:

```bash
# broader unlabeled eval set
python scripts/eval.py

# only software queries
python scripts/eval.py --category software

# small labeled regression set
python scripts/eval.py --queries docs/eval_labeled_queries.json --labels-only
```

Query sets:

- [`docs/eval_queries.json`](./docs/eval_queries.json): broader unlabeled query mix
- [`docs/eval_labeled_queries.json`](./docs/eval_labeled_queries.json): small labeled regression set

Metrics include:

- proxy metrics such as rows returned, fill rate, official-site rate, multi-source rate, source diversity, and runtime
- labeled metrics such as entity precision-ish, entity recall-ish, field accuracy, and citation presence rate

## Limitations

- The system is still heuristic-heavy; evidence regimes and ranking are interpretable heuristics, not trained classifiers.
- Deterministic extraction is conservative by design, so many pages still fall back to the LLM.
- The selective JS fallback is budgeted and optional, not a full browser-based crawl.
- The labeled evaluation set is intentionally small and useful for regression checks, not broad benchmark claims.
- Latency is still dominated by retrieval, scraping, and extraction; this is not a low-latency interactive search engine.

## Project Structure

```text
BUILD_JOURNAL.md              # canonical build/iteration log
README.md
app/
  api/                        # FastAPI routes
  core/                       # config + logging
  models/                     # Pydantic models + SQLite layer
  services/                   # planner, search, scraper, extraction, ranking, verification
  utils/                      # text/url/dedupe helpers
templates/                    # Jinja2 UI template
static/                       # vanilla JS + CSS reviewer UI
scripts/eval.py               # evaluation harness
docs/eval_queries.json
docs/eval_labeled_queries.json
tests/
```
