# AgenticSearch

> **Provenance-first entity discovery via multi-angle web search**

Given a free-text topic query, AgenticSearch discovers, structures, and verifies a table of real-world entities — with every cell traceable to its source URL, evidence snippet, and confidence score.

---

## One-line summary

Submit a query → get a ranked table of entities where every cell cites the web page it came from.

---

## Challenge fit

The CIIR Agentic Search Challenge targets systems that go beyond single-shot retrieval to perform multi-step, evidence-grounded information gathering. This system addresses that directly:

- **Agentic pipeline**: query → planning → multi-angle search → scraping → extraction → merge → gap-fill → structured output
- **Evidence grounding**: every cell in the output table has a source URL, verbatim evidence snippet, and confidence score
- **Dynamic schema**: the schema (entity type and columns) is inferred per query — not hardcoded per domain
- **Targeted gap-fill**: a post-extraction enrichment pass fills sparse cells with focused follow-up searches

---

## Architecture overview

```
Query
  │
  ▼
┌─────────────┐
│   Planner   │  LLM infers: entity_type, columns (5–8), search_angles (3–5)
└──────┬──────┘
       │
       ▼
┌─────────────┐
│ Brave Search│  Parallel async requests for each search angle
└──────┬──────┘  Deduplicates URLs, filters junk
       │
       ▼
┌─────────────┐
│   Scraper   │  Async fetch with SQLite cache
└──────┬──────┘  trafilatura → BeautifulSoup fallback
       │
       ▼
┌─────────────┐
│  Extractor  │  LLM structured extraction per page (with chunking)
└──────┬──────┘  Returns: entity_name + cells + evidence_snippet + confidence
       │
       ▼
┌─────────────┐
│   Merger    │  Fuzzy dedup via rapidfuzz + domain matching
└──────┬──────┘  Best-confidence cell wins per column
       │
       ▼
┌─────────────┐
│   Ranker    │  Score = completeness + avg_confidence + source_support + has_website
└──────┬──────┘
       │
       ▼
┌─────────────┐
│  Gap-fill   │  Top-3 sparse rows → targeted Brave queries → scrape → extract
└──────┬──────┘  Fills only missing columns (bounded: max 3 entities × 2 URLs)
       │
       ▼
  Structured JSON response + interactive UI table
```

### Module map

```
app/
  api/
    routes_search.py   # POST /api/search, GET /api/search/{job_id}
    routes_export.py   # GET /api/export/json|csv
  core/
    config.py          # pydantic-settings, all env vars
    logging.py         # structured logging setup
  models/
    schema.py          # Pydantic models for every pipeline stage
    db.py              # SQLite async layer (aiosqlite)
  services/
    llm.py             # OpenAI-compatible client + retry logic
    planner.py         # Schema planning prompt
    brave_search.py    # Brave Search API, parallel async
    scraper.py         # Async fetcher + trafilatura/BS4
    extractor.py       # LLM extraction with chunking
    merger.py          # Fuzzy entity merge
    ranker.py          # Scoring and ranking
    gap_fill.py        # Targeted enrichment (stretch feature)
    exporter.py        # JSON + CSV export helpers
  utils/
    url.py             # URL normalization, filtering, dedup
    text.py            # Chunking, token estimation, normalize_name
    dedupe.py          # rapidfuzz wrappers for entity matching
  main.py              # FastAPI app + lifespan
templates/
  index.html           # Single-page Jinja2 template
static/
  app.js               # Vanilla JS: polling, table render, modal
  style.css            # Dark theme, responsive
tests/                 # pytest test suite
data/                  # SQLite database (created at runtime)
```

---

## Additional Feauters Implemented

### 1. Dynamic schema inference

No schemas are hardcoded per domain. For "AI startups in healthcare" the LLM infers columns like `focus_area`, `funding_stage`, `notable_claim`. For "pizza places in Brooklyn" it infers `cuisine_type`, `price_range`, `neighborhood`. The planner also generates 3–5 diversified search angles (e.g., list pages + official sites + news) to improve recall over a single query.

### 2. Per-cell provenance

Every cell in the output table stores:

- `value` — the extracted string
- `source_url` — the page it came from
- `source_title` — the page's title
- `evidence_snippet` — a verbatim or near-verbatim excerpt from the page that supports the value
- `confidence` — a 0–1 score from the LLM reflecting how explicitly the value was stated

This makes the system auditable: a user can click any cell to verify exactly where the value came from.

### 3. Targeted gap-fill (stretch feature)

After the initial extraction and merge pass, the system identifies the top-N entities with the most missing columns. For each, it generates focused queries (e.g., `"Tempus AI headquarters"`, `"Tempus AI official website about"`) and runs a small supplementary search+scrape+extract cycle to fill only the missing cells. This is bounded to 3 entities × 2 URLs × 1 round to keep latency and cost reasonable.

### 4. Fuzzy entity deduplication

Entities extracted from different pages that refer to the same real-world entity are merged. Matching uses:

- RapidFuzz `token_set_ratio` on normalized names (handles "OpenAI" vs "OpenAI Inc")
- Domain matching on website URLs (strong signal)
- When merging: the highest-confidence cell per column wins

---

## Setup

### Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip

### Install

```bash
# With uv (fast)
uv pip install -e ".[dev]"

# Or with pip
pip install -e ".[dev]"
```

### Environment variables

Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
```

| Variable          | Required | Description                                                |
| ----------------- | -------- | ---------------------------------------------------------- |
| `BRAVE_API_KEY`   | ✅       | From [brave.com/search/api](https://brave.com/search/api/) |
| `OPENAI_API_KEY`  | ✅       | OpenAI or compatible provider                              |
| `OPENAI_MODEL`    | optional | Default: `gpt-4o-mini`                                     |
| `OPENAI_BASE_URL` | optional | For non-OpenAI providers (e.g., Groq, Together)            |
| `APP_ENV`         | optional | `development` or `production`                              |
| `LOG_LEVEL`       | optional | `INFO` (default) or `DEBUG`                                |

---

## How to run

### Backend

```bash
uvicorn app.main:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000) in your browser.

The SQLite database is created automatically at `data/agentic_search.db` on first run.

### Tests

```bash
pytest tests/ -v
```

---

## Example queries

| Query                            | Entity type | Sample columns inferred                                               |
| -------------------------------- | ----------- | --------------------------------------------------------------------- |
| `AI startups in healthcare`      | startup     | name, website, headquarters, focus_area, funding_stage, notable_claim |
| `top pizza places in Brooklyn`   | restaurant  | name, address, cuisine_type, price_range, neighborhood, rating        |
| `open source database tools`     | tool        | name, website, language, license, github_stars, primary_use_case      |
| `climate tech startups series A` | startup     | name, website, focus_area, funding_amount, investors, founded         |

---

## API

### POST `/api/search`

Submit a query. Returns a `job_id` immediately.

```json
{ "query": "AI startups in healthcare" }
```

Response (`202 Accepted`):

```json
{ "job_id": "abc-123", "status": "pending", "phase": "queued" }
```

### GET `/api/search/{job_id}`

Poll for job status. When `status = "done"`, the full result is included.

```json
{
  "job_id": "abc-123",
  "status": "done",
  "phase": "done",
  "result": {
    "query_id": "abc-123",
    "query": "AI startups in healthcare",
    "entity_type": "startup",
    "columns": [
      "name",
      "website",
      "headquarters",
      "focus_area",
      "funding_stage",
      "notable_claim"
    ],
    "rows": [
      {
        "entity_id": "tempus",
        "cells": {
          "name": {
            "value": "Tempus",
            "source_url": "https://techcrunch.com/...",
            "source_title": "TechCrunch",
            "evidence_snippet": "Tempus is an AI company advancing precision medicine...",
            "confidence": 0.96
          }
        },
        "aggregate_confidence": 0.91,
        "sources_count": 3
      }
    ],
    "metadata": {
      "search_angles": ["top AI healthcare startups 2024", "..."],
      "urls_considered": 24,
      "pages_scraped": 15,
      "entities_extracted": 42,
      "entities_after_merge": 11,
      "gap_fill_used": true,
      "duration_seconds": 18.2
    }
  }
}
```

### GET `/api/export/json?query_id={id}`

Download the full result as JSON.

### GET `/api/export/csv?query_id={id}`

Download a flattened CSV with columns like:
`name, name_source_url, name_confidence, website, website_source_url, ...`

### GET `/api/health`

Returns `{ "status": "ok" }`.

---

## Design decisions and tradeoffs

**Job-based async model**: The pipeline takes 15–60 seconds, so the UI submits a job and polls every 2 seconds. This avoids HTTP timeouts and keeps the frontend simple.

**FastAPI + Jinja2 + vanilla JS**: Chosen over React to eliminate a build step and keep the frontend deployable as static files. The entire UI is ~200 lines of JS with no dependencies.

**trafilatura-first extraction**: trafilatura produces clean prose text (removes nav, ads, etc.), which significantly improves LLM extraction quality. BeautifulSoup is a fallback for pages trafilatura cannot handle.

**LLM in JSON mode, not function-calling**: Using `response_format={"type": "json_object"}` is broadly compatible with OpenAI-compatible APIs (Groq, Together, Mistral, etc.) while structured function calling varies more across providers.

**Text chunking, not summarization**: Long pages are chunked and each chunk is extracted independently, then merged. This preserves faithful evidence snippets; summarization would lose verbatim quotes.

**Ranker simplicity**: The ranking formula is a weighted sum of four interpretable signals. More complex ranking (BM25 against query, embedding similarity) was intentionally omitted — the completeness and confidence signals are already well-correlated with quality in practice.

---

## Known limitations

- **LLM hallucination**: Despite strong prompt constraints, the extractor may occasionally assign `confidence > 0` to values weakly implied by context. The evidence snippet requirement reduces this significantly.
- **Dynamic pages**: JavaScript-rendered pages (SPAs) are not scraped; the system fetches static HTML only. This misses some sources.
- **Rate limits**: Running many queries quickly may hit Brave API rate limits (depends on your plan).
- **Latency**: A typical query takes 20–50 seconds depending on the number of pages and LLM speed. `gpt-4o-mini` is fast; larger models increase quality but also latency.
- **Schema quality**: The planner occasionally produces generic column names. This could be improved with few-shot examples.

---

## What was intentionally cut

- **Browser automation** (Playwright/Selenium): adds significant complexity for marginal gain on most queries.
- **Vector database / embeddings**: the dataset per query is small enough (10–50 rows) that fuzzy string matching outperforms embedding retrieval and avoids an extra dependency.
- **User accounts / auth**: not relevant for a research submission.
- **Recursive refinement loops**: gap-fill is bounded to 1 round. An unbounded loop would be hard to reason about and expensive.
- **Multi-agent orchestration**: the pipeline is a linear DAG. Each stage is a focused async function, not a separate agent. This is simpler to debug and more reliable.

---

## Latency and cost

For `gpt-4o-mini` and 15 scraped pages:

- Planner: ~0.5s, ~200 tokens
- Extractor: ~1–2s per page, ~1500 tokens per chunk — the dominant cost
- Gap-fill: adds ~5–15s and 2–4 extra LLM calls for sparse rows

Estimated token cost per query: **~30k–80k tokens** with `gpt-4o-mini` (~$0.01–$0.03).

---

## Notes on provenance and trust

Provenance in this system operates at the **cell level**. Each attribute value carries:

1. The URL it was extracted from (verifiable by the user)
2. The snippet of text that supported the extraction (auditable)
3. A confidence score (the LLM's self-reported certainty)

The confidence score is not calibrated by a held-out dataset; it reflects the LLM's estimate of how explicitly the value was stated. Values backed by multiple independent sources (tracked via `sources_count`) are more trustworthy than single-source values.

The extractor prompt explicitly instructs the LLM to omit values not supported by the page content, which reduces hallucination but does not eliminate it entirely.

---

## Notes on gap-fill refinement

Gap-fill is triggered after the initial merge. The `find_sparse_rows()` function in `ranker.py` selects the top-N rows with the most missing columns. For each:

1. `gap_fill.py` generates 1–2 focused queries, prioritizing high-value missing columns (`website`, `headquarters`, `funding_stage`).
2. A fresh Brave search is run for these queries.
3. Up to 2 pages are scraped and run through the same LLM extractor.
4. Only cells for **missing columns** are accepted; already-filled cells are not overwritten unless the new evidence has higher confidence.

This design keeps gap-fill cheap and safe — it cannot degrade existing high-quality cells.

---

## Future improvements

- **Calibrated confidence**: Fine-tune confidence scores against a labeled dataset of correct/incorrect extractions.
- **Source credibility weighting**: Prefer values from authoritative domains (official sites, established news outlets).
- **Incremental streaming**: Stream extracted rows to the UI as they arrive rather than waiting for the full pipeline.
- **JS rendering fallback**: Add optional Playwright scraping for JS-heavy pages.
- **Multi-round refinement**: Allow controlled recursive gap-fill for very sparse tables.
- **Query caching**: Reuse results for near-duplicate queries.
