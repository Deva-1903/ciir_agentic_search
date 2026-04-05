# AgenticSearch

> **Provenance-first entity discovery via multi-angle web search**

Given a free-text topic query, AgenticSearch discovers, structures, and verifies a table of real-world entities — with every cell traceable to its source URL, evidence snippet, and confidence score.

---

## One-line summary

Submit a query → get a ranked table of entities where every cell cites the web page it came from.

---

## Challenge fit

The CIIR Agentic Search Challenge targets systems that go beyond single-shot retrieval to perform multi-step, evidence-grounded information gathering. This system addresses that directly:

- **Retrieval pipeline**: query normalization → constrained query-family planning → typed multi-angle search → scrape → rerank → candidate discovery → merge/canonicalize → official-site resolution → attribute fill → late verification → structured output
- **Evidence grounding**: every cell in the output table has a source URL, verbatim evidence snippet, and confidence score
- **Constrained planning**: the planner picks a small query family and a strong default schema template instead of drifting into generic `entity` plans
- **Evaluation-friendly outputs**: final metadata includes per-stage counts plus a small evaluation harness for rows, fill, actionable fields, official-site rate, and diversity

---

## Architecture overview

```
Query
  │
  ▼
┌─────────────┐
│ Normalizer  │  Safe typo cleanup, spacing cleanup, light location normalization
└──────┬──────┘
       │
       ▼
┌─────────────┐
│   Planner   │  Query family + schema template + typed retrieval facets
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
│ Discovery   │  LLM candidate discovery per page/chunk, recall-first
└──────┬──────┘  Preserves multiple entities; retries on secondary provider if needed
       │
       ▼
┌─────────────┐
│   Merger    │  Fuzzy dedup via rapidfuzz + domain matching
└──────┬──────┘  Best-confidence cell wins per column
       │
       ▼
┌─────────────┐
│ Official    │  Resolve canonical domains / likely official websites where possible
│ Site Resolver│
└──────┬──────┘
       │
       ▼
┌─────────────┐
│   Ranker    │  Score = completeness + confidence + source_quality + source_support
└──────┬──────┘         + actionable + source_diversity (6 weighted components)
       │
       ▼
┌─────────────┐
│  Gap-fill   │  Focused attribute fill for top sparse candidates
└──────┬──────┘  Prefers official/about/contact pages when available
       │
       ▼
┌─────────────┐
│Cell Verifier│  Per-cell entity-alignment check (fuzzy name in evidence/title/domain)
└──────┬──────┘  Penalizes misaligned cells (0.6× confidence) rather than deleting
       │
       ▼
┌─────────────┐
│  Verifier   │  Late row filter for obvious junk, weak marketplace-only rows, and thin evidence
└──────┬──────┘  Falls back safely if all rows would be removed
       │
       ▼
┌─────────────┐
│  Prune+Rank │  Light final cleanup after enrichment and verification
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
    query_normalizer.py# Safe query cleanup before planning/retrieval
    planner.py         # Constrained query-family planning + typed facets
    brave_search.py    # Brave Search API, parallel async
    scraper.py         # Async fetcher + trafilatura/BS4
    extractor.py       # Discovery/fill extraction with chunking + provider fallback
    merger.py          # Fuzzy entity merge
    official_site.py   # Canonical / official-site resolution heuristics
    reranker.py        # Cross-encoder reranking (+ Jaccard fallback)
    ranker.py          # Scoring, ranking, and row pruning
    gap_fill.py        # Focused attribute fill for sparse candidate rows
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
tests/                 # pytest test suite (157 tests)
scripts/
  eval.py              # Evaluation harness (CLI)
docs/
  BUILD_JOURNAL.md     # Full development journal (22 iterations)
  eval_queries.json    # Eval query set (10 queries, 3 categories)
data/                  # Eval reports (created at runtime)
```

---

## Additional Features Implemented

### 1. Constrained query-family planning

The planner first classifies the query into a small family such as `local_business`, `startup_company`, `software_tool`, `product_category`, `organization`, or `fallback_generic`. Each family has a strong schema template. The LLM still helps with typed retrieval facets, but it no longer controls the whole schema surface area. This prevents drift into weak plans like `entity + description + category + location`.

### 2. Per-cell provenance

Every cell in the output table stores:

- `value` — the extracted string
- `source_url` — the page it came from
- `source_title` — the page's title
- `evidence_snippet` — a verbatim or near-verbatim excerpt from the page that supports the value
- `confidence` — a 0–1 score from the LLM reflecting how explicitly the value was stated

This makes the system auditable: a user can click any cell to verify exactly where the value came from.

### 3. Two-stage discovery and fill

Broad entity-discovery queries are handled in two passes. First, the extractor runs in discovery mode and preserves multiple candidate entities from list, review, and directory pages. Then `gap_fill.py` performs focused attribute filling for the top sparse candidates, using the candidate name, missing columns, and any resolved canonical site. This separation makes recall much more stable than asking one extraction pass to both discover and fully fill every row.

### 4. Official-site resolution

After candidate merge, the pipeline tries to resolve canonical domains from explicit website cells and from high-confidence official-looking pages. When resolved, these domains are preferred during attribute filling and improve trust for fields like website, address, phone, and company facts. Final `website` values prefer canonical homepages and leave the field empty rather than stuffing in an article or directory URL.

### 5. Fuzzy entity deduplication

Entities extracted from different pages that refer to the same real-world entity are merged. Matching uses:

- RapidFuzz `token_set_ratio` on normalized names (handles "OpenAI" vs "OpenAI Inc")
- Domain matching on website URLs (strong signal)
- When merging: the highest-confidence cell per column wins

### 6. Source quality scoring

Every row is scored on the trustworthiness of its evidence sources, not just extraction confidence. Sources are classified as `official` (entity's own site), `editorial` (nytimes, theinfatuation, eater, etc.), `directory` (yelp, tripadvisor), `marketplace` (ubereats, doordash), or `unknown`. The `source_quality` score is a confidence-weighted average across all cells and feeds directly into ranking.

### 7. Evidence-based row verification

Before final ranking, a verifier pass removes rows that would not be useful to a user. For strict queries ("top", "best", "leading"), marketplace-only rows are dropped. Late pseudo-entity filtering also removes obvious category/list artifacts that look like labels rather than companies or places. Rows with very low source quality and few cells are also filtered. The verifier always falls back to the original set if everything would be removed.

### 8. Cross-encoder reranking

After scraping, pages are reranked by query relevance before extraction. A cross-encoder model (`cross-encoder/ms-marco-MiniLM-L-6-v2`) scores each page against the original query and keeps only the top-K most relevant. This focuses the extraction LLM budget on pages most likely to contain useful entity data. If the cross-encoder fails to load (e.g., no GPU, missing dependency), a Jaccard token-overlap scorer is used as fallback.

### 9. Cell-level entity verification

After merge and again after gap-fill, every cell is checked for entity alignment: does the evidence snippet, source title, or source domain actually refer to the entity the row is assigned to? Cells that fail all three checks get a 0.6× confidence penalty. This catches the "right row, wrong fact" failure where gap-fill or multi-entity pages introduce cells from a co-mentioned entity.

### 10. Field validation at the extraction boundary

Before cells enter the pipeline, a rule-based validator normalizes and filters by column type:

- **Website**: adds `https://`, validates TLD presence, canonicalizes homepage-like company URLs to the site root, and rejects editorial/article/directory URLs as final website values
- **Phone**: requires ≥7 digits
- **Rating**: requires a number in [0, 10]

Malformed cells are dropped silently; the extractor proceedes with structurally valid data only.

### 11. Source diversity in ranking

The ranker includes a source-diversity component: rows assembled from multiple independent domains score higher than rows where one domain contributed all cells. This is a tie-breaker (0.08 weight), not a gate — a single authoritative official source still ranks well via the dominant source_quality weight (0.32).

### 12. Small evaluation harness

`scripts/eval.py` drives the running server with 10 representative queries and records practical metrics: rows returned, fill rate, actionable-field rate, official-site rate, multi-source row rate, source diversity, average confidence, and runtime. It is intentionally small and reusable rather than a benchmark platform.

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
| `OPENAI_API_KEY`     | ✅       | OpenAI API key (used for planning and default demo extraction) |
| `OPENAI_MODEL`       | optional | Default: `gpt-4o-mini`                                     |
| `GROQ_API_KEY`       | optional | Groq API key (optional alternate/fallback extractor path)  |
| `GROQ_MODEL`         | optional | Default: `llama-3.3-70b-versatile`                         |
| `GROQ_BASE_URL`      | optional | Default: `https://api.groq.com/openai/v1`                  |
| `PLANNER_PROVIDER`   | optional | Default: `openai` — which provider the planner uses        |
| `EXTRACTOR_PROVIDER` | optional | Default: `openai` — which provider the extractor uses      |
| `OPENAI_BASE_URL`    | optional | For non-OpenAI providers                                   |
| `APP_ENV`            | optional | `development` or `production`                              |
| `LOG_LEVEL`          | optional | `INFO` (default) or `DEBUG`                                |

The system uses a **dual-provider** model with an OpenAI-first demo path: OpenAI handles schema planning and is also the default extractor provider for reviewer-facing runs. Groq remains supported as an optional alternate/fallback extractor path through the same OpenAI-compatible client wrapper. If the configured primary extractor provider fails and another configured provider is available, extraction retries on the secondary provider instead of silently returning zero entities.

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
| `AI startups in healthcare`      | startup     | name, website, headquarters, focus_area, product_or_service, funding_stage |
| `top pizza places in Brooklyn`   | pizza place | name, website, address, phone_number, category, rating                    |
| `open source database tools`     | software tool | name, website, license, language, github_repo, use_case                 |
| `climate tech startups series A` | startup     | name, website, headquarters, focus_area, product_or_service, funding_stage |

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
      "product_or_service",
      "funding_stage"
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
      "original_query": "AI startups in healthcare",
      "normalized_query": "AI startups in healthcare",
      "query_family": "startup_company",
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
        "candidate_rows": 14,
        "official_sites_resolved": 4,
        "rows_after_gap_fill": 14,
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

**Query normalization is lightweight by design**: The normalizer only applies bounded cleanup and a small set of safe typo/location fixes. It is meant to rescue obvious retrieval-poisoning mistakes, not rewrite user intent.

**Constrained planning beats free-form planning**: The planner uses a small query-family classifier and schema templates so it cannot drift into useless generic schemas. The LLM still contributes retrieval facets, but the entity type and columns stay reviewer-friendly and predictable.

**Discovery and filling are separate responsibilities**: Candidate discovery is optimized for recall; attribute filling is optimized for completeness and provenance. Keeping them separate is simpler and more stable than forcing one extraction pass to do both jobs.

**Extractor provider fallback**: Broad discovery queries should not collapse to zero rows just because one extraction provider is rate-limited. The demo now defaults extraction to OpenAI for runtime stability, while still allowing a configured secondary provider such as Groq to serve as an alternate/fallback path. This favors correctness and predictable reviewer runs over minimum latency.

**Ranker design**: The ranking formula is a weighted sum of six interpretable signals: completeness (0.25), average confidence (0.20), source quality (0.32), source support (0.08), actionable-field bonus (0.07), and source diversity (0.08). Source quality dominates by design. More complex ranking (BM25 against query, embedding similarity) was intentionally omitted — the signals are already well-correlated with quality and remain fully explainable.

---

## Known limitations

- **LLM hallucination**: Despite strong prompt constraints, the extractor may occasionally assign `confidence > 0` to values weakly implied by context. The evidence snippet requirement and cell-level verification reduce this but do not eliminate it.
- **Dynamic pages**: JavaScript-rendered pages (SPAs) are not scraped; the system fetches static HTML only. This misses some sources.
- **Rate limits**: Running many queries quickly may hit Brave or OpenAI limits depending on your plan. Groq is no longer the default demo extractor because its free tier can throttle unpredictably; if you enable it as an alternate/fallback path, provider recovery can still increase latency and cost.
- **Latency**: A typical query takes about 25–60 seconds on the default OpenAI demo path depending on page count, reranking, and gap-fill. Optional Groq use can be faster when healthy, but fallback recovery during provider throttling can push latency higher.
- **Schema quality**: Constrained planning sharply reduced generic schemas, but `fallback_generic` still exists for ambiguous topics and can be weaker than the domain-specific families.
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

With the default demo setup (`gpt-4o-mini` planner + `gpt-4o-mini` extractor) and 15 scraped pages:

- Planner: ~0.3–1.0s
- Extractor: ~0.5–1.5s per page, ~1500 tokens per chunk — the dominant cost
- Gap-fill: adds ~10–25s and up to 5 focused entity fills for sparse rows

Estimated token cost per query is still **~30k–80k tokens**. The OpenAI-first demo path avoids Groq 429 recovery loops at the cost of somewhat slower extraction than an ideal healthy Groq run.

If Groq is enabled as an alternate/fallback extractor, it can be faster when healthy, but provider throttling can increase total latency and secondary-provider spend. With `gpt-4o-mini`, typical extraction spend remains in the ~$0.01–$0.03 per query range.

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

Gap-fill is triggered after candidate merge and ranking. The `find_sparse_rows()` function in `ranker.py` selects the top-N rows with the most missing columns. For each:

1. `gap_fill.py` generates focused queries, prioritizing high-value missing columns (`website`, `headquarters`, `funding_stage`) and using any resolved canonical domain first.
2. A fresh Brave search is run only if the official/canonical pages do not already cover the gap.
3. A small set of pages is scraped and run through the same LLM extractor in fill mode.
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

Results are saved to `data/eval_<tag>_<timestamp>.json` and `.csv`. Metrics include: rows returned, fill rate, actionable-field rate, official-site rate, multi-source rate, average confidence, source diversity, and duration.

See `docs/eval_queries.json` for the query set (10 queries across food, tech, and travel categories).

---

## Why these improvements were prioritized

The improvement phases were ordered by practical impact on output quality:

1. **Query normalization + constrained planning**: The planner drives everything downstream, so the first win is making it predictable. Light normalization rescues obvious typos, and query-family templates keep the schema from collapsing into generic output.

2. **Discovery-first extraction + official-site resolution**: Broad queries fail when recall collapses too early. Separating candidate discovery from attribute fill keeps more plausible entities alive, and official-site resolution improves the later facts without turning recall into a hard gate.

3. **Cross-encoder reranking + softer late filtering**: Reranking keeps extraction focused, while late verification avoids zeroing out broad discovery runs before ranking has a chance to separate strong rows from weak ones.

4. **Evaluation harness**: Without metrics, everything above is validated by unit tests and gut feel. The eval harness makes quality measurable — even if the metrics are still proxy signals rather than ground truth.

All improvements are heuristic. Source quality classification uses hand-curated domain lists, not a trained classifier. Cell verification uses fuzzy string matching, not a semantic model. The evaluation harness measures fill rate and diversity, not factual accuracy. These are practical engineering choices for a system that needs to work across arbitrary domains without labeled training data.

---

## Why the UI is designed this way

The UI is intentionally a single-page Jinja2 template with vanilla JS — no framework, no build step. It communicates the system's retrieval and verification process to a reviewer without overclaiming precision.

**Phase tracker:** An 8-stage horizontal pipeline indicator shows the current phase during execution (planning → searching → scraping → reranking → extracting → merging → gap-fill → verifying). Each stage dot transitions from pending → active → done as the job progresses. A live elapsed timer shows wall-clock time.

**Retrieval plan panel:** After results arrive, a collapsible panel shows the normalized query (when it changed), query family, entity type, columns, each typed facet (with its query and expected-fill columns), and reranking stats. This makes the system's retrieval strategy inspectable without digging into logs.

**Quality controls panel:** Summarizes which recall and quality controls ran: query normalization, constrained planning, candidate discovery, deduplication, official-site resolution, gap-fill enrichment, cell-level verification, field validation, source quality/diversity scoring, and late row filtering. It shows the controls that were active, not fabricated precision.

**Trust badges on rows:** Each row in the results table shows badges for sources count, confidence tier (high/medium/low), and source type diversity (official, editorial, directory, marketplace). Rows with only a single source get a warning badge. These are computed from the actual cell data — source URLs are classified against the same domain lists used by `source_quality.py`.

**Run stats panel:** A compact stats table showing query family, normalized query, URLs considered, pages scraped, candidate rows, official-site matches, entities extracted, entities after merge, gap-fill usage, and total duration. The backend also exposes deeper per-stage counters in `SearchMetadata.pipeline_counts` for debugging pipeline collapses.

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
