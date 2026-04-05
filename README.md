# AgenticSearch

> **Provenance-first entity discovery via multi-angle web search**

Given a free-text topic query, AgenticSearch discovers, structures, and verifies a table of real-world entities — with every cell traceable to its source URL, evidence snippet, and confidence score.

---

## One-line summary

Submit a query → get a ranked table of entities where every cell cites the web page it came from.

---

## Challenge fit

The CIIR Agentic Search Challenge targets systems that go beyond single-shot retrieval to perform multi-step, evidence-grounded information gathering. This system addresses that directly:

- **Agentic pipeline**: query → planning → multi-angle search → scraping → reranking → extraction → merge → prune → cell verification → rank → gap-fill → verify → structured output
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
│   Planner   │  LLM infers: entity_type, columns (5–8), typed retrieval facets
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
│  Reranker   │  Cross-encoder (ms-marco-MiniLM-L-6-v2) or Jaccard fallback
└──────┬──────┘  Focuses extraction budget on top-K relevant pages
       │
       ▼
┌─────────────┐
│  Extractor  │  LLM extraction per page + field validation at cell boundary
└──────┬──────┘  Retries on a secondary provider if the primary extractor fails
       │
       ▼
┌─────────────┐
│   Merger    │  Fuzzy dedup via rapidfuzz + domain matching
└──────┬──────┘  Best-confidence cell wins per column
       │
       ▼
┌─────────────┐
│   Pruner    │  Drops low-information rows (name-only, weak-signal columns only)
└──────┬──────┘
       │
       ▼
┌─────────────┐
│Cell Verifier│  Per-cell entity-alignment check (fuzzy name in evidence/title/domain)
└──────┬──────┘  Penalizes misaligned cells (0.6× confidence) rather than deleting
       │
       ▼
┌─────────────┐
│   Ranker    │  Score = completeness + confidence + source_quality + source_support
└──────┬──────┘         + actionable + source_diversity (6 weighted components)
       │
       ▼
┌─────────────┐
│  Gap-fill   │  Top-3 sparse rows → targeted Brave queries → scrape → extract
└──────┬──────┘  Fills only missing columns (bounded: max 3 entities × 2 URLs)
       │
       ▼
┌─────────────┐
│  Verifier   │  Drops marketplace-only rows on strict queries; filters low-trust sparse rows
└──────┬──────┘  Falls back to original set if all rows would be removed
       │
       ▼
┌─────────────┐
│  Prune+Rank │  Final prune and re-rank after enrichment
└──────┬──────┘
       │
       ▼
  Structured JSON response + interactive UI table + debug metadata
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
    llm.py             # LLM client wrapper — dual-provider (OpenAI + Groq) with per-stage routing
    planner.py         # Schema planning prompt
    brave_search.py    # Brave Search API, parallel async
    scraper.py         # Async fetcher + trafilatura/BS4
    extractor.py       # LLM extraction with chunking + provider fallback
    merger.py          # Fuzzy entity merge
    reranker.py        # Cross-encoder reranking (+ Jaccard fallback)
    ranker.py          # Scoring, ranking, and row pruning
    gap_fill.py        # Targeted enrichment (stretch feature)
    source_quality.py  # Heuristic source classification (official/editorial/directory/marketplace)
    cell_verifier.py   # Per-cell entity-alignment check
    field_validator.py # URL/phone/rating normalization at extraction boundary
    verifier.py        # Final row filter before ranking
    exporter.py        # JSON + CSV export helpers
  utils/
    url.py             # URL normalization, filtering, dedup
    text.py            # Chunking, token estimation, normalize_name
    dedupe.py          # rapidfuzz wrappers for entity matching
  main.py              # FastAPI app + lifespan
templates/
  index.html           # Single-page Jinja2 template
static/
  app.js               # Vanilla JS: polling, phase tracker, panels, table with trust badges, modal
  style.css            # Dark theme, responsive, pipeline tracker, badge system
tests/                 # pytest test suite (142 tests)
scripts/
  eval.py              # Evaluation harness (CLI)
docs/
  BUILD_JOURNAL.md     # Full development journal (20 iterations)
  eval_queries.json    # Eval query set (10 queries, 3 categories)
data/                  # Eval reports (created at runtime)
```

---

## Additional Features Implemented

### 1. Dynamic schema inference with typed retrieval facets

No schemas are hardcoded per domain. For "AI startups in healthcare" the LLM infers columns like `focus_area`, `funding_stage`, `notable_claim`. For "pizza places in Brooklyn" it infers `cuisine_type`, `price_range`, `neighborhood`. The planner generates typed retrieval facets (`entity_list`, `official_source`, `editorial_review`, `attribute_specific`, `news_recent`, `comparison`) instead of free-form paraphrase angles. Each facet declares which columns it expects to help fill, giving downstream stages a structured retrieval intent.

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

### 5. Source quality scoring

Every row is scored on the trustworthiness of its evidence sources, not just extraction confidence. Sources are classified as `official` (entity's own site), `editorial` (nytimes, theinfatuation, eater, etc.), `directory` (yelp, tripadvisor), `marketplace` (ubereats, doordash), or `unknown`. The `source_quality` score is a confidence-weighted average across all cells and feeds directly into ranking.

### 6. Evidence-based row verification

Before final ranking, a verifier pass removes rows that would not be useful to a user. For strict queries ("top", "best", "leading"), marketplace-only rows are dropped. Rows with very low source quality and few cells are also filtered. The verifier always falls back to the original set if everything would be removed.

### 7. Cross-encoder reranking

After scraping, pages are reranked by query relevance before extraction. A cross-encoder model (`cross-encoder/ms-marco-MiniLM-L-6-v2`) scores each page against the original query and keeps only the top-K most relevant. This focuses the extraction LLM budget on pages most likely to contain useful entity data. If the cross-encoder fails to load (e.g., no GPU, missing dependency), a Jaccard token-overlap scorer is used as fallback.

### 8. Cell-level entity verification

After merge and again after gap-fill, every cell is checked for entity alignment: does the evidence snippet, source title, or source domain actually refer to the entity the row is assigned to? Cells that fail all three checks get a 0.6× confidence penalty. This catches the "right row, wrong fact" failure where gap-fill or multi-entity pages introduce cells from a co-mentioned entity.

### 9. Field validation at the extraction boundary

Before cells enter the pipeline, a rule-based validator normalizes and filters by column type:

- **Website**: adds `https://`, validates TLD presence, rejects bare words like `robertaspizza`
- **Phone**: requires ≥7 digits
- **Rating**: requires a number in [0, 10]

Malformed cells are dropped silently; the extractor proceedes with structurally valid data only.

### 10. Source diversity in ranking

The ranker includes a source-diversity component: rows assembled from multiple independent domains score higher than rows where one domain contributed all cells. This is a tie-breaker (0.08 weight), not a gate — a single authoritative official source still ranks well via the dominant source_quality weight (0.32).

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

| Variable             | Required | Description                                                |
| -------------------- | -------- | ---------------------------------------------------------- |
| `BRAVE_API_KEY`      | ✅       | From [brave.com/search/api](https://brave.com/search/api/) |
| `OPENAI_API_KEY`     | ✅       | OpenAI API key (used for planning)                         |
| `OPENAI_MODEL`       | optional | Default: `gpt-4o-mini`                                     |
| `GROQ_API_KEY`       | ✅       | Groq API key (used for extraction)                         |
| `GROQ_MODEL`         | optional | Default: `llama-3.3-70b-versatile`                         |
| `GROQ_BASE_URL`      | optional | Default: `https://api.groq.com/openai/v1`                  |
| `PLANNER_PROVIDER`   | optional | Default: `openai` — which provider the planner uses        |
| `EXTRACTOR_PROVIDER` | optional | Default: `groq` — which provider the extractor uses        |
| `OPENAI_BASE_URL`    | optional | For non-OpenAI providers                                   |
| `APP_ENV`            | optional | `development` or `production`                              |
| `LOG_LEVEL`          | optional | `INFO` (default) or `DEBUG`                                |

The system uses a **split-provider** model: OpenAI handles schema planning (higher reliability for structured reasoning) while Groq handles entity extraction (faster inference for bulk page processing). Both providers use the same OpenAI-compatible client internally. If the primary extractor provider fails and another configured provider is available, extraction retries on the secondary provider instead of silently returning zero entities.

---

## How to run

### Backend

```bash
uvicorn app.main:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000) in your browser.

The SQLite job/cache database is created automatically at `/tmp/agentic_search.db` on first run. Evaluation reports are still written under `data/`.

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
      "facets": [
        {
          "type": "entity_list",
          "query": "top AI healthcare startups 2024",
          "expected_fill_columns": ["name", "focus_area"],
          "rationale": "..."
        }
      ],
      "urls_considered": 24,
      "pages_scraped": 15,
      "pages_after_rerank": 10,
      "rerank_scorer": "cross_encoder",
      "entities_extracted": 42,
      "entities_after_merge": 11,
      "gap_fill_used": true,
      "duration_seconds": 18.2,
      "pipeline_counts": {
        "search_angles": 5,
        "search_facets": 5,
        "urls_after_dedupe": 24,
        "pages_scraped": 15,
        "pages_after_rerank": 10,
        "extraction_calls": 18,
        "entities_before_merge": 42,
        "rows_after_merge": 14,
        "rows_after_verifier": 11,
        "final_rows": 11
      }
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

**FastAPI + Jinja2 + vanilla JS**: Chosen over React to eliminate a build step and keep the frontend deployable as static files. No framework dependencies.

**trafilatura-first extraction**: trafilatura produces clean prose text (removes nav, ads, etc.), which significantly improves LLM extraction quality. BeautifulSoup is a fallback for pages trafilatura cannot handle.

**LLM in JSON mode, not function-calling**: Using `response_format={"type": "json_object"}` is broadly compatible with OpenAI-compatible APIs (Groq, Together, Mistral, etc.) while structured function calling varies more across providers.

**Text chunking, not summarization**: Long pages are chunked and each chunk is extracted independently, then merged. This preserves faithful evidence snippets; summarization would lose verbatim quotes.

**Extractor provider fallback**: Broad discovery queries should not collapse to zero rows just because one extraction provider is rate-limited. The extractor tries the configured primary provider first and retries on a configured secondary provider when needed. This favors correctness over minimum latency.

**Ranker design**: The ranking formula is a weighted sum of six interpretable signals: completeness (0.25), average confidence (0.20), source quality (0.32), source support (0.08), actionable-field bonus (0.07), and source diversity (0.08). Source quality dominates by design. More complex ranking (BM25 against query, embedding similarity) was intentionally omitted — the signals are already well-correlated with quality and remain fully explainable.

---

## Known limitations

- **LLM hallucination**: Despite strong prompt constraints, the extractor may occasionally assign `confidence > 0` to values weakly implied by context. The evidence snippet requirement and cell-level verification reduce this but do not eliminate it.
- **Dynamic pages**: JavaScript-rendered pages (SPAs) are not scraped; the system fetches static HTML only. This misses some sources.
- **Rate limits**: Running many queries quickly may hit Brave or Groq rate limits (depends on your plan). When Groq throttles, extraction can fall back to OpenAI if configured, which preserves results but increases latency and cost.
- **Latency**: A typical query takes 15–45 seconds with Groq extraction (25–60s with OpenAI extraction) depending on page count, reranking, and LLM speed. The cross-encoder adds ~1-2s; gap-fill adds 10–20s. Extractor fallback during provider throttling can push latency higher.
- **Schema quality**: Typed facets improved planner output, but very broad queries still occasionally produce generic columns.
- **Cell verifier false matches**: Short entity names (e.g. "Joe's") can fuzzy-match against unrelated evidence. The 80-threshold partial_ratio mitigates but does not eliminate this.
- **Source domain calibration**: `source_quality.py` is calibrated for food/startup queries. Medical, legal, and academic domains get a neutral `unknown` score (0.55).
- **No ground-truth evaluation**: The eval harness measures proxy signals (fill rate, multi-source rate, diversity) — not precision/recall against labeled data.

---

## What was intentionally cut

- **Browser automation** (Playwright/Selenium): adds significant complexity for marginal gain on most queries.
- **Vector database / embeddings**: the dataset per query is small enough (10–50 rows) that fuzzy string matching outperforms embedding retrieval and avoids an extra dependency.
- **User accounts / auth**: not relevant for a research submission.
- **Recursive refinement loops**: gap-fill is bounded to 1 round. An unbounded loop would be hard to reason about and expensive.
- **Multi-agent orchestration**: the pipeline is a linear DAG. Each stage is a focused async function, not a separate agent. This is simpler to debug and more reliable.

---

## Latency and cost

With the default split-provider setup (`gpt-4o-mini` planner + `llama-3.3-70b-versatile` extractor) and 15 scraped pages:

- Planner: ~0.3–1.0s
- Extractor: ~0.5–1.5s per page, ~1500 tokens per chunk — the dominant cost
- Gap-fill: adds ~5–15s and 2–4 extra LLM calls for sparse rows

Groq provides significantly faster extraction inference than hosted OpenAI models, but its free tier may apply stricter rate limits. Estimated token cost per query is still **~30k–80k tokens**. When extractor fallback to OpenAI is triggered, total latency and spend rise, but broad queries continue to return rows instead of failing closed with empty output.

With OpenAI fallback (`gpt-4o-mini`): similar token counts, ~$0.01–$0.03 per query.

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

## Evaluation

A lightweight evaluation harness is included in `scripts/eval.py`. It drives the running server with a set of diverse queries and produces per-query and aggregate metrics:

```bash
# Run all 10 eval queries
python scripts/eval.py

# Run only food queries
python scripts/eval.py --category food

# Tag a run for comparison (e.g., after disabling reranker)
python scripts/eval.py --tag no-rerank
```

Results are saved to `data/eval_<tag>_<timestamp>.json` and `.csv`. Metrics include: rows returned, fill rate, actionable rate, multi-source rate, average confidence, source diversity, and duration.

See `docs/eval_queries.json` for the query set (10 queries across food, tech, and travel categories).

---

## Why these improvements were prioritized

The improvement phases were ordered by practical impact on output quality:

1. **Facet-typed planning** (Phase 4): The planner drives everything downstream. Replacing paraphrase angles with typed facets gives the system structured retrieval intent — entity lists for recall, official sources for trust, editorial for context. This is the highest-leverage change because every later stage benefits from better input pages.

2. **Cross-encoder reranking** (Phase 4): Scraping is expensive (network I/O + LLM tokens). Reranking the scraped pages by query relevance before extraction focuses the token budget on pages that actually contain the target entities. The cross-encoder is free (local model), and the Jaccard fallback ensures the pipeline works without GPU.

3. **Cell verification + field validation + source diversity** (Phase 5): These close the gap between row-level quality (which was already addressed by source_quality + verifier) and cell-level integrity. The cell verifier catches cross-entity contamination; field validation catches malformed values; diversity scoring rewards independent corroboration.

4. **Evaluation harness** (Phase 6): Without metrics, everything above is validated by unit tests and gut feel. The eval harness makes quality measurable — even if the metrics are proxy signals rather than ground truth.

All improvements are heuristic. Source quality classification uses hand-curated domain lists, not a trained classifier. Cell verification uses fuzzy string matching, not a semantic model. The evaluation harness measures fill rate and diversity, not factual accuracy. These are practical engineering choices for a system that needs to work across arbitrary domains without labeled training data.

---

## Why the UI is designed this way

The UI is intentionally a single-page Jinja2 template with vanilla JS — no framework, no build step. It communicates the system's retrieval and verification process to a reviewer without overclaiming precision.

**Phase tracker:** An 8-stage horizontal pipeline indicator shows the current phase during execution (planning → searching → scraping → reranking → extracting → merging → gap-fill → verifying). Each stage dot transitions from pending → active → done as the job progresses. A live elapsed timer shows wall-clock time.

**Retrieval plan panel:** After results arrive, a collapsible panel shows what the planner decided: entity type, columns, each typed facet (with its query and expected-fill columns), and reranking stats. This makes the system's retrieval strategy inspectable without digging into logs.

**Quality controls panel:** Summarizes which verification and filtering steps ran: cross-encoder reranking, entity deduplication with merge counts, gap-fill enrichment, cell-level verification, field validation, source quality/diversity scoring, and final row filtering. It shows the controls that were active, not fabricated precision.

**Trust badges on rows:** Each row in the results table shows badges for sources count, confidence tier (high/medium/low), and source type diversity (official, editorial, directory, marketplace). Rows with only a single source get a warning badge. These are computed from the actual cell data — source URLs are classified against the same domain lists used by `source_quality.py`.

**Run stats panel:** A compact stats table showing URLs considered, pages scraped, pages after reranking, entities extracted, entities after merge, gap-fill usage, and total duration. The backend also exposes deeper per-stage counters in `SearchMetadata.pipeline_counts` for debugging pipeline collapses.

**Enhanced evidence modal:** Clicking a cell opens a modal with the cell value, confidence bar, verbatim evidence snippet, source URL (linked), source title, and a source-type badge. Validation flags (low confidence, single source) appear when applicable.

**Empty state and error handling:** When no search has been run, a "how it works" explainer shows the pipeline steps. Error and no-results banners provide guidance without hiding the user's last action.

---

## Future improvements

- **Calibrated confidence**: Fine-tune confidence scores against a labeled dataset of correct/incorrect extractions.
- **Incremental streaming**: Stream extracted rows to the UI as they arrive rather than waiting for the full pipeline.
- **JS rendering fallback**: Add optional Playwright scraping for JS-heavy pages.
- **Multi-round refinement**: Allow controlled recursive gap-fill for very sparse tables.
- **Query caching**: Reuse results for near-duplicate queries.
- **Ground-truth evaluation**: Create a small labeled dataset (50–100 cells with verified correct/incorrect labels) to measure precision/recall, not just proxy signals.
- **Broader source quality calibration**: Extend `source_quality.py` domain lists beyond food/startup to cover medical, legal, academic, and e-commerce verticals.
