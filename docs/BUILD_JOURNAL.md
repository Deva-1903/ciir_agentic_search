# Build Journal — AgenticSearch

## Project Goal

Build a provenance-first entity discovery app for the CIIR Agentic Search Challenge. Given a free-text topic query (e.g. "AI startups in healthcare"), the system discovers real-world entities on the web, structures them into a ranked table, and attaches verifiable evidence to every cell — source URL, verbatim snippet, and confidence score. Every value must be traceable to exactly where it came from.

---

## Evolution Snapshot

- **Started as:** single-pass query → search → scrape → extract → return table
- **Added:** multi-angle query planning so recall covers list pages, official sites, and news
- **Added:** per-cell provenance (source_url, evidence_snippet, confidence) at extraction time
- **Added:** fuzzy entity deduplication across pages (rapidfuzz + domain matching)
- **Fixed (Claude):** `--reload` killing background tasks — moved DB to `/tmp`, restricted watch dir
- **Fixed (Claude):** LLM extraction hanging forever — 60s timeout + global semaphore
- **Fixed (Claude):** polling stopping on transient `ERR_NETWORK_CHANGED` — retry up to 8 failures
- **Fixed (Claude):** stale jobs stuck in `running` after restart — cleanup on startup
- **Fixed (user):** infinite loop in `chunk_text` causing `Killed: 9` OOM crash
- **Fixed (user):** semaphore leak — chunks of the same page bypassed the global extraction limit
- **Improved (user):** gap-fill entity contamination — focused queries + name-similarity filter
- **Improved (user):** lost `name` cells — auto-promote `entity_name` into schema cells
- **Improved (user):** merger lookup stale — refresh after every absorption
- **Improved (user):** weak rows surviving — `prune_rows()` + actionable-field scoring
- **Added (user):** `source_quality.py` — classifies sources as official/editorial/directory/marketplace
- **Added (user):** `verifier.py` — filters marketplace-only and low-evidence rows before final ranking
- **Evaluated:** pizza query run confirms top-of-list quality improved; cell-level entity consistency still imperfect
- **Added (user):** facet-typed planning — planner emits typed `SearchFacet` objects instead of flat search angles
- **Added (user):** `reranker.py` — cross-encoder reranking (MiniLM-L-6-v2) before extraction; Jaccard fallback
- **Added (user):** `cell_verifier.py` — per-cell entity-alignment penalty (evidence/title/domain match)
- **Added (user):** `field_validator.py` — URL/phone/rating normalization at extraction boundary
- **Added (user):** `_source_diversity()` in ranker — penalises single-domain rows (0.08 weight)
- **Added (user):** `scripts/eval.py` + `docs/eval_queries.json` — CLI evaluation harness with 10 queries, 7 metrics
- **Rebuilt (user):** UI — phase tracker, retrieval plan panel, quality controls panel, trust badges, run stats, enhanced modal, empty/error states
- **Added (user):** Groq as primary LLM provider, OpenAI fallback, `_extract_json` markdown fence handling
- **Current state:** full 11+-stage pipeline (incl. reranker, cell verifier ×2), Groq primary / OpenAI fallback, dark-theme UI with phase tracker, trust badges, quality panels, per-cell evidence modal, JSON/CSV export

---

## Attribution Note

The initial system scaffold, all FastAPI plumbing, the full pipeline skeleton, and the runtime stability fixes (timeout, semaphore, reload, polling) were written by Claude.

The quality improvement pass — chunker fix, extraction semaphore refactor, gap-fill entity focus, name backfill, merger lookup refresh, row pruning, `source_quality.py`, `verifier.py`, and the iterative evaluation — was driven and implemented by the user. Those changes are documented accurately below.

---

## Initial Plan

### Scope

Build a single-backend, single-frontend system that takes a topic query and returns a structured entity table. The challenge requires evidence grounding, so every cell must carry provenance. Target: submission-ready in 3–4 days.

### Chosen stack

- **Backend:** Python / FastAPI / Pydantic — fast to write, good async support, Pydantic models enforce structure at every layer
- **LLM client:** OpenAI-compatible API — works with gpt-4o-mini (cheap, fast), compatible with Groq/Together/Mistral
- **Search:** Brave Search API — clean REST API, good English-language web coverage
- **Scraping:** trafilatura (primary) + BeautifulSoup fallback — trafilatura strips nav/ads and produces clean prose, which matters a lot for LLM extraction quality
- **Dedup:** rapidfuzz — fast fuzzy string matching, no ML dependency
- **Persistence:** SQLite via aiosqlite — zero-ops, sufficient for caching scraped pages and job state
- **Frontend:** Jinja2 + vanilla JS — no build step, no framework overhead

### What was intentionally left out

- Browser automation — adds 500MB dependency for marginal gain
- Vector DB / embeddings — dataset per query is 10–50 rows, fuzzy matching is faster and sufficient
- Auth / user accounts — irrelevant for a research submission
- Streaming results — job-based polling is simpler and reliable
- Multi-agent orchestration — the pipeline is a linear DAG; each stage is a focused async function

---

## Iteration Log

---

### Iteration 1 — Initial Vertical Slice

**Date:** 2026-03-31  
**Author:** Claude  
**Goal:** Working end-to-end pipeline from query to structured JSON.

**What was implemented:**

- Full project scaffold: `app/`, `templates/`, `static/`, `tests/`, `docs/`
- Config via pydantic-settings, structured logging
- SQLite schema for `scraped_pages` (URL cache) and `query_jobs` (pipeline state)
- `planner.py` — LLM infers entity_type, columns (5–8), search angles (3–5)
- `brave_search.py` — parallel async Brave API calls per angle, URL dedupe + junk filter
- `scraper.py` — async fetch with semaphore, trafilatura → BS4 fallback, SQLite cache
- `extractor.py` — LLM structured extraction per page with per-cell evidence snippets
- `merger.py` — rapidfuzz + domain dedup, best-confidence cell selection
- `ranker.py` — weighted score: completeness + avg_confidence + source_support + has_website
- `gap_fill.py` — targeted queries for top-3 sparse rows (bounded: 3 entities × 2 URLs × 1 round)
- `exporter.py` — JSON + flattened CSV with provenance columns
- FastAPI routes: `POST /api/search` (job-based 202), `GET /api/search/{id}`, export endpoints
- Jinja2 + vanilla JS UI: search box, polling, result table, per-cell evidence modal, export buttons
- 57 unit tests (URL utils, dedupe, merger, ranker, exporter, text utils)

**Why job-based model:**
Pipeline takes 20–60s. Synchronous HTTP would time out. Returning `job_id` + polling avoids that with no extra infrastructure.

**What worked:**

- Planner produced good schemas from the first prompt
- Brave search reliably returned 15–25 unique URLs across 5 angles
- Per-cell provenance design was sound — evidence snippets came through in extraction
- UI polling, evidence modal, and export all functioned

**What failed / weak spots found immediately:**

- `Jinja2Templates.TemplateResponse("index.html", {"request": request})` — Starlette 1.0 changed the API, caused HTTP 500 on every page load
- `uvicorn --reload` watched entire project directory including `data/` — every SQLite write triggered a worker restart, killing in-flight background tasks
- No explicit timeout on `AsyncOpenAI` client — default is 600s, LLM calls could hang forever
- No concurrency limit on extraction — 13 pages × 2 chunks = 26 simultaneous LLM calls
- Polling JS: `catch` block unconditionally called `clearInterval` — any single network error killed all future polls

---

### Iteration 2 — Runtime Stability

**Date:** 2026-03-31  
**Author:** Claude  
**Goal:** Make the pipeline reliably reach completion; make failures visible.

**Problems being fixed:**

| #   | Problem                          | Symptom                                                     |
| --- | -------------------------------- | ----------------------------------------------------------- |
| 1   | `--reload` restarts on DB writes | Job stuck at `scraping` forever after every scrape          |
| 2   | No LLM timeout                   | Extraction phase hangs indefinitely, no error               |
| 3   | No extraction semaphore          | 26 simultaneous LLM calls saturate rate limit, most stall   |
| 4   | Polling stops on any error       | `ERR_NETWORK_CHANGED` shows "Polling failed", stops forever |
| 5   | Stale `running` jobs on restart  | Old job IDs return `status=running` on fresh server start   |
| 6   | Silent extraction phase          | Logs stop after scraping with no indication of progress     |

**Fixes applied:**

| Problem              | Fix                                                                                      | File           |
| -------------------- | ---------------------------------------------------------------------------------------- | -------------- |
| --reload restart     | Moved DB to `/tmp/agentic_search.db`; `--reload-dir app`                                 | `config.py`    |
| LLM hangs            | `AsyncOpenAI(timeout=60.0, max_retries=0)`                                               | `llm.py`       |
| Too many LLM calls   | `asyncio.Semaphore(3)` at page level                                                     | `extractor.py` |
| Polling dies on blip | `_pollErrors` counter, retry up to 8 before giving up                                    | `app.js`       |
| Stale jobs           | `UPDATE query_jobs SET status='failed' WHERE status IN ('running','pending')` on startup | `db.py`        |
| Silent extraction    | `log.debug` → `log.info` per page                                                        | `extractor.py` |

**What also changed in this iteration:**

- `routes_search.py` rewritten with `_phase()` helper — every stage logs elapsed time
- All pipeline exceptions now produce `=== Pipeline FAILED ===` banner with full traceback
- `tenacity` dependency removed from `llm.py`, replaced with explicit retry loop (more transparent)

**Tradeoffs introduced:**

- Hard 60s LLM timeout abandons slow calls instead of retrying. For large pages this may lose extraction. Acceptable because `max_chunks_per_page=2` limits exposure.
- Semaphore of 3 makes extraction more sequential, increasing total latency ~2–3×. Necessary to stay within rate limits.

**Resulting improvement:**
Pipeline now reliably reaches completion on a clean start. Any failure produces a full traceback. UI survives transient network blips during 40s extraction runs.

---

### Iteration 3 — Chunker Bug / OOM Crash Fix

**Date:** 2026-03-31  
**Author:** User  
**Goal:** Fix server crash (`Killed: 9`) that was occurring during extraction on large pages.

**Problem:**
`chunk_text()` in `app/utils/text.py` moved the `start` index backward by `overlap_chars` after every chunk. For middle chunks this was fine. For the final chunk, if `end == length`, the next `start` calculation moved backward — causing the loop to regenerate the same tail chunk forever. Memory grew until macOS OOM-killed the process.

**What changed in `text.py`:**

- Loop now breaks immediately once `end >= length`
- `start` is always forced to advance by at least 1 character (no backward movement at end-of-text)
- Added `max_chunks` parameter — hard cap on number of chunks generated

**What changed in `extractor.py`:**

- `extract_from_page()` passes `max_chunks=settings.max_chunks_per_page` directly into `chunk_text()` — no longer generates extra chunks it will never use
- Semaphore fix: original Iteration 2 semaphore was at the page level, but each page's chunk tasks used a separate `asyncio.gather` — a 2-chunk page could fire 2 LLM calls regardless of the semaphore. Refactored to pass `llm_sem` down to every individual chunk call, so the semaphore truly bounds global LLM concurrency.

**What changed in `config.py`:**

- Added `extract_llm_timeout_seconds: float = 30.0` — shorter timeout for extraction than planning
- Added `extract_llm_max_attempts: int = 1` — extraction does not retry; fail fast and skip the page

**Why these values:**
Extraction is best-effort per page. A 30s timeout means a slow page is abandoned quickly, not retried. The pipeline continues with remaining pages rather than waiting.

**Tests added:** `test_long_text_does_not_loop_forever_on_final_chunk`, `test_respects_max_chunks_limit` in `tests/test_text_utils.py`.

**Files affected:** `app/utils/text.py`, `app/services/extractor.py`, `app/core/config.py`

---

### Iteration 4 — Extraction Precision: Gap-fill, Name Backfill, Merger Fix

**Date:** 2026-03-31  
**Author:** User  
**Goal:** Fix three extraction quality issues that caused wrong data in cells.

**Problem 1 — Gap-fill entity contamination:**

Gap-fill used the full original query and full original schema when searching for missing attributes. If a target page mentioned multiple similar entities (e.g. multiple pizza places), the extractor could return drafts for the wrong entity, and gap-fill would absorb those into the target row.

Fix in `gap_fill.py`:

- Build focused queries using entity name + specific missing column hints (`_COLUMN_QUERY_HINTS` dict: `phone_number` → `"phone number"`, `funding_stage` → `"funding stage"`, etc.)
- First query always: `"<entity_name>" <original_query>`
- Create a reduced extraction plan with only `name` + missing columns — stops LLM from extracting already-filled columns
- Use entity name as the focused extraction query (not the original query)
- After extraction, reject drafts whose `entity_name` does not pass `names_are_similar()` against the target
- Re-check `_missing_cols()` after each page — stop early if all gaps filled

**Problem 2 — Lost `name` cells:**

LLM sometimes returned a valid `entity_name` in the extraction response but omitted the structured `"name"` cell in the `cells` dict. Downstream, such rows had no name cell, were treated as non-viable, and were pruned.

Fix in `extractor.py`:

- After parsing a draft, if `"name"` is in `plan.columns` but not in `cells`, backfill `cells["name"]` from `entity_name` at confidence 0.75

**Problem 3 — Merger lookup stale after website absorption:**

`merger.py` maintained a `lookup` list for dedup matching. After merging a draft that added a `website` cell to an existing entity, the lookup entry was not updated. A later draft whose best match signal was domain overlap would fail to find the entity in the lookup and create a duplicate row.

Fix in `merger.py`:

- After `states[idx].absorb(draft)`, update `lookup[idx]` with the merged entity's current `_name_str` and `_website`

**Test added:** `test_updates_lookup_after_merge_when_website_arrives_later` in `tests/test_merger.py` — verifies three drafts with name+website arriving in different order all collapse to one entity.

**Files affected:** `app/services/gap_fill.py`, `app/services/extractor.py`, `app/services/merger.py`, `tests/test_merger.py`

---

### Iteration 5 — Row Quality: Pruning and Actionable Scoring

**Date:** 2026-03-31  
**Author:** User  
**Goal:** Stop low-information rows from polluting the output.

**Problem:**
After first real runs (e.g. "top pizza places in Brooklyn"), results included rows like:

- `name: "Pizza Place"`, `cuisine_type: "Pizza"` — no address, no website, no contact info
- `name: "Italian Restaurant"` — name only
- Rows technically valid but with zero actionable value to a user

The original ranker scored completeness + confidence but made no distinction between "strong" columns (address, phone, website, rating) and "weak" columns (cuisine_type, category, description). A row with two confident `cuisine_type` extractions scored almost as well as a row with address + rating.

**What changed in `ranker.py`:**

New concepts:

- `_WEAK_SIGNAL_COLS`: `{category, cuisine_type, description, industry, overview, summary, type}` — columns that are often present but low utility
- `_is_actionable_col(col)`: returns True for any column that is not `name` and not in weak-signal set
- `is_row_viable(row, plan)`: returns False if row has no `name` cell, or if all non-name columns are weak-signal
- `prune_rows(rows, plan)`: filters to viable rows; falls back to original if pruning removes everything
- `actionable` score component added to ranking formula — bonus for having at least one actionable non-name field
- `find_sparse_rows()` now uses `is_row_viable()` as filter — gap-fill only targets rows worth enriching

**New ranking weights:**

```
completeness:   0.28  (was 0.40)
avg_confidence: 0.22  (was 0.35)
source_support: 0.10  (was 0.15)
actionable:     0.08  (new)
source_quality: 0.32  (new — see Iteration 6)
```

**Pipeline change:**
Two prune passes now run:

1. After merge — remove obviously junk rows
2. After verify — remove rows that survived gap-fill but still have weak evidence

**Tests added:** `test_prunes_generic_name_plus_one_weak_field_rows`, `test_falls_back_to_original_rows_when_everything_is_pruned`, `test_skips_low_information_rows` in `tests/test_ranker.py`.

**Files affected:** `app/services/ranker.py`, `app/api/routes_search.py`, `tests/test_ranker.py`

---

### Iteration 6 — Source Quality Scoring and Final Verifier

**Date:** 2026-03-31  
**Author:** User  
**Goal:** Stop low-trust sources (delivery apps, thin listicles) from dominating rankings; add a final filter based on evidence quality.

**Problem:**
After Iteration 5, the system still ranked rows highly if they were sourced from UberEats/DoorDash category pages with confident but low-value extractions. A row with `name + cuisine_type` from a delivery listing ranked similarly to a row with `name + address + rating` from a food editorial. Confidence reflects how clearly the LLM read a value — it says nothing about whether the source itself is trustworthy.

**What was added:**

**`app/services/source_quality.py`** (new module, written by user):

Classifies source URLs into 5 categories:

- `official` — domain matches the row's own website cell (score: 1.0)
- `editorial` — eater.com, foodandwine.com, grubstreet.com, michelin.com, newyorker.com, nytimes.com, seriouseats.com, tastingtable.com, theinfatuation.com, thrillist.com, timeout.com, vogue.com (score: ~0.85)
- `directory` — yelp.com, tripadvisor.com, opentable.com (score: ~0.65)
- `marketplace` — ubereats.com, doordash.com, grubhub.com, seamless.com, postmates.com (score: ~0.20)
- `unknown` — everything else (score: 0.55, neutral)

Score modifiers:

- `+0.05` if `"official"` in title OR URL path contains `/about`, `/contact`, `/menu`, `/locations`
- `+0.05` if title/path contains "best", "guide", "review", "reviews", "top"
- `-0.20` if title/path contains "delivery", "near me", "order", "category"

`row_source_quality(row)` computes confidence-weighted average quality across all source URLs contributing to a row.

`row_source_profile(row)` returns counts by kind — used by verifier to detect marketplace-only rows.

**`app/services/verifier.py`** (new module, written by user):

`verify_rows(rows, plan, query)` filters rows before final ranking:

- `_query_is_strict(query)` — True if query contains "best", "top", "leading", "highest rated", "must visit", "must-visit"
- Drops rows that are `marketplace_only` (no official, editorial, or directory sources) on strict queries
- Drops rows with `source_quality < 0.3` AND fewer than 4 cells
- Drops rows with `source_quality < 0.45` AND fewer than 2 actionable fields on strict queries
- Falls back to original set if all rows would be removed

**Pipeline wiring in `routes_search.py`:**

```
merge → prune → rank → gap_fill → verify → prune → rank
```

**Tests added:** `tests/test_source_quality.py`, `tests/test_verifier.py`; updated `tests/test_ranker.py` with `test_better_sources_rank_higher`.

**Files added:** `app/services/source_quality.py`, `app/services/verifier.py`  
**Files changed:** `app/services/ranker.py`, `app/api/routes_search.py`, `tests/test_ranker.py`

---

### Iteration 7 — Evaluation and Remaining Issues

**Date:** 2026-03-31  
**Author:** User (evaluation), both (analysis)  
**Goal:** Assess actual output quality after the full quality pass.

**Test query:** `top pizza places in Brooklyn`

**What improved vs initial run:**

1. Marketplace-only rows (UberEats category pages) removed from final output
2. Top-of-list now includes credible entities: Di Fara, Lucali, Paulie Gee's, Roberta's, L'Industrie, Juliana's
3. More rows contain actionable fields (address, phone_number, rating)
4. Runtime improved: ~61s vs ~114s from earlier runs (benefit of semaphore + shorter extraction timeout)

**Issues still observed:**

| Issue                             | Example                                                                         | Root cause                                                   |
| --------------------------------- | ------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| Cell-level entity contamination   | `Espresso Pizzeria` received address + phone belonging to `F&F Pizzeria`        | Gap-fill still not filtering tightly enough at cell level    |
| Mixed entity rows                 | `Paulie Gee's` mixes the restaurant and the slice shop                          | Same page mentions both; name similarity threshold too loose |
| Website not normalized            | `Roberta's` website value is `robertaspizza` not a valid URL                    | No URL validation on extracted website values                |
| Single-source domination          | `foodieflashpacker.com` contributes most contact/rating fields across many rows | No source diversity constraint in merger/ranker              |
| Weak generic fields still present | `cuisine_type: Pizza` appearing without useful context                          | Correctly pruned by prune_rows but still extracted           |

**Conclusion:**
Source quality scoring and verifier worked at the row level — low-trust row selection improved. But cell-level entity consistency is still imperfect. One source can still dominate a large share of a row's evidence without penalty.

**Recommended next improvements (not yet implemented):**

1. Stricter cell-level entity verification — reject cells whose evidence does not clearly name the target entity
2. Website URL validation — require `http(s)://` prefix, reject fragments
3. Source diversity constraint — penalize rows where >60% of cells come from a single domain
4. Domain-aware column weighting — restaurants prioritize address/phone/rating; startups prioritize funding/investors/website

---

### Iteration 8 — Facet-Typed Search Planning

**Date:** 2026-04-04
**Author:** User (directed), Claude (implementation)
**Goal:** Replace paraphrase-style `search_angles` with typed retrieval facets so each search query has explicit intent.

**Initial assumption:**
The existing planner produced 3–5 "search angles" as plain strings. They worked, but nothing in the output told downstream code _why_ a given angle existed. From an IR standpoint, this looked like query rewriting, not retrieval planning.

**Issue discovered:**

- Angles were often paraphrases: `"AI startups healthcare"`, `"healthcare AI companies"`, `"top AI startups healthcare 2024"`. Three strings, one intent.
- No way to say "this angle exists to find official sites" vs "this angle exists to pull funding news".
- No signal to a reranker or extractor about which columns each retrieved page is expected to fill.

**Why the old approach was insufficient:**
Search diversity was shallow. Recall depended on the LLM accidentally producing angles of different shapes. There was also no hook for downstream stages (Phase 2 reranker, extractor) to weight a page's utility per facet.

**What was built:**

- `SearchFacet` pydantic model with `type`, `query`, `expected_fill_columns`, `rationale`.
- Canonical facet types (enforced via normalizing validator): `entity_list`, `official_source`, `editorial_review`, `attribute_specific`, `news_recent`, `comparison`, `other`.
- `PlannerOutput.facets: List[SearchFacet]` added alongside existing `search_angles`.
- `search_angles` is now _derived_ from `[f.query for f in facets]` — preserves backward compat for `brave_search.py`, `routes_search.py`, `gap_fill.py`.
- Planner prompt rewritten to demand typed facets with distinct retrieval intent; explicit type definitions in the prompt.
- `_sanitize_facets()` drops empty queries and restricts `expected_fill_columns` to the actual schema columns.
- `_fallback_plan()` now produces 3 canonical fallback facets (entity_list / official_source / editorial_review) instead of a list of strings.
- `SearchMetadata.facets` surfaces the facet plan in the API response.

**Why this change was chosen:**
Alternative 1 — keep strings, just add a parallel `angle_types: List[str]` array. Rejected: two loosely-coupled lists break if reordered.
Alternative 2 — make facet type an `Enum`. Rejected: LLMs can return unexpected values; a validator that normalizes to a closed set with an `other` fallback is more robust and keeps JSON parsing simple.
Alternative 3 — remove `search_angles` entirely. Rejected: would churn four files (`brave_search`, `routes_search`, `gap_fill`, `test_*`) for no behavioral gain. Making it a derived property is the smaller move.

**What happened when tested:**
7 new planner tests pass; all 87 tests pass overall. Normalization correctly folds `"Entity List"` → `"entity_list"` and unknown types → `"other"`. Column sanitization correctly strips invalid column names from `expected_fill_columns`.

**Failures / issues observed:**
None during Phase 1. The LLM prompt is longer (more token cost per plan call, ~512→768 max output tokens); acceptable because planning is called once per query.

**Resulting improvement:**

- Retrieval planning is now explicit and inspectable in the API response.
- `expected_fill_columns` is available for Phase 2 (reranker can score "does this page help fill the columns this facet targeted?").
- Fallback path produces typed facets even when the LLM fails.

**Tradeoffs introduced:**

- Slightly larger planner prompt and response.
- LLM occasionally returns a type outside the canonical set; the validator converts to `other` rather than failing — honest but some facet intent is lost.

**Files/modules affected:**
`app/models/schema.py`, `app/services/planner.py`, `app/api/routes_search.py`, `tests/test_planner.py` (new).

**Next step:**
Phase 2 — add a cross-encoder reranker that scores scraped pages against the raw query (and optionally facet queries) so extraction budget focuses on high-utility pages.

---

### Iteration 9 — Cross-Encoder Reranking Before Extraction

**Date:** 2026-04-04
**Author:** User (directed), Claude (implementation)
**Goal:** Replace "extract from everything Brave returned" with a real query-aware IR step so extraction budget is spent on high-utility pages.

**Initial assumption:**
Brave's result order plus URL dedup was good enough. In practice, Brave returns a lot of near-miss pages (loose topic match, marketplace listings, SEO pages) that the extractor was spending 30s LLM calls on.

**Issue discovered:**

- No learned relevance signal between "query" and "page content" anywhere in the pipeline.
- Extraction was the single most expensive stage (~1–2s per page × 15–25 pages = 20–50s) and some of that budget was being wasted on pages that contributed nothing after merge/prune.
- From an IR-submission standpoint, the pipeline did retrieval (Brave) but no reranking — a visible gap.

**Why the old approach was insufficient:**
Brave ranks on its own signals and cannot see the cleaned page text. A bad-title / good-body page gets buried; a good-title / thin-body page ranks too high. We need a reranker that actually reads the scraped content.

**What was built:**

- New module `app/services/reranker.py`.
- Primary scorer: `cross-encoder/ms-marco-MiniLM-L-6-v2` via `sentence-transformers`. Model is loaded lazily on first use, then cached at module level. Scoring runs on a worker thread (`asyncio.to_thread`) so it does not block the event loop.
- Fallback scorer: Jaccard-style lexical overlap on tokenized query + page doc. Zero deps, deterministic — used when sentence-transformers is missing, fails to load, or `predict()` raises.
- Page "document" for scoring = `title + first 1200 chars of cleaned_text`. Keeps cross-encoder input under 512 tokens.
- Wired into pipeline **between scrape and extract**: if `pages_scraped > rerank_top_k`, call `rerank_pages(query, pages, top_k)` and only extract from the returned top-K.
- Config: `rerank_enabled: bool = True`, `rerank_top_k: int = 10`.
- Metadata: `pages_after_rerank` and `rerank_scorer` added to `SearchMetadata` for observability.

**Why this change was chosen:**
Alternative 1 — rerank Brave results _before_ scraping. Rejected: reranker would only see title + snippet (essentially what Brave already ranked on). Scraping first and reranking on real body text is the more informative signal.
Alternative 2 — use a bi-encoder + embedding index. Rejected: bi-encoders are faster but worse for query-document relevance at this scale; there is no retrieval set large enough to justify vectors.
Alternative 3 — use the LLM itself for reranking. Rejected: adds cost/latency and duplicates the extractor's work.
Alternative 4 — score against each facet query instead of the raw query. Deferred: introduces "which facet fires?" complexity and requires per-facet top-K logic. Raw query is the clean starting point.

**What happened when tested:**
8 new reranker tests pass (lexical score ordering, empty input, top-K, graceful fallback when `predict()` throws). All 95 tests pass overall. The model loads cleanly in the test env (~3s cold) and batches 20 pages in <200ms.

**Failures / issues observed:**

- `sentence-transformers` pulls in torch (~800MB). Large but already present on most ML-capable environments. Added to `pyproject.toml` as a hard dep because the lexical fallback is a safety net, not a substitute. If install is a blocker the fallback keeps the pipeline functional.
- First-run model download hits HuggingFace; on air-gapped machines the fallback kicks in automatically.

**Resulting improvement:**

- Extraction now runs on a focused set (default top-10) instead of everything Brave returned.
- Visible IR step in the API response (`rerank_scorer`, `pages_after_rerank`).
- Predictable LLM budget: max 10 pages × 2 chunks = 20 chunks per query regardless of Brave's volume.

**Tradeoffs introduced:**

- Cold model load adds ~3s to the first query of the process. Subsequent queries amortize it away.
- Reranker can drop a legitimately good page that has a weak title/lead — no free lunch. `rerank_top_k=10` is chosen to be tolerant rather than greedy.
- Torch dependency is large. Lexical fallback keeps the project runnable without it, but the retrieval-quality claim rests on the cross-encoder being available.

**Files/modules affected:**
`app/services/reranker.py` (new), `app/api/routes_search.py`, `app/core/config.py`, `app/models/schema.py`, `pyproject.toml`, `tests/test_reranker.py` (new).

**Next step:**
Phase 3 — cell-level verification (evidence-entity alignment), field validation (website URL normalization), and source diversity penalty in ranking.

---

### Iteration 10 — Cell-Level Verification, Field Validation, Source Diversity

**Date:** 2026-03-31
**Author:** User (directed), Claude (implementation)
**Goal:** Close the "right entity wrong fact" failure mode: a row for entity A accidentally absorbing a cell whose evidence actually describes entity B. Also harden the cell boundary against malformed values and reward rows backed by multiple independent domains.

**Initial assumption:**
Row-level verification (`verifier.py`, Iteration 6) + merger dedup (`merger.py`, Iteration 4) were sufficient to keep each row's cells coherent. In practice, they could not: the row verifier decides whether a whole row survives, and the merger deduplicates rows by name — neither one re-checks _each cell's evidence_ against the entity the row ended up being assigned to.

**Issue discovered:**

- Iteration 7's regression list contained `Espresso Pizzeria → "F&F Pizzeria can be reached at 718-555-1234"`. The row name was correct, the phone was extracted cleanly, but the evidence snippet describes a _different_ entity. No existing stage caught it.
- Iteration 7 also recorded a website cell of `robertaspizza` (no TLD, not a URL). The extractor accepted the raw LLM output as-is; there was no type-aware boundary before it entered the pipeline.
- Ranking had no "source diversity" component: a row with three cells all from one domain scored identically to a row with three cells from three independent domains.

**Why the old approach was insufficient:**

- Row verifier operates on aggregate signals (source quality, sources_count). It cannot see per-cell evidence.
- Merger compares _values_ across rows, not evidence-to-name alignment _within_ a row.
- Extractor trusted LLM output to be type-conformant — that assumption fails on URLs in particular.
- Ranker rewarded completeness but not independence. A single marketplace page filling every cell would score like three independent editorial confirmations.

**What was built:**

1. **`app/services/field_validator.py`** (new) — rule-based normalization at the extraction boundary.
   - `normalize_website()`: accepts `x.com` / `HTTPS://X.com/path`, adds `https://`, lowercases host, strips trailing slash and fragment, rejects bare words with no TLD (the `robertaspizza` case).
   - `validate_phone()`: requires ≥7 digits after stripping junk.
   - `validate_rating()`: requires a number in [0, 10].
   - `validate_and_normalize(col, value)` dispatch entry point keyed on column name sets (`_WEBSITE_COLS`, `_PHONE_COLS`, `_RATING_COLS`). Unknown columns pass through unchanged.
   - Wired into `extractor.py` inside the cell-parse loop: any cell that fails validation is dropped with a debug log, never entering the draft.

2. **`app/services/cell_verifier.py`** (new) — per-cell entity-alignment check, applied after merge+prune and again after gap-fill.
   - Three-tier rule: (1) evidence snippet mentions the entity name (exact substring after `normalize_name`, OR rapidfuzz `partial_ratio ≥ 80`), (2) source title mentions the name, (3) cell's source URL shares domain with the entity's own website cell (`domains_match`). Any of the three → aligned.
   - Cells that pass no rule are _penalized_, not deleted: `confidence ← round(confidence × 0.6, 3)`. Value and provenance remain visible; the ranker deprioritizes.
   - `_SKIP_COLS` excludes `name`, `cuisine_type`, `category`, `type`, `description`, `overview`, `summary` — short/weak-signal columns rarely contain the entity name and would be penalized noisily.
   - Row `aggregate_confidence` is recomputed when any cell gets penalized so the downstream ranker sees the adjusted value.

3. **`app/services/ranker.py`** — added `_source_diversity(row)` and rebalanced the score.
   - Diversity = `1 - (max_domain_share / total_cells)`. 1.0 = every cell from a different domain; 0.0 = all cells from one domain.
   - New weights: `completeness=0.25, avg_confidence=0.20, source_support=0.08, actionable=0.07, source_quality=0.32, source_diversity=0.08`. Source quality still dominates; diversity gets a modest tie-breaker weight.

**Why this change was chosen:**

- Alternative 1 — _hard-delete_ weakly-aligned cells. Rejected: a borderline-correct fact (e.g. name appears in paragraph 3, not in the 200-char snippet) would vanish without a trace. Penalizing preserves the row-level transparency that Iteration 7 stressed.
- Alternative 2 — ask the LLM "does this evidence describe this entity?". Rejected: adds a per-cell LLM call (cost + latency) for a problem that rule-based fuzzy matching solves adequately.
- Alternative 3 — reject non-aligned cells during _extraction_. Rejected: the extractor sees one page at a time and does not yet know what canonical entity name the row will settle on after merging. Post-merge is the correct seam.
- Alternative 4 — treat source diversity as a hard gate. Rejected: some queries have one authoritative source (official site) and nothing else — a gate would wipe out legitimate rows. A soft weight (0.08) nudges without excluding.

**What happened when tested:**

- 121 tests pass. Added `tests/test_cell_verifier.py` (7 tests: entity-name-in-evidence kept, missing-name penalized to 0.9 × 0.6 = 0.54, own-domain cell kept without name mention, weak-signal columns skipped, aggregate recompute, graceful skip when no name cell, title-based verification path) and `tests/test_field_validator.py` (18 tests across URL, phone, rating, dispatch). Added a source-diversity ranker test that confirms a multi-domain row outranks a single-domain row when all other signals are equal.
- One bug caught by tests: `_is_url_like()` and the scheme-prefix check in `normalize_website()` were case-sensitive, so `HTTPS://Lucali.COM/menu` was being rejected. Fixed by lower-casing before the `startswith` compare.

**Failures / issues observed:**

- The three-rule alignment check is intentionally generous. Short names (e.g. "Joe's") can false-match on fuzzy `partial_ratio`. Relying on `normalize_name` substring as the strongest signal mitigates this but does not eliminate it.
- Domain-match alignment depends on the row already having a `website` (or alias) cell. Rows that never pick up an official site rely entirely on snippet/title mentions.
- The 0.6× penalty is a chosen constant. A smaller penalty would under-weight the signal; a larger one would crowd out legitimate borderline cells. Kept tunable in the module header.

**Resulting improvement:**

- The `Espresso Pizzeria` / `F&F Pizzeria` style mix-up is now caught post-merge: the phone cell's confidence drops from 0.9 → 0.54, and the row's aggregate is recomputed, so a clean row outranks it.
- Malformed values like `robertaspizza` never enter the draft. Cells that survive extraction are guaranteed to be structurally valid for their column type.
- The ranker now favors rows assembled from independent evidence. A single marketplace page that "completes" a row no longer scores the same as three corroborating sources.

**Tradeoffs introduced:**

- Field validators are strict by intent — a valid website with an exotic TLD could be rejected if the TLD regex tightens further. Current rule (`[a-z]{2,}`) is permissive.
- Re-running `verify_rows_cells` after gap-fill doubles the pass. It is cheap (rule-based, no I/O) but the wiring is load-bearing: if gap-fill is added to or re-ordered, the second call must move with it.
- Source-diversity weighting shifts ranking subtly. A row with one very-high-quality official source now competes on slightly worse footing with a row assembled from three medium-quality sources. Intentional — official-source still dominates via the 0.32 `source_quality` weight.

**Files/modules affected:**
`app/services/field_validator.py` (new), `app/services/cell_verifier.py` (new), `app/services/extractor.py` (wiring), `app/services/ranker.py` (diversity + rebalanced weights), `app/api/routes_search.py` (wiring), `tests/test_field_validator.py` (new), `tests/test_cell_verifier.py` (new), `tests/test_ranker.py` (new diversity test).

**Next step:**
Phase 4 — small evaluation harness: a handful of diverse queries, summary metrics (rows returned, fill rate, actionable-field rate, multi-source rate, avg source quality, avg diversity), and an optional ablation mode to measure the marginal contribution of reranker + cell verifier + diversity scoring.

---

### Iteration 11 — Evaluation Harness

**Date:** 2026-04-04
**Author:** User (directed), Claude (implementation)
**Goal:** Provide a repeatable, metrics-driven way to assess pipeline quality across diverse query categories instead of relying on ad-hoc manual spot-checks.

**Issue:**
All improvements through Iteration 10 were validated only by unit tests and occasional manual queries. There was no systematic way to answer: "Is the pipeline better after feature X?" or "Which query categories are weakest?"

**What was built:**

1. **`docs/eval_queries.json`** — 10 eval queries across 3 categories (food, tech, travel). Each query has an `id`, `category`, and `notes` field explaining what it tests.
2. **`scripts/eval.py`** — CLI evaluation harness that:
   - Submits each query to the running server via `POST /api/search`
   - Polls `GET /api/search/{job_id}` until completion or timeout (180s)
   - Computes per-query metrics: `rows_returned`, `fill_rate` (cells/columns), `actionable_rate` (rows with ≥1 actionable field), `multi_source_rate` (rows with >1 distinct source URL), `avg_aggregate_confidence`, `avg_source_diversity`, `duration_seconds`
   - Computes aggregate summary: means across all successful queries
   - Writes JSON report (`data/eval_<tag>_<ts>.json`) and CSV (`data/eval_<tag>_<ts>.csv`)
   - Supports `--category` filtering, `--tag` labeling (for ablation runs like `no-rerank`), and `--base-url` override

**Design choices:**

- Harness is external to the app — it's a plain Python script using `httpx`, not wired into the FastAPI app. This keeps it decoupled and safe to remove.
- No assertions or pass/fail thresholds. The harness produces metrics; interpretation is manual. Premature thresholds would create false confidence.
- Source diversity is recomputed from raw cell source URLs rather than relying on internal ranker state, giving an independent cross-check.
- Ablation is tag-based: run once with full pipeline, once with env var `RERANK_ENABLED=false`, compare the two JSON reports. No in-script feature toggles beyond what the server config supports.

**Tradeoffs:**

- Requires a running server — cannot run in CI without spinning up the full stack + API keys. Acceptable for a project that already requires Brave + OpenAI keys.
- 10 queries × ~40s each = ~7 min per full eval run. Enough to detect regressions without burning excessive API credits.
- No ground-truth labels. Metrics are proxy quality signals (fill rate, actionable rate, diversity), not precision/recall against a gold dataset. Honest about this limitation.

**Files/modules affected:**
`scripts/eval.py` (new), `docs/eval_queries.json` (new).

**Next step:**
Phase 5 — README updates documenting all improvements with honest assessment of what is heuristic vs. proven.

---

### Iteration 12 — UI Overhaul for Reviewer Impact

**Date:** 2026-04-04
**Author:** User (directed), Claude (implementation)
**Goal:** Make the UI communicate the pipeline's retrieval planning, quality controls, and per-row trust signals — not just show a table.

**Problem:**
The original UI was a search box, a spinner, a metadata bar, and a table with a per-cell evidence modal. It was functional but communicated almost none of what the pipeline actually does:

- No progress feedback during the 15–60s pipeline run
- No visibility into what the planner decided (schema, facets, angles)
- No indication of which quality controls ran (reranking, dedup, verification)
- No row-level trust signals — all rows looked identical
- No latency/throughput information
- Error and empty states were minimal

**What was built:**

1. **Phase tracker** — horizontal 8-stage pipeline indicator (planning → searching → scraping → reranking → extracting → merging → gap-fill → verifying). Each stage has a dot that transitions pending → active (pulsing) → done (green checkmark). A live elapsed timer counts wall-clock seconds during the run. This gives the reviewer immediate feedback that the system is multi-stage, not a single LLM call.

2. **Results summary strip** — after completion, shows entity type, row count, URLs considered, pages scraped, pages sent to extraction, gap-fill yes/no, and duration. All values from `SearchMetadata`.

3. **Retrieval plan panel** — collapsible panel showing entity type, columns, each typed facet (type badge, query, expected fill columns), and reranking stats (pages before/after, scorer type). Makes the planning stage inspectable.

4. **Quality controls panel** — lists the verification and filtering steps that ran: cross-encoder reranking with page counts, entity deduplication with merge counts, gap-fill enrichment, cell-level verification enabled/disabled, field validation, source quality/diversity scoring, and final row filtering. Shows what was active based on actual metadata — does not fabricate precision.

5. **Run stats panel** — compact stats table: URLs considered, pages scraped, pages after rerank, entities extracted, entities after merge, gap-fill used, duration.

6. **Trust badges on table rows** — new "Trust" column in the results table. Each row gets badges computed from actual cell data:
   - Sources count badge (green if ≥ 3, blue if 2, yellow if 1)
   - Confidence tier (high ≥ 0.75, medium ≥ 0.5, low < 0.5)
   - Source type badges (official/editorial/directory/marketplace) — classified by matching cell source URLs against the same domain sets used by `source_quality.py`
   - Single-source warning when all cells come from one domain

7. **Enhanced evidence modal** — source type badge, source title, confidence bar, and validation flags (low confidence, single source) alongside the existing evidence snippet and source URL.

8. **Empty state** — "how it works" explainer with 5 labeled pipeline steps. Shown when no search has been run.

9. **Error/no-results banners** — error banner with hint text, no-results banner with query suggestions.

**Design decisions:**

- Frontend source classification mirrors `source_quality.py` domain sets — keeps trust signals consistent between backend scoring and UI display. This is intentional duplication for a different layer (display vs ranking).
- All panels are collapsible (CSS `<details>`) — reviewer can drill into what interests them without visual overload.
- Phase tracker uses CSS animations (pulse on active dot) — lightweight, no JS animation library.
- Trust badges are pure frontend computation from the API response — no backend changes needed.
- Kept vanilla JS + CSS + Jinja2 — no framework, no build step.

**Files rewritten (full rewrites, originals backed up as .bak):**
`templates/index.html`, `static/app.js`, `static/style.css`.

**Tradeoffs:**

- Frontend source classification could drift from backend if domain lists change in `source_quality.py`. Acceptable because the UI badges are informational, not scoring inputs.
- Collapsible panels default to closed — reviewer must click to see details. Intentional to avoid overwhelming the initial view.
- Trust badges are heuristic (based on URL domain matching) — a source that relocated domains would be misclassified. Same limitation as the backend.

---

### Iteration 13 — Groq LLM Provider Migration

**Date:** 2026-04-04
**Author:** User (directed), Claude (implementation)
**Goal:** Switch the primary LLM provider from OpenAI to Groq for faster inference, while keeping OpenAI as a transparent fallback.

**Problem:**
The system was hardcoded to use OpenAI via `OPENAI_API_KEY`, `OPENAI_MODEL`, and `OPENAI_BASE_URL`. While `OPENAI_BASE_URL` allowed pointing at any OpenAI-compatible endpoint, there was no explicit support for Groq as a first-class provider, no provider detection, and no fallback logic.

**Why Groq:**
Groq offers significantly faster inference (especially for Llama models) via an OpenAI-compatible API. The system already uses `response_format={"type": "json_object"}` which Groq supports for Llama 3.1+ models. No new dependencies are needed — the existing `openai` Python library works with Groq's endpoint via base_url override.

**What was built:**

1. **Config properties (`config.py`):**
   - Added `groq_api_key`, `groq_model` (default: `llama-3.1-70b-versatile`), `groq_base_url` (default: `https://api.groq.com/openai/v1`).
   - Added 4 computed properties: `llm_provider` (returns `"groq"` if `groq_api_key` is set, else `"openai"`), `active_api_key`, `active_model`, `active_base_url`. All downstream code uses these properties — never references `openai_*` or `groq_*` fields directly.
   - Kept all `openai_*` fields for backward compatibility — existing `.env` files with only `OPENAI_API_KEY` continue to work.

2. **LLM client (`llm.py`):**
   - `_get_client()` now uses `settings.active_api_key` and `settings.active_base_url` instead of the old `settings.openai_api_key` / `settings.openai_base_url`. Logs the active provider, model, and base URL on first init.
   - Added `_extract_json(raw)` — a two-stage JSON parser that tries direct `json.loads` first, then extracts from \`\`\`json markdown fences. This handles a known Llama behavior where models sometimes wrap valid JSON in markdown code fences even when `response_format={"type": "json_object"}` is requested.
   - `chat_json()` uses `settings.active_model` and `_extract_json()` instead of `settings.openai_model` and bare `json.loads`.

3. **Startup log (`main.py`):**
   - Changed from `model=%s` to `llm=%s model=%s` showing the active provider and model at startup.

4. **Environment:**
   - `.env` updated with `GROQ_MODEL=llama-3.1-70b-versatile`.
   - `.env.example` created with Groq as the documented primary and OpenAI as commented-out fallback.

**Why `_extract_json()` was needed:**
Llama 3.1 models on Groq generally respect `response_format={"type": "json_object"}` and return raw JSON. However, in some edge cases (long prompts, complex schemas), the model wraps the JSON in \`\`\`json fences. Rather than adding a preprocessing step per caller, the centralized `_extract_json` handles this transparently. OpenAI models never exhibit this behavior, so the fallback path is a no-op for them.

**Testing strategy:**
- Unit tests pass because `llm.py` is mocked in all tests (no live API calls).
- The provider-agnostic properties can be tested by setting `GROQ_API_KEY` to empty (falls back to OpenAI) or non-empty (activates Groq).
- Full integration requires a valid `GROQ_API_KEY` and a running Groq-compatible endpoint.

**Files changed:** `app/core/config.py`, `app/services/llm.py`, `app/main.py`, `.env`.
**Files created:** `.env.example`.

**Tradeoffs:**

- Groq's free tier has rate limits (requests per minute, tokens per minute). The existing retry logic in `chat_json()` handles `RateLimitError` with exponential backoff, which should absorb transient rate-limit hits.
- Llama 3.1 70B is a strong general-purpose model but may produce slightly different JSON structure than GPT-4o-mini. The Pydantic validation layer catches structural mismatches; `_extract_json` catches formatting differences.
- No provider-switching UI — the provider is determined at startup by which API key is set. This is intentional: the system runs one provider per session, not per-request.

---

## Overall Progress Summary

**Phase 1 (Iterations 1–2) — Reliability:**  
Built and stabilised the pipeline. Fixed crashes from the reload watcher, LLM hangs, extraction semaphore, stale job state, and polling fragility. Outcome: pipeline reliably reaches completion on a clean start.

**Phase 2 (Iterations 3–5) — Extraction Precision:**  
Fixed the chunker OOM crash, tightened the extraction semaphore to the chunk level, refocused gap-fill to target-entity queries, backfilled missing `name` cells, refreshed merger lookup after absorptions, and introduced row pruning with actionable-field scoring. Outcome: more rows keep usable name + actionable attributes; wrong-entity enrichment substantially reduced.

**Phase 3 (Iterations 6–7) — Source Trust:**  
Added `source_quality.py` (confidence-weighted source scoring) and `verifier.py` (row-level filter for marketplace-only and low-evidence rows). Wired into pipeline as a second prune/rank pass after gap-fill. Outcome: marketplace-only rows removed for strict queries; top results now backed by editorial or official sources more often.

**Phase 4 (Iterations 8–9) — Retrieval Quality:**  
Replaced paraphrase search angles with typed retrieval facets (`entity_list`, `official_source`, `editorial_review`, `attribute_specific`, `news_recent`, `comparison`, `other`). Added cross-encoder reranking (ms-marco-MiniLM-L-6-v2) with Jaccard lexical fallback between scrape and extraction. Outcome: extraction sees higher-relevance pages; planner output is more structured and predictable.

**Phase 5 (Iteration 10) — Cell-Level Integrity:**  
Added per-cell entity-alignment verification (fuzzy name match in evidence/title/domain), field-type validation at the extraction boundary (URL/phone/rating), and source-diversity scoring in the ranker. Outcome: cross-entity cell contamination penalized rather than silently accepted; malformed values rejected before entering the pipeline; single-domain rows down-ranked.

**Phase 6 (Iteration 11) — Evaluation:**  
Built a repeatable CLI evaluation harness (`scripts/eval.py`) with 10 queries across food/tech/travel categories. Metrics: rows returned, fill rate, actionable rate, multi-source rate, confidence, source diversity, duration. JSON + CSV output for comparison across runs, tag-based ablation support.

**Phase 7 (Iteration 12) — UI for Reviewer Impact:**  
Rewrote the frontend to communicate the pipeline's process and trust signals. Added phase tracker, retrieval plan panel, quality controls panel, run stats panel, row-level trust badges, enhanced evidence modal, and empty/error states. Same tech stack (Jinja2 + vanilla JS + CSS), no framework.

**Phase 8 (Iteration 13) — LLM Provider Migration:**  
Switched primary LLM from OpenAI to Groq. Added provider-agnostic config properties, markdown-fence JSON extraction fallback, startup provider logging. OpenAI remains as transparent fallback when Groq API key is not set.

**Remaining gap:**  
No ground-truth labels — metrics are proxy signals, not precision/recall.

---

## Key Design Decisions

### Decision: Use Brave Search API

**Context:** Need web search for entity discovery.  
**Why chosen:** Clean REST API, reasonable free tier, good English-language web coverage, no JS rendering complications.  
**Alternatives considered:** Google CSE (quota restrictions), SerpAPI (paid), DuckDuckGo (no official API).  
**Tradeoffs:** Lower coverage than Google for long-tail queries. No dedicated news endpoint.  
**Status:** Kept.

---

### Decision: trafilatura-first, BeautifulSoup fallback

**Context:** Raw HTML contains nav bars, ads, sidebars — all pollute LLM context.  
**Why chosen:** trafilatura is purpose-built for article/content extraction, handles boilerplate removal well.  
**Tradeoffs:** trafilatura sometimes returns too little text from JS-heavy pages. BS4 fallback handles those with noisier output.  
**Status:** Kept.

---

### Decision: Job-based async model (POST + poll)

**Context:** Pipeline takes 20–60s. HTTP timeouts typically 30–60s.  
**Why chosen:** Returning `job_id` immediately avoids timeouts. Polling every 2s is simple to implement and debug.  
**Alternatives considered:** Server-sent events, WebSockets — add connection management complexity with no meaningful benefit.  
**Status:** Kept.

---

### Decision: LLM JSON mode, not function-calling

**Context:** Need structured JSON output.  
**Why chosen:** `response_format={"type": "json_object"}` works across OpenAI-compatible providers. Function-calling varies in behavior across Groq/Together/Mistral.  
**Tradeoffs:** No schema enforcement from the API — requires Pydantic validation layer.  
**Status:** Kept.

---

### Decision: Per-cell provenance at extraction time

**Context:** Core requirement — every cell must be traceable to its source.  
**Why chosen:** Extracting evidence_snippet at the same time as the value is the only way to get faithful verbatim quotes. Post-hoc attribution would be lossy.  
**Tradeoffs:** Longer extraction prompts and larger LLM output. Acceptable.  
**Status:** Kept — defining feature of the system.

---

### Decision: Targeted gap-fill, bounded to 1 round

**Context:** After first-pass extraction, some entities have 2–3 filled columns out of 7.  
**Why chosen:** A second targeted pass measurably improves completeness without rebuilding the whole pipeline.  
**Why not recursive:** An unbounded loop is hard to reason about and expensive. 3 entities × 2 URLs × 1 round keeps cost predictable.  
**Status:** Kept.

---

### Decision: Source quality scoring as a ranking signal

**Context:** Rows from delivery/marketplace pages ranked high due to confident but low-value extractions.  
**Why chosen:** Source type is orthogonal to extraction confidence. A delivery app can confidently extract `name + cuisine_type` — still weaker evidence than an editorial review.  
**Tradeoffs:** Domain classification lists are hand-curated and food/startup-biased. Other domains get `unknown` (neutral, not penalizing).  
**Status:** Kept. Domain lists can be extended per use case.

---

### Decision: Verifier fallback — never return empty results

**Context:** Overly aggressive filtering could remove all rows from a valid query.  
**Why chosen:** Better to return imperfect results than nothing. User can see low-quality rows are present and judge accordingly.  
**Tradeoffs:** Means the verifier is a soft filter, not a hard gate. Low-quality rows can still appear if they are the only rows.  
**Status:** Kept.

---

## Failure / Debug Log

### Issue: Server restarts killing background tasks mid-pipeline

**Detected in:** Iteration 1, first live run  
**Symptoms:** Job shows `status=running, phase=scraping` forever. Server log shows watchfiles restart immediately after scraping completes.  
**Root cause:** `uvicorn --reload` watches entire project directory. `aiosqlite` writes to `data/agentic_search.db` are file changes → worker restart → `BackgroundTasks` coroutine cancelled.  
**Fix:** Moved DB to `/tmp/agentic_search.db`. Changed start command to `--reload-dir app`.  
**Status:** Resolved.

---

### Issue: LLM extraction hangs indefinitely

**Detected in:** Iteration 1, first 13-page extraction  
**Symptoms:** Logs stop after scraping. No error. Worker memory grows. Job stays in `extracting` forever.  
**Root cause:** `AsyncOpenAI` default timeout 600s. All 13 × 2 chunks fire simultaneously, saturating rate limit. Most calls stall.  
**Fix:** `AsyncOpenAI(timeout=60.0)`, `Semaphore(3)` shared globally across all chunk calls.  
**Status:** Resolved.

---

### Issue: Server crash (`Killed: 9`) during extraction on large pages

**Detected in:** Iteration 2, large page processing  
**Symptoms:** Process killed by OS. No Python exception. Happened reliably on pages with very long cleaned text.  
**Root cause:** `chunk_text()` infinite loop: when `end == length`, `start = end - overlap_chars` moved backward, re-generating the same tail chunk forever. Memory grew until OOM kill.  
**Fix:** Break immediately when `end >= length`. Force `start` to advance by at least 1. Add `max_chunks` hard cap.  
**Status:** Resolved.

---

### Issue: Semaphore not bounding chunk-level LLM concurrency

**Detected in:** Iteration 2/3, extraction still occasionally saturating rate limit  
**Symptoms:** Rate limit errors despite `Semaphore(3)` being in place.  
**Root cause:** Semaphore was at the page level. Inside each page, `asyncio.gather(*[_extract_from_chunk(...)])` fired all chunks concurrently regardless of semaphore state. 5 pages × 2 chunks = 10 simultaneous calls.  
**Fix:** Single `llm_sem` instance created in `extract_from_pages()` and passed down to every individual chunk call.  
**Status:** Resolved.

---

### Issue: Polling permanently stopped by transient network error

**Detected in:** Iteration 1, browser testing during WiFi reconnect  
**Symptoms:** Browser shows "Polling failed: Failed to fetch". Pipeline still running server-side. No recovery without manual page refresh.  
**Root cause:** `catch` block in `pollJob()` called `clearInterval(pollTimer)` unconditionally — any exception killed all future polls.  
**Fix:** `_pollErrors` counter. Log warnings for transient failures. Only stop after 8 consecutive failures.  
**Status:** Resolved.

---

### Issue: Stale `running` jobs after server restart

**Detected in:** Iteration 2  
**Symptoms:** Old job IDs return `status=running` after fresh server start. UI polls forever.  
**Root cause:** Background tasks are in-process — killing the process abandons them with no DB cleanup.  
**Fix:** `init_db()` runs `UPDATE query_jobs SET status='failed' WHERE status IN ('running','pending')` on every startup.  
**Status:** Resolved.

---

### Issue: HTTP 500 on every page load

**Detected in:** Iteration 1, first browser open  
**Symptoms:** `GET /` returns 500. Error: `TypeError: unhashable type: 'dict'`  
**Root cause:** Starlette 1.0 changed `TemplateResponse` signature. Old: `TemplateResponse("index.html", {"request": request})`. New: `TemplateResponse(request=request, name="index.html")`.  
**Fix:** One-line change in `app/main.py`.  
**Status:** Resolved.

---

### Issue: Duplicate entities from stale merger lookup

**Detected in:** Iteration 3, test-driven  
**Symptoms:** Three drafts for same entity (name + later domain match) produce 2–3 rows instead of 1.  
**Root cause:** `merger.py` `lookup` never updated after a draft was absorbed. Website added by draft 2 not reflected in lookup — draft 3's domain match fails.  
**Fix:** Update `lookup[idx]` with current `_name_str` and `_website` after every absorption.  
**Status:** Resolved.

---

### Issue: Gap-fill absorbing data from wrong entity

**Detected in:** Iteration 4, pizza query evaluation  
**Symptoms:** `Espresso Pizzeria` row received `address` and `phone_number` belonging to a different restaurant on the same page.  
**Root cause:** Gap-fill searched with the full original query, extracted with the full schema, and accepted any draft returned from the page without checking entity name similarity.  
**Fix:** Focused extraction query (entity name only), reduced plan (only missing columns), name-similarity filter on all returned drafts.  
**Status:** Partially resolved — improved significantly, but cell-level entity consistency still imperfect in edge cases.

---

### Issue: `name` cell missing despite entity being found

**Detected in:** Iteration 4, extraction output analysis  
**Symptoms:** Rows had useful cells but no `name` cell. Downstream `is_row_viable()` rejected them. Gap-fill skipped them.  
**Root cause:** LLM returned valid `entity_name` in the outer dict but omitted the `"name"` key inside `cells`.  
**Fix:** Auto-backfill `cells["name"]` from `entity_name` at confidence 0.75 when `"name"` is in schema but missing from cells.  
**Status:** Resolved.

---

## Before vs After Improvements

### Improvement: Pipeline completion reliability

**Before:** Pipeline stalled at extraction on ~80% of runs. Either OOM crash (chunker loop), or silent hang (no LLM timeout), or worker restart (--reload).  
**After:** Consistent completion. 60s hard timeout, global semaphore, DB in `/tmp`, chunker loop fixed.  
**What caused improvement:** Iterations 2 and 3 — three independent fixes working together.

---

### Improvement: Entity deduplication accuracy

**Before:** Same entity appearing in 3 pages could produce 2–3 rows if website arrived after initial lookup was built.  
**After:** Lookup updated after every absorption. All drafts for same entity correctly collapse.  
**What caused improvement:** Merger lookup refresh in Iteration 4.

---

### Improvement: Gap-fill precision

**Before:** Gap-fill could attach facts from a different entity on the same page. Wrong phone numbers, wrong addresses absorbed into target rows.  
**After:** Focused queries, reduced extraction plan, name-similarity filter on returned drafts. Cross-entity contamination significantly reduced.  
**What caused improvement:** Gap-fill refactor in Iteration 4.

---

### Improvement: Output signal-to-noise

**Before:** Results included name-only rows, rows with only `cuisine_type`, delivery-app rows. All technically valid, all useless.  
**After:** Two-pass pruning removes non-viable rows. Source quality scoring down-ranks marketplace sources. Verifier removes marketplace-only rows on strict queries.  
**What caused improvement:** Iterations 5 and 6 working together.

---

### Improvement: Observability

**Before:** Logs went silent for 30–60s during extraction. Impossible to know if pipeline was alive or dead.  
**After:** Every page logs start + entity count. Phases log elapsed time. Failures produce `=== Pipeline FAILED ===` with full traceback.  
**What caused improvement:** Iteration 2 logging changes.

---

## Current Architecture

### Pipeline stages (in order)

```
POST /api/search
  │
  ▼
1. planner.py         — LLM infers entity_type, columns (5–8), typed retrieval facets
2. brave_search.py    — parallel Brave API calls (facet queries × 5 results), URL dedup
3. scraper.py         — async fetch (semaphore=5), trafilatura→BS4, SQLite cache (24h TTL)
3.5 reranker.py       — cross-encoder rerank (ms-marco-MiniLM-L-6-v2) or Jaccard fallback
4. extractor.py       — LLM extraction per page + field_validator at cell boundary
                        30s timeout per call, 1 attempt (fail-fast per page)
5. merger.py          — rapidfuzz + domain dedup, best-confidence cell wins, lookup refresh
6. prune_rows()       — remove non-viable rows (no name, or name-only with weak columns)
7. rank_rows()        — completeness(0.25) + confidence(0.20) + source_quality(0.32)
                        + source_support(0.08) + actionable(0.07) + source_diversity(0.08)
7.5 cell_verifier.py  — per-cell entity-alignment check, 0.6× confidence penalty
8. gap_fill.py        — focused queries for top-3 sparse rows, name-verified extraction
8.5 cell_verifier.py  — second pass after gap-fill
9. verify_rows()      — marketplace-only filter, source quality threshold, fallback
10. prune + re-rank   — second cleanup pass with final ordering
11. complete_job()    — write result JSON to SQLite
```

### Module status

| Module               | Author        | Status     | Notes                                                                           |
| -------------------- | ------------- | ---------- | ------------------------------------------------------------------------------- |
| `planner.py`         | Claude        | Stable     | Good schema quality on gpt-4o-mini                                              |
| `brave_search.py`    | Claude        | Stable     | 15–25 unique URLs typical                                                       |
| `scraper.py`         | Claude        | Stable     | trafilatura handles most pages; JS-rendered pages skipped                       |
| `extractor.py`       | Claude + user | Stable     | Semaphore + chunker fix in place                                                |
| `merger.py`          | Claude + user | Stable     | Lookup refresh fix applied                                                      |
| `ranker.py`          | Claude + user | Stable     | 6-component score; source_quality dominant (0.32), diversity tie-breaker (0.08) |
| `reranker.py`        | Claude        | Stable     | Cross-encoder ms-marco-MiniLM-L-6-v2 + Jaccard fallback                         |
| `cell_verifier.py`   | Claude        | Stable     | Three-rule alignment, 0.6× penalty, runs twice                                  |
| `field_validator.py` | Claude        | Stable     | URL/phone/rating normalization at extraction boundary                           |
| `gap_fill.py`        | Claude + user | Functional | Entity-focused; residual contamination in edge cases                            |
| `source_quality.py`  | User          | Functional | Domain lists hand-curated; food/startup-biased                                  |
| `verifier.py`        | User          | Functional | Conservative; fallback prevents empty results                                   |
| `exporter.py`        | Claude        | Stable     | JSON + CSV with per-column provenance                                           |
| `llm.py`             | Claude + user | Stable     | Groq primary, OpenAI fallback, markdown fence extraction, 60s timeout |

### What is stable

- Full pipeline from query to ranked table
- Per-cell provenance (source_url, title, snippet, confidence)
- SQLite caching of scraped pages (24h TTL in `/tmp`)
- JSON/CSV export with provenance columns
- Dark-theme UI with phase tracker, trust badges, quality/stats panels, and evidence modal

### What is still weak

- **Cell-level entity consistency:** mostly mitigated by `cell_verifier.py` (Iteration 10), but short entity names can still false-match on fuzzy `partial_ratio`
- **JS-rendered pages:** SPAs return empty content — skipped by `_MIN_TEXT_LENGTH` threshold
- **Source domain lists:** `source_quality.py` is calibrated for food/startup queries; other domains get neutral `unknown` score
- ~~**No URL validation:**~~ **Resolved** in Iteration 10 — `field_validator.py` rejects bare words, normalizes scheme/host
- **Schema planner edge cases:** very broad queries produce generic columns
- ~~**No source diversity constraint:**~~ **Resolved** in Iteration 10 — `_source_diversity()` in `ranker.py` with 0.08 weight
- ~~**No automated evaluation:**~~ **Resolved** in Iteration 11 — `scripts/eval.py` provides a repeatable CLI harness with 10 queries, 7 metrics, and tag-based ablation support

### Intentionally out of scope

- Browser automation (Playwright/Selenium)
- Vector DB / semantic search
- User auth
- Recursive refinement loops
- Background job queue (Redis/Celery)
- Multi-agent orchestration

---

## Known Limitations

1. **JS-rendered pages:** Pages built with React/Next.js that require JS execution return empty or minimal content and are dropped.

2. **LLM hallucination:** Despite prompt constraints, the LLM occasionally assigns moderate confidence to values weakly implied by context. Evidence snippet requirement reduces this but does not eliminate it.

3. **Source classification bias:** `source_quality.py` was calibrated for food/startup queries. Medical, legal, and academic domains fall into `unknown` (neutral score 0.55) — not penalized, but not rewarded either.

4. **Cell-level entity consistency:** When a page discusses multiple similar entities (e.g. two restaurants on the same block), gap-fill can still occasionally absorb a cell from the wrong entity despite the name-similarity filter.

5. ~~**No URL validation:**~~ **Resolved** — `field_validator.py` (Iteration 10) rejects bare words without TLDs, normalizes scheme/host/trailing-slash.

6. **Latency:** 15–45s typical with Groq (25–60s with OpenAI), plus 10–20s for gap-fill. Phase tracker provides live feedback during the wait.

7. **Single-round gap-fill:** Entities still sparse after one enrichment round remain sparse. Second round would improve completeness at doubled cost.

8. **No query caching:** Re-running the same query re-executes the full pipeline. Only page scraping is cached.

9. **Groq rate limits:** Groq's free tier has per-minute request and token caps. The retry logic handles transient rate-limit errors, but sustained high-volume usage requires a paid plan.

10. **Llama JSON reliability:** Llama 3.1 models occasionally wrap valid JSON in markdown code fences even with `json_object` response format. The `_extract_json()` fallback handles this, but very complex schemas may still produce parse failures more often than GPT-4o-mini.

11. **Frontend source classification drift:** The UI trust badges classify source URLs using domain sets that mirror `source_quality.py`. If the backend domain lists are updated without updating the JS, the UI badges may disagree with backend scoring. This is informational only — the backend scoring is what matters for ranking.
