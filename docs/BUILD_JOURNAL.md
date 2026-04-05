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
- **Added (user):** `scripts/eval.py` + `docs/eval_queries.json` — CLI evaluation harness with 10 queries; later expanded with actionable-field and official-site metrics
- **Rebuilt (user):** UI — phase tracker, retrieval plan panel, quality controls panel, trust badges, run stats, enhanced modal, empty/error states
- **Added (user):** Groq as primary LLM provider, OpenAI fallback, `_extract_json` markdown fence handling
- **Fixed (user):** zero-entity regression — Groq decommissioned `llama-3.1-70b-versatile`; updated to `llama-3.3-70b-versatile`
- **Added (user):** 0-entity safeguard log in pipeline — detects likely systemic extraction failure
- **Split (user):** dual-provider routing — planner→OpenAI (gpt-4o-mini), extractor→Groq (llama-3.3-70b-versatile)
- **Fixed (Claude):** broad-query zero-result regression — Groq extractor 429s now retry on OpenAI; added `pipeline_counts` metadata and stage-count logging
- **Added (Claude):** `query_normalizer.py` — safe query cleanup with bounded typo/location fixes and original/normalized query metadata
- **Reworked (Claude):** `planner.py` — constrained query-family planning (`local_business`, `startup_company`, `software_tool`, `organization`, etc.) with schema templates instead of free-form generic schemas
- **Reworked (Claude):** `extractor.py` into discovery + fill modes so candidate recall is separated from attribute completion
- **Added (Claude):** `official_site.py` — canonical/official-site resolution as a first-class step before focused fill
- **Softened (Claude):** early prune/verifier behavior — more rows survive to ranking and late verification
- **Expanded (Claude):** evaluation metrics — actionable-field rate, official-site rate, normalized query/query-family metadata
- **Hardened (Claude):** default demo extractor path reverted to OpenAI; Groq kept optional as alternate/fallback to avoid reviewer-facing 429 churn
- **Hardened (Claude):** late pseudo-entity filtering removes category/list labels such as `"AI Copilots & Agents for Psychiatry"` from final rows
- **Hardened (Claude):** website semantics now prefer canonical homepages and reject article/directory URLs as final `website` values
- **Current state:** normalized-query, discovery-first pipeline with constrained planning, typed retrieval facets, candidate merge, official-site resolution, focused fill, late verification, per-cell provenance, async jobs, exports, evaluation harness, and reviewer-facing UI. OpenAI is now the default planner + demo extractor path, with Groq remaining optional as an alternate/fallback extractor when desired.

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

### Iteration 14 — Groq Model Decommission Fix

**Date:** 2026-04-04
**Author:** User (directed), Claude (implementation)
**Goal:** Debug and fix a regression where every query returns zero entities.

**Symptoms:**

- Every query returns "No entities found" — zero rows regardless of topic.
- Pipeline completes in ~1 second (normally 15–60s).
- Planner falls back to generic `entity` type with hardcoded columns (instead of LLM-inferred schema).
- `entities_extracted=0` despite 8 pages successfully scraped.

**Root cause:**
Groq decommissioned `llama-3.1-70b-versatile` on January 24, 2025. Every API call returned HTTP 400 with error code `model_decommissioned`. The Iteration 13 migration hardcoded this model name. Both `planner.py` and `extractor.py` are designed to be resilient to transient LLM failures — the planner catches exceptions and falls back to a hardcoded plan, the extractor catches exceptions per-page and returns `[]`. This resilience pattern inadvertently masked a permanent configuration error, allowing the pipeline to complete "successfully" with 0 entities.

**Diagnostic path:**

1. Reproduced via `scripts/smoke_test.py` — confirmed 0 entities, fallback plan, 1.05s.
2. Traced pipeline counts: `urls=9, scraped=8, reranked=8, extracted=0` — collapse at extraction.
3. Server logs showed: `Error code: 400 - model_decommissioned` on every LLM call.
4. Verified at https://console.groq.com/docs/deprecations — model deprecated 2025-01-24.
5. Confirmed replacement `llama-3.3-70b-versatile` is active production model on Groq.

**Fix (3 files):**

- `app/core/config.py` — default `groq_model` changed from `llama-3.1-70b-versatile` to `llama-3.3-70b-versatile`.
- `.env` — `GROQ_MODEL=llama-3.3-70b-versatile`.
- `.env.example` — same.

**Safeguard added:**

- `app/api/routes_search.py` — after extraction, when `entities_extracted == 0` and `pages_scraped >= 3`, log an ERROR: "0 entities from N pages — likely systemic failure (model misconfiguration, API error, or prompt incompatibility)." This is log-only — does not change pipeline behavior.

**Validation:**

- Smoke test "open source database tools": 7 entities, 10.17s. Pipeline progressed through all phases.
- Smoke test "AI startups in healthcare": 13 entities, 9.39s.
- `pytest`: 129/129 passed (tests mock LLM — unaffected by model name change).

**Why the failure was silent:**
The planner and extractor error-handling was designed for transient failures (timeouts, rate limits, network errors). A model decommission is a permanent 400 error that looks structurally identical to a transient API error. The system has no way to distinguish "temporary hiccup" from "this model will never work again" without inspecting the error code. The safeguard log added here detects the downstream effect (0 entities from many pages) rather than the upstream cause, which is a pragmatic tradeoff — it catches any systemic extraction failure, not just model decommission.

**Files changed:** `app/core/config.py`, `app/api/routes_search.py`, `.env`, `.env.example`.
**Files created:** `scripts/smoke_test.py` (reusable HTTP smoke test harness).

---

### Iteration 15 — Split-Provider Routing (Planner→OpenAI, Extractor→Groq)

**Date:** 2026-04-04
**Author:** User (directed), Claude (implementation)
**Goal:** Route planning to OpenAI for structural reliability, extraction to Groq for speed.

**Previous state:**
Single-provider model — either Groq or OpenAI for all LLM calls, chosen at startup by which API key is set. `llm.py` had a single `_client` singleton. Both planner and extractor used the same model.

**Why split:**

- **Planning** requires structured schema inference (entity_type, columns, typed facets). GPT-4o-mini is more reliable at producing well-formed JSON with correct structure at low cost (~$0.15/$0.60 per M tokens).
- **Extraction** is the bulk workload (8–16 LLM calls per query, one per page chunk). Groq’s Llama 3.3 70B runs at ~280 tokens/sec inference — significantly faster than OpenAI for the same quality tier, reducing pipeline latency.
- Single-provider approach meant choosing between reliability (OpenAI for everything, slower) or speed (Groq for everything, weaker planning). The split gets both.

**What was built:**

1. **Config (`config.py`):**
   - Added `planner_provider` (default: `"openai"`) and `extractor_provider` (default: `"groq"`) as env-settable fields.
   - Added `provider_config(provider: str) -> tuple[str, str, str | None]` method returning (api_key, model, base_url) for any named provider. Eliminates scattered conditionals.
   - Kept legacy `llm_provider`, `active_api_key`, `active_model`, `active_base_url` properties for backward compat.

2. **LLM wrapper (`llm.py`):**
   - Replaced single `_client` singleton with `_clients: dict[str, AsyncOpenAI]` — one per provider, lazily created on first use.
   - `_get_client(provider)` returns `(client, model)` tuple. When `provider=None`, falls back to legacy single-provider path.
   - `chat_json()` and `chat_json_validated()` accept an optional `provider` kwarg. Callers that pass it get routed to the correct client/model. Callers that don’t still work via the legacy path.
   - Each client is logged on creation: `LLM client [openai]: model=gpt-4o-mini  base_url=(default)`.

3. **Planner (`planner.py`):**
   - Now passes `provider=settings.planner_provider` to `chat_json_validated()`.
   - Default routes to OpenAI.

4. **Extractor (`extractor.py`):**
   - Now passes `provider=settings.extractor_provider` to `chat_json()`.
   - Default routes to Groq. Gap-fill also uses the extractor path, so it inherits Groq routing automatically.

5. **Startup log (`main.py`):**
   - Changed from `llm=groq model=llama-3.3-70b-versatile` to `planner=openai/gpt-4o-mini  extractor=groq/llama-3.3-70b-versatile`.

6. **Environment:**
   - `.env` updated with `PLANNER_PROVIDER=openai`, `EXTRACTOR_PROVIDER=groq`.
   - `.env.example` rewritten with both providers as required (not fallback).
   - `README.md` env var table updated.

**Testing:**

- 8 new tests in `tests/test_provider_routing.py`: config routing (openai/groq), default routing values, override support, legacy property backward compat, planner provider passthrough, extractor provider passthrough.
- 2 existing extractor tests updated (added `extractor_provider` to `SimpleNamespace` mocks).
- **137/137 tests pass.**
- Smoke test: pipeline completed end-to-end with split providers. Startup log confirmed `planner=openai/gpt-4o-mini extractor=groq/llama-3.3-70b-versatile`. Extraction degraded by Groq free-tier rate limiting (429s on TPM), not by the routing change.

**What was NOT verified live:**

- Planner LLM output quality comparison (OpenAI vs Groq for planning) — would require multiple A/B test runs.
- Extraction speed difference — Groq rate limiting masked any latency improvement on free tier.

**Files changed:** `app/core/config.py`, `app/services/llm.py`, `app/services/planner.py`, `app/services/extractor.py`, `app/main.py`, `.env`, `.env.example`, `README.md`, `tests/test_extractor.py`.
**Files created:** `tests/test_provider_routing.py`.

**Tradeoffs:**

- Two API keys are now required instead of one. If either is missing, the provider’s calls will fail. The system does not validate keys at startup — failures surface as API errors at call time.
- Groq rate limits are stricter on free tier (12K TPM). With concurrent extraction across 8+ pages, rate-limit 429s are common. The existing retry logic handles `RateLimitError` with backoff but only retries within the `attempts` budget (default 1 for extraction — no retries).
- The routing vars (`PLANNER_PROVIDER`, `EXTRACTOR_PROVIDER`) accept any string. There’s no validation that the string maps to a real provider with a configured API key. Invalid values will fail at runtime when `provider_config()` returns empty credentials.
- Legacy single-provider mode still works: if `provider` kwarg is not passed to `chat_json`, it uses the old `active_*` properties. No existing code paths rely on this anymore, but it’s preserved for safety.

---

### Iteration 16 — Results Table Layout Overhaul

**Date:** 2025-07-25
**Author:** User (directed), Claude (implementation)
**Goal:** Make the results table wider, more stable, and reviewer-ready — especially for wide/dynamic schemas.

**Previous state:**
The results section was constrained inside `.app { max-width: 1200px }`, leaving excessive whitespace on either side of the table. All data columns had identical `min-width: 140px / max-width: 220px` regardless of content type. No sticky header — scrolling long tables lost column context. No zebra striping or visual rhythm. Wide schemas with many columns collapsed awkwardly.

**What was built:**

1. **Breakout container (`style.css`):**
   - `.results-section` now breaks out of the 1200px app container using `margin-left: calc(-50vw + 50%)` and `max-width: 95vw`. The search form and panels remain centered; the table spans nearly full viewport width.
   - Mobile responsive: collapses to `100vw` with minimal padding at `≤700px`.

2. **Sticky header (`style.css`):**
   - Table header (`th`) is `position: sticky; top: 0; z-index: 10` inside the scroll container.
   - `max-height: 80vh` on `.table-scroll` ensures vertical scrolling triggers the sticky behavior for long result sets.

3. **Zebra striping (`style.css`):**
   - `tbody tr:nth-child(even) td` gets a subtle dark tint (`rgba(30, 34, 50, 0.45)`). Row hover still overrides with `--bg-hover`.

4. **Smart column sizing (`app.js` + `style.css`):**
   - New `colSizeClass(col)` function classifies columns by name into four sizing tiers:
     - `col-name-data` (name/entity_name/company/title): 180–280px
     - `col-url-data` (url/website/homepage/link/\*\_url): 160–300px, word-break
     - `col-desc-data` (description/summary/overview/bio): 200–360px
     - `col-default-data` (everything else): 120–240px
   - Classes applied to both `th` and `td` (including empty cells) during table rendering.
   - `table-layout: auto` allows the browser to distribute remaining space naturally.

5. **Visual polish (`style.css`):**
   - Increased cell padding from `0.55rem 0.8rem` to `0.6rem 1rem` for breathing room.
   - Header bottom border from `1px` to `2px` for stronger visual separation.
   - `.cell-value` max-width changed from `90%` to `calc(100% - 16px)`.
   - `.col-idx` and `.col-trust` min/max widths tightened.
   - Border moved from table to `.table-scroll` wrapper.
   - `border-collapse: separate` + `border-spacing: 0` (required for sticky header).

**Testing:**

- Visual inspection: table fills viewport width, columns size per type, header stays pinned on scroll, zebra striping visible, hover/modal/export all work.
- No backend changes — purely frontend CSS/JS.

**Files changed:** `static/style.css`, `static/app.js`.

**Tradeoffs:**

- Breakout container uses `calc(-50vw + 50%)` — assumes `.app` parent is centered.
- Column sizing is keyword-based; unusual column names fall to default tier.
- `max-height: 80vh` on `.table-scroll` means long tables scroll within the container rather than extending the page.

---

### Iteration 17 — Dynamic Schema-Aware Table Rendering

**Date:** 2026-04-04
**Author:** User (directed), Claude (implementation)
**Goal:** Make the table rendering handle variable-width schemas intelligently — different queries produce different column sets, and the naive approach broke visually for wide or text-heavy schemas.

**Issue observed:**
Dynamic schemas meant the same table renderer saw 4-column results (e.g. "top pizza places") and 10-column results (e.g. "AI startups in healthcare" with funding_stage, investors, headquarters, etc.). The Iteration 16 layout improvements (wider container, sticky header) helped but the column sizing was still flat — every column got the same `min-width`/`max-width` regardless of content type. Long text fields (description, notable_claim) pushed all other columns off-screen. URLs rendered as full-length strings eating available width. No way to prioritize important columns (name, rating) over auxiliary ones (summary, notes).

**Why dynamic schemas caused instability:**

1. **Uniform column sizing:** All data columns shared the same 4-class system (`col-name-data`, `col-url-data`, `col-desc-data`, `col-default-data`) with overlapping widths. A 10-column schema exceeded viewport width and every column got squashed equally.
2. **No column ordering:** Columns rendered in backend order, so `description` could appear before `name`.
3. **No text adaptation:** A 200-character description cell rendered the same way as a 5-character rating.
4. **No wide-schema awareness:** The CSS had no concept of "this schema has many columns, tighten up."

**What was built:**

1. **Column priority system (`app.js`):**
   - `COL_PRIORITY` map with 4 tiers: highest (name/entity_name/company/title), high (website/address/headquarters/rating/funding_stage), medium (price_range/phone/category/investors), low (description/summary/notable_claim/notes).
   - `colPriority(col)` resolves any column name to a tier (0–3) with heuristic fallbacks for `*_url` → high, `*description*` → low, everything else → medium.
   - `sortColumnsByPriority(cols)` reorders columns so name appears first, then high-priority structured fields, then medium, then long text last.
   - Replaces the old `colSizeClass()` 4-class system.

2. **Priority-based CSS classes (`style.css`):**
   - `col-pri-highest`: 180–300px, font-weight 500.
   - `col-pri-high`: 140–260px.
   - `col-pri-medium`: 110–200px.
   - `col-pri-low`: 100–280px (wide max for text, but clamped visually).
   - Wide-schema overrides (`.wide-schema .col-pri-*`): when table has 7+ columns, all tiers get tighter min/max widths.

3. **Safe text rendering (`app.js` + `style.css`):**
   - **URLs:** `truncateUrl()` extracts hostname + path and truncates to 40 chars with ellipsis. Full URL preserved in `td.title` and modal view. Rendered with monospace `.cell-url` class.
   - **Long text:** `.cell-text-clamp` uses `-webkit-line-clamp: 2` for a 2-line visual clamp. Full text accessible via click-to-modal.
   - **Short/categorical fields:** Render as-is with single-line ellipsis overflow.
   - **Empty cells:** Em dash placeholder (unchanged from Iteration 16).

4. **Wide schema handling (`app.js` + `style.css`):**
   - `renderTable()` adds `.wide-schema` class to the table when columns > 6.
   - CSS responds with tighter min/max widths across all priority tiers.
   - Horizontal scroll container (from Iteration 16) ensures all columns remain accessible.

5. **Compact/full view toggle (`app.js` + `index.html` + `style.css`):**
   - Toggle button ("⊟ Compact" / "⊞ Full view") added to summary strip actions, next to export buttons.
   - Compact mode: hides low-priority columns (`display: none`), tightens padding, reduces medium-column widths.
   - Button auto-hidden when schema has ≤ 4 columns (not useful for narrow schemas).
   - State managed via `_viewCompact` flag; toggled by `toggleViewMode()`.

**Files changed:** `static/app.js`, `static/style.css`, `templates/index.html`.

**Tradeoffs:**

- Column priority is keyword-based. Novel column names (e.g. `clinical_trial_phase`) fall to medium priority by default — not wrong, but not optimized.
- `sortColumnsByPriority()` reorders columns away from the backend's original order. If the backend order was intentional (e.g. user-facing schema design), the reorder may surprise.
- `-webkit-line-clamp` is not a formal CSS standard (though widely supported). Firefox has supported it since v68.
- Compact mode hides low-priority columns entirely. If a user needs to see `description` data without scrolling, they must toggle back to full view.
- The priority map must be maintained manually — new domain-specific columns won't automatically get the right tier.

---

### Iteration 18 — Reviewer-Facing Communication Polish

**Date:** 2026-04-04  
**Author:** User (directed), Claude (implementation)  
**Goal:** Make the UI intelligible to a reviewer within 30 seconds — what query was run, what the system did, why the table can be trusted, how to inspect evidence, and how much work was performed. No backend changes, no decorative additions.

**Issue observed:**
The UI from Iterations 12–17 was functionally complete but not oriented toward a reviewer scanning the page for the first time. Specific gaps:

1. The summary strip didn't echo the original query — a reviewer couldn't see what was searched.
2. The pipeline tracker showed stage progress but not what query was being processed.
3. Error and no-results banners were generic ("Error:" + message) with no query context.
4. The evidence modal's confidence display was plain text instead of a visual signal.
5. Panel body text was tight with no line-height breathing room.
6. No affordance hinting that cells are clickable for evidence.

**What was built:**

1. **Summary strip two-row layout (`index.html` + `style.css` + `app.js`):**
   - Top row: query echo (in smart quotes) + action buttons (compact toggle, JSON, CSV export).
   - Bottom row: entity type badge + metrics (rows, URLs, scraped, extracted, gap-fill, duration).
   - Query stored in `currentQuery` state variable and populated from form submit.
   - Metric labels shortened for scanability ("URLs" not "URLs inspected", "scraped" not "pages scraped").
   - Duration prefixed with ⏱ symbol.

2. **Pipeline tracker query echo (`index.html` + `style.css` + `app.js`):**
   - New `.pipeline-query` element shows the running query in quotes above the progress stages.
   - Populated on form submit; hidden when empty via `:empty` CSS rule.

3. **Error/no-results diagnostic context (`index.html` + `style.css` + `app.js`):**
   - Error banner restructured: title ("Pipeline Error"), detail message, hint on separate lines.
   - No-results banner: title, query echo line ("Query: '...'"), and diagnostic hint as separate elements.
   - No-results query populated from `currentQuery` state.

4. **Table evidence hint (`index.html` + `style.css`):**
   - "Click any cell to view its source URL, evidence snippet, and confidence score." hint above the table.
   - Styled small, dim, unobtrusive.

5. **Evidence modal confidence badge (`style.css`):**
   - Confidence display changed from plain text to a pill badge with color-coded background/border.
   - Green pill for ≥80%, yellow for ≥50%, red for <50%. Matches existing conf-dot color scheme.
   - Snippet blockquote given subtle background tint and rounded right corners.

6. **Panel readability (`style.css`):**
   - Panel body line-height increased to 1.6 for better readability.
   - Quality controls list items given more vertical and horizontal gap.
   - QC icons given fixed width for alignment.

**Files changed:** `templates/index.html`, `static/style.css`, `static/app.js`.

**Tradeoffs:**

- Query echo relies on `currentQuery` JS state — if the page is refreshed mid-results, the query echo is lost (acceptable: results are also lost on refresh).
- Confidence badge colors duplicate the badge CSS pattern used in trust badges. Could be consolidated into shared utility classes, but keeping them separate avoids coupling.
- The table hint is static text — it doesn't disappear after the user clicks a cell. Acceptable for a reviewer demo.

---

### Iteration 19 — Silent Planner Fallback Regression Fix

**Date:** 2026-04-04  
**Author:** User (directed), Claude (investigation + implementation)  
**Goal:** Find and fix the regression where broad queries like "AI startups in healthcare" return only 1 entity with `entity_type="entity"` and generic columns, instead of ~16+ specific entities.

**Issue observed:**
Queries that should discover many entities started returning exactly 1 row, generic entity_type `"entity"`, generic columns (`name`, `website`, `description`, `category`, `location`), and `rerank_scorer=null`. The planner was silently falling back to its hardcoded fallback plan on every query, making the extractor work with vague instructions and few search angles.

**Root cause:**
`PlannerOutput` in `app/models/schema.py` defined `search_angles: List[str]` as a **required field** (no default value). The planner LLM prompt asks for `entity_type`, `columns`, and `facets` — but **not** `search_angles`. The design is that `search_angles` is derived post-validation from `facets` (line: `result.search_angles = [f.query for f in result.facets][:5]`).

However, `plan_schema()` calls `chat_json_validated()`, which calls `PlannerOutput.model_validate(raw)` on the raw LLM response. Since the LLM never returns `search_angles`, Pydantic validation **always** raised `ValidationError` for the missing required field. The exception was caught by the `try/except` in `plan_schema()`, which then called `_fallback_plan()` — returning `entity_type="entity"` and 5 generic columns.

This means the planner has been falling back on **every single query** since `search_angles` was added to the model. The bug was silent because the fallback plan still produced valid (but degraded) results.

**Reproduction:**
Wrote a Python script confirming that `PlannerOutput.model_validate({"entity_type": "startup", "columns": [...], "facets": [...]})` raises `ValidationError: search_angles — Field required`. Adding `search_angles: List[str] = Field(default_factory=list)` makes validation succeed and the post-validation derivation from facets works correctly.

**Fix applied:**

| File                   | Change                                                                                |
| ---------------------- | ------------------------------------------------------------------------------------- |
| `app/models/schema.py` | `search_angles: List[str]` → `search_angles: List[str] = Field(default_factory=list)` |

One-line change. The field now defaults to `[]` if the LLM omits it (which it always does), and the post-validation derivation from facets populates it correctly.

**Tests added:**

| Test file                 | Test name                                             | Purpose                                                  |
| ------------------------- | ----------------------------------------------------- | -------------------------------------------------------- |
| `tests/test_planner.py`   | `test_planner_output_validates_without_search_angles` | Core regression test — validates without field           |
| `tests/test_planner.py`   | `test_planner_output_validates_with_search_angles`    | Backward compat — validates with field present           |
| `tests/test_extractor.py` | `test_extract_from_chunk_preserves_multiple_entities` | Verifies all entities from multi-entity response         |
| `tests/test_extractor.py` | `test_extract_from_pages_accumulates_across_pages`    | Verifies cross-page accumulation (extend, not overwrite) |

All 141 tests pass (137 original + 4 new).

**Live validation — "AI startups in healthcare":**

| Metric                 | Before (fallback)                                | After (fixed planner)                                                                    |
| ---------------------- | ------------------------------------------------ | ---------------------------------------------------------------------------------------- |
| `entity_type`          | `"entity"` (generic)                             | `"startup"` (domain-specific)                                                            |
| `columns`              | `name, website, description, category, location` | `name, founders, funding, headquarters, focus_area, website`                             |
| `facets`               | 3 generic paraphrase facets                      | 5 typed facets (entity_list, official_source, editorial_review, news_recent, comparison) |
| `entities_extracted`   | 1                                                | 93                                                                                       |
| `entities_after_merge` | 1                                                | 82                                                                                       |
| `pages_scraped`        | ~8                                               | 8                                                                                        |
| `urls_considered`      | ~13                                              | 12                                                                                       |

The fixed planner produces a domain-specific entity type, meaningful columns, and typed facets that generate better Brave search queries. The extractor, receiving instructions to find `startup` entities with specific columns, returns 93 entities from the same number of pages.

**Why this was hard to find:**
The fallback was designed for graceful degradation — the pipeline still produced results (just worse ones). No error was logged at WARNING or above; only a DEBUG-level catch in `plan_schema()` fired. The `search_angles` field was derived from facets _after_ validation, but adding it to the Pydantic model as required meant validation always failed _before_ the derivation could run.

**Tradeoffs:**

- The fix makes `search_angles` optional at the Pydantic layer. If future code depends on `search_angles` being populated at validation time (before post-processing), it would find an empty list. This is acceptable because the field is always populated immediately after validation.

---

### Iteration 20 - Fixing zero-entity completion regression on broad queries

Date: 2026-04-04
Goal: Find why broad discovery queries like "top pizza places in Brooklyn" completed with zero final rows, restore sane output quality, and add enough observability to prove the failure stage quickly next time.
Initial assumption: The most likely failure was somewhere between reranking and verification, but the first step was to prove whether rows were being extracted and filtered later or whether extraction itself had already collapsed.
Issue discovered: The collapse happened at extraction, not planning, reranking, merge, prune, verifier, serialization, or the UI. The planner produced `entity_type="pizza place"`, 6 relevant columns, and 5 typed facets. Brave search returned 18-21 deduped URLs, scrape kept 12-14 pages, reranker kept 10 pages, and then extraction returned 0 entities.
How it manifested: `scripts/smoke_test.py` against the live `/api/search` route returned `rows=0`, `pages_after_rerank=10`, `entities_extracted=0`, `entities_after_merge=0`, and the frontend showed the no-results banner only because `job.result.rows.length === 0`. A direct trace of the reranked pages showed that all 10 pages were plausible pizza-list or official pages, so the retrieval stack was healthy.
Why the old behavior was insufficient: `extractor.py` caught every LLM exception per page and converted it to `[]`. When the primary extractor provider fails systematically, the pipeline still "completes" and downstream stages see empty input, which makes the UI look like the query was bad instead of the extraction provider failing.
Root cause analysis: The split-provider routing from Iteration 15 sent all extraction calls to Groq. On the live pizza trace every Groq extraction call failed with HTTP 429 `rate_limit_exceeded` on `llama-3.3-70b-versatile`. A one-page sanity check using the same prompt/parser but forcing `provider="openai"` returned 15 pizza entities immediately, proving the prompt, parser, aggregation, and merge logic were not the problem. The first true collapse was therefore: Groq 429s -> extractor swallows exception -> empty draft list -> merge/prune/verifier never see rows. Backend JSON was genuinely empty, so the UI banner was correct and not itself the bug.
What was built or changed:

- `app/services/extractor.py`: added ordered extractor-provider fallback logic. Extraction now tries the configured primary provider first, then retries the same chunk on a configured secondary provider instead of returning `[]` immediately.
- `app/services/extractor.py`: added optional extraction stats counters (`llm_calls_attempted`, `provider_fallback_attempts`, `provider_fallback_successes`, `chunks_seen`, `pages_seen`, `pages_with_entities`).
- `app/api/routes_search.py`: added `pipeline_counts` tracking for search angles, facets, deduped URLs, scraped pages, reranked pages, extraction calls, merge/prune/verifier counts, and final rows. Also added a warning when extracted entities exist but final rows end up at zero.
- `app/models/schema.py`: extended `SearchMetadata` with `pipeline_counts`.
- `scripts/smoke_test.py`: now prints `pipeline_counts` for quick regression tracing.
- `tests/test_extractor.py`: added a regression test proving extractor fallback from Groq to OpenAI on provider failure.
  Why this fix was chosen: The prompt/parser path was already proven healthy when run through OpenAI, so the smallest safe fix was to repair provider-failure handling rather than redesign extraction or loosen quality filters. This keeps existing planner, reranker, merge, prune, and verifier behavior intact while preventing a transient or quota-based provider outage from being silently reinterpreted as "no entities found."
  What happened when it was run/tested:
- Live route, before fix, `"top pizza places in Brooklyn"`: `rows=0`, `pages_after_rerank=10`, `entities_extracted=0`.
- Live route, after fix, `"top pizza places in Brooklyn"`: `rows=30`, `entities_extracted=102`, `rows_after_merge=53`, `rows_after_verifier=30`, `final_rows=30`, `provider_fallback_attempts=15`, `provider_fallback_successes=13`.
- Live route, after fix, `"AI startups in healthcare"`: `rows=23`, `entities_extracted=24`, `rows_after_merge=23`, `final_rows=23`, `provider_fallback_attempts=14`, `provider_fallback_successes=7`.
- Full test suite: 142/142 passed.
  Failures / issues observed: Groq remained rate-limited during validation, so extraction latency increased substantially because many chunk calls had to retry on OpenAI. The fix restored correctness and non-zero outputs, but not Groq throughput.
  What was fixed immediately: The extractor now retries on a secondary provider when the primary provider fails, and the response metadata/logs now expose enough counts to pinpoint whether collapse happens at extraction, merge/prune, or verification.
  What was deferred: No startup-time provider health check was added, and Groq-specific error classification still lives only in logs rather than a dedicated structured error channel. The planner's generic fallback behavior also remains as a separate resilience tradeoff.
  Resulting improvement: Broad but valid discovery queries no longer collapse to zero rows just because the primary extractor provider is quota-limited. The pipeline now degrades into a slower secondary-provider extraction path instead of an empty-result path.
  Tradeoffs introduced: Extraction can take noticeably longer and consume secondary-provider budget when Groq is rate-limited. `pipeline_counts` slightly increases response metadata size, but it is compact and useful for debugging.
  Files/modules affected: `app/services/extractor.py`, `app/api/routes_search.py`, `app/models/schema.py`, `scripts/smoke_test.py`, `tests/test_extractor.py`, `docs/BUILD_JOURNAL.md`.
  Next step: Add a bounded provider-health or quota-awareness check so the pipeline can choose the fallback provider earlier, reducing the long extraction stall before fallback succeeds.

---

### Iteration 21 - Simplifying the pipeline into a normalized, discovery-first retrieval flow

Date: 2026-04-04
Goal: Move the project away from a brittle, over-filtered single-pass pipeline and toward a simpler, higher-recall, evaluation-friendly architecture without rebuilding the repo.
Initial assumption: The zero-entity regression from Iteration 20 was fixed, but broad-query quality was still too dependent on a single extraction pass, free-form planner behavior, and early row viability checks.
Issue discovered: The repo already had many good components, but they had accreted around a fragile shape: no query normalization, planner freedom that could drift toward generic schemas, extraction doing both discovery and fill in one step, no first-class official-site resolution, and prune/verifier logic that assumed rows should be complete much earlier than a discovery system can safely guarantee.
How it manifested: Valid broad queries could still be poisoned by light query noise, generic planning, or early low-information filtering. The earlier pipeline also had no explicit place for "candidate discovery first", which made recall heavily dependent on whichever pages and cells happened to survive the first extraction pass.
Why the old behavior was insufficient: The system had become sophisticated but less stable. It had many quality controls, yet too many of them sat before ranking and focused fill. That made the system more likely to collapse or under-return exactly on the broad discovery queries it was supposed to handle well.
Root cause analysis: The brittleness was structural, not one bug. Query normalization was missing entirely. Planner output was too unconstrained. Extraction carried both responsibilities (find entities and fill attributes), so any parser, prompt, or provider wobble hit recall directly. Canonical/official sources were only an implicit ranking signal instead of an explicit resolution step. `is_row_viable()`, `prune_rows()`, and `verify_rows()` were tuned more like a precision-first cleaner than a recall-first discovery pipeline.
What was built or changed:

- `app/services/query_normalizer.py`: added lightweight normalization with safe typo and location cleanup plus `original_query` / `normalized_query` metadata.
- `app/services/planner.py`: replaced the free-form schema planner with query-family classification and strong schema templates for `local_business`, `startup_company`, `software_tool`, `product_category`, `organization`, and `fallback_generic`.
- `app/services/extractor.py`: split extraction into `discovery` and `fill` modes so list pages preserve multiple candidate entities before later enrichment.
- `app/services/official_site.py`: added a practical canonical-site resolver that attaches likely official domains/websites when pages strongly match a candidate.
- `app/api/routes_search.py`: reordered the pipeline around the new flow: normalize -> constrained plan -> search/scrape/rerank -> discovery extraction -> merge -> official-site resolution -> rank -> focused fill -> late verification -> final prune/rank.
- `app/services/gap_fill.py`: now prefers canonical/about/contact pages for attribute fill before falling back to fresh search results.
- `app/services/ranker.py` and `app/services/verifier.py`: softened early hard rejection so plausible rows survive into ranking and late verification; only obvious junk is dropped early.
- `scripts/eval.py`: expanded run metrics with actionable-field rate, official-site rate, and passthrough of normalized query / query family / pipeline counts.
- Tests: added regression coverage for query normalization, constrained planning, official-site resolution, and discovery-plan behavior. Total suite: 150 passing tests.
- Docs/UI: README, BUILD_JOURNAL, and reviewer-facing UI copy updated to tell the simpler normalized-query -> discovery-first -> official-source -> late-filtering story.
Why this change was chosen: It is the smallest safe evolution that preserves the repo's best engineering pieces: async jobs, provenance, reranking, merge, exports, evaluation harness, tests, and the UI. Instead of adding more orchestration, it reuses the existing modules in a clearer order with fewer hard failure points.
What happened when it was run/tested:

- Targeted tests for planner/normalizer/official-site/extractor/ranker/verifier/gap-fill/eval: 54 passed.
- Full test suite: 150/150 passed.
- Live smoke test, `"top pizza places in Brooklyn"`: 31 final rows, `entities_before_merge=115`, `rows_after_merge=56`, `rows_after_verifier=31`, `final_rows=31`.
- Live smoke test, `"AI startups in healthcare"`: 31 final rows, `entities_before_merge=157`, `rows_after_merge=130`, `official_sites_resolved=2`, `rows_after_verifier=31`, `final_rows=31`.
- Live traces showed the first healthy collapse point moved much later in the pipeline: planning/search/discovery/merge all stayed populated, and the final cut happened in late verification instead of at extraction or early prune.
Failures / issues observed: Groq remained rate-limited during live validation, so many discovery/fill calls fell back to OpenAI and pushed end-to-end latency above 100 seconds. Official-site resolution was intentionally conservative and only resolved a small subset of rows in the live runs.
What was fixed immediately: Broad-query recall is now protected by normalization, constrained planning, discovery-first extraction, official-site-aware fill, and softer late filtering.
What was deferred: Official-site resolution could be broadened further, startup-time provider health checks are still absent, and there is still no labeled precision/recall benchmark.
Resulting improvement: The pipeline is now easier to reason about and materially more stable on broad discovery queries. Good rows survive long enough to be ranked and enriched instead of being filtered out before the system has assembled enough evidence.
Tradeoffs introduced: More candidates survive longer, which increases downstream work and can raise latency/cost. Official-site resolution is heuristic rather than guaranteed. The planner is intentionally less free-form, so exotic queries may route through `fallback_generic` more often than before.
Files/modules affected: `app/models/schema.py`, `app/core/config.py`, `app/api/routes_search.py`, `app/services/query_normalizer.py`, `app/services/planner.py`, `app/services/extractor.py`, `app/services/official_site.py`, `app/services/gap_fill.py`, `app/services/ranker.py`, `app/services/verifier.py`, `scripts/eval.py`, `static/app.js`, `templates/index.html`, `README.md`, `docs/BUILD_JOURNAL.md`, `tests/test_planner.py`, `tests/test_query_normalizer.py`, `tests/test_official_site.py`, `tests/test_extractor.py`, `tests/test_provider_routing.py`, `tests/test_eval_metrics.py`.
Next step: Improve canonical-domain recall and add a small benchmark of labeled cells or rows so late-filtering changes can be judged on precision as well as recall.

---

### Iteration 22 - Hardening the reviewer demo path, pseudo-entity filter, and website semantics

Date: 2026-04-04
Goal: Improve runtime stability and reviewer trust without changing the overall architecture by fixing three concrete demo issues: unstable default extractor routing, pseudo-entity rows, and semantically wrong website values.
Initial assumption: Iteration 21 restored broad-query recall, but the demo still looked brittle because Groq was the default extractor path, category/list labels could survive as rows, and `website` sometimes reflected article or directory URLs instead of official homepages.
Issue discovered: Three reviewer-facing problems were still visible in real runs. First, Groq remained the configured default extractor even though repeated 429s meant OpenAI fallback was doing much of the real work anyway. Second, list-page labels like `"AI Copilots & Agents for Psychiatry"` could survive to the final table as if they were real startups. Third, website validation and canonical resolution were permissive enough that editorial articles or directory roots could populate the final `website` cell.
How it manifested: Startup/runtime logs still showed `extractor=groq/...` until the env override was corrected. On the healthcare smoke query, a pseudo row with entity id `ai-copilots-agents-for-psychiatry` appeared in one run because the listing root looked superficially like a plausible website. Separate audit traces also showed article/blog URLs being accepted as website candidates, which made final rows look semantically wrong even when the rest of the evidence was good.
Why the old behavior was insufficient: Reviewer-facing demos need stable runtime defaults and trustworthy semantics. Recovering from Groq throttling via fallback was better than returning zero rows, but it still added avoidable latency and made the runtime story look fragile. A category label row undermines confidence in the discovery stage. An editorial article in the `website` column makes the table feel careless even if the entity itself is real.
Root cause analysis: The primary issue was not architecture but small policy gaps. Config defaults and `.env` still preferred Groq for extraction even after repeated live throttling proved OpenAI was the more stable default for the demo. Pseudo-entity handling relied too much on generic viability signals and not enough on combined name/source/website heuristics. Website normalization focused on URL shape more than website meaning, and `official_site.py` could accidentally bootstrap a directory or article URL into `canonical_domain` by trusting the row's existing `website`.
What was built or changed:

- `app/core/config.py`, `.env.example`, and `.env`: switched the default demo extractor path to OpenAI while leaving Groq available as an optional alternate/fallback provider.
- `app/services/field_validator.py`: strengthened `website` semantics so editorial/article/directory/marketplace URLs are rejected as final website values, obvious company-info subpages are canonicalized to the homepage root, and empty is preferred over a semantically wrong website.
- `app/services/official_site.py`: sanitizes existing website cells before resolution, ignores listing pages as canonical candidates, and replaces semantically worse website values when a likely homepage is found.
- `app/services/verifier.py`: added a targeted late pseudo-entity filter combining suspicious category-style names, suspicious source/title/path hints, weak actionable evidence, and same-domain listing websites.
- `app/services/extractor.py`: now passes source URL/title context into website validation so article/listing rejection happens at the extraction boundary too.
- Tests: updated provider-routing defaults and added regression coverage for pseudo-entity filtering, editorial/directory website rejection, homepage canonicalization, and non-bootstrap official-site behavior.
- Docs: README now reflects OpenAI as the default demo extractor, Groq as optional, late pseudo-entity filtering, and homepage-first website semantics.
Why this change was chosen: These were the smallest safe fixes that directly addressed reviewer-facing weaknesses without redesigning the pipeline. The planner, retrieval flow, discovery/fill split, provenance model, ranking, verifier structure, exports, and UI all stay intact; only the default runtime path and the two weak semantic filters were tightened.
What happened when it was run/tested:

- Targeted regression tests for provider routing, website semantics, official-site resolution, and pseudo-entity filtering: 38/38 passed.
- Full test suite: 157/157 passed.
- Startup/provider sanity check after updating `.env`: logs reflected `planner=openai/gpt-4o-mini extractor=openai/gpt-4o-mini`.
- Live bounded smoke validation on `"AI startups in healthcare"` after the final verifier/website changes: the final saved result no longer contained the pseudo entity id `ai-copilots-agents-for-psychiatry`; top rows remained real startups such as `insilico-medicine`, `freenome`, `tempus`, `pathai`, and `verily`.
Failures / issues observed: Optional Groq fallback can still appear in logs when OpenAI times out on individual calls and Groq is configured as the secondary provider; if Groq is throttled at the same time, latency still rises. Official-site resolution remains conservative and will sometimes leave `website` blank rather than guessing. The pseudo-entity filter is heuristic and may still miss subtler category artifacts in other domains.
What was fixed immediately: Reviewer runs now default to OpenAI extraction instead of starting with a provider that is known to 429 on the demo workload. Obvious category/list labels are removed from final rows. Final `website` cells now prefer canonical homepages and reject article/directory values unless there is no better official candidate.
What was deferred: No startup-time provider health check was added, and there is still no broader canonical-domain enrichment pass beyond the existing heuristic resolver. The pseudo-entity rules remain deliberately targeted rather than becoming a large classifier.
Resulting improvement: The demo runtime path is more stable, final rows are more trustworthy, and the `website` column now matches normal reviewer expectations. These changes improve semantic correctness and reduce the kinds of artifacts that make a near-finished project look unreliable.
Tradeoffs introduced: OpenAI-first extraction is generally slower and can cost more than an ideal healthy Groq run. Website cells may be empty more often because the system now refuses semantically wrong URLs. The pseudo-entity rule is intentionally conservative, so some borderline category-like labels may still be downranked rather than always deleted.
Files/modules affected: `app/core/config.py`, `.env`, `.env.example`, `app/services/llm.py`, `app/services/extractor.py`, `app/services/field_validator.py`, `app/services/source_quality.py`, `app/services/official_site.py`, `app/services/verifier.py`, `README.md`, `docs/BUILD_JOURNAL.md`, `tests/test_provider_routing.py`, `tests/test_extractor.py`, `tests/test_field_validator.py`, `tests/test_official_site.py`, `tests/test_verifier.py`.
Next step: Improve canonical-domain recall without weakening the stricter homepage semantics, and consider lightweight startup/provider health telemetry so the demo can surface degraded secondary-provider behavior more explicitly.

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
Built a repeatable CLI evaluation harness (`scripts/eval.py`) with 10 queries across food/tech/travel categories. Metrics now cover rows returned, fill rate, actionable rate, official-site rate, multi-source rate, confidence, source diversity, and duration. JSON + CSV output for comparison across runs, tag-based ablation support.

**Phase 7 (Iteration 12) — UI for Reviewer Impact:**  
Rewrote the frontend to communicate the pipeline's process and trust signals. Added phase tracker, retrieval plan panel, quality controls panel, run stats panel, row-level trust badges, enhanced evidence modal, and empty/error states. Same tech stack (Jinja2 + vanilla JS + CSS), no framework.

**Phase 8 (Iteration 13) — LLM Provider Migration:**  
Switched primary LLM from OpenAI to Groq. Added provider-agnostic config properties, markdown-fence JSON extraction fallback, startup provider logging. OpenAI remains as transparent fallback when Groq API key is not set.

**Phase 9 (Iteration 14) — Model Decommission Fix:**  
Groq decommissioned `llama-3.1-70b-versatile` (2025-01-24). Every LLM call returned HTTP 400 `model_decommissioned`. Both planner and extractor silently swallowed errors (by design for transient failures), masking a permanent configuration error. Updated to `llama-3.3-70b-versatile`. Added pipeline safeguard that logs ERROR when extraction produces 0 entities from ≥3 pages.

**Phase 10 (Iteration 15) — Split-Provider Routing:**  
Split LLM usage: planner routes to OpenAI (gpt-4o-mini) for structural reliability, extractor routes to Groq (llama-3.3-70b-versatile) for faster inference. Dual-client pool in `llm.py`, env-based routing via `PLANNER_PROVIDER` and `EXTRACTOR_PROVIDER`. Outcome: best-of-both-providers — reliable schema planning + fast bulk extraction.

**Phase 11 (Iterations 16–18) — Reviewer-Ready UI:**  
Widened results container to 95vw with sticky header and zebra rows (Iteration 16). Added priority-based column sizing, compact/full view toggle, URL truncation, and text clamping (Iteration 17). Polished reviewer-facing communication: query echo in summary strip and pipeline tracker, two-row summary layout, confidence badge pills in modal, diagnostic error/no-results banners, table evidence hint, panel readability improvements (Iteration 18). Outcome: a reviewer can orient within 30 seconds on what was queried, what work was performed, and how to drill into evidence.

**Phase 12 (Iteration 19) — Silent Planner Regression Fix:**  
`PlannerOutput.search_angles` was a required Pydantic field that the LLM never returned, causing validation to fail silently on every query and forcing the planner into its generic fallback plan. One-line fix (default_factory=list) restored domain-specific planning. Live validation: "AI startups in healthcare" went from 1 generic entity to 93 extracted / 82 merged entities with domain-specific entity type, columns, and typed retrieval facets. Added 4 regression tests (141 total).

**Phase 13 (Iteration 20) — Extraction Provider Fallback + Stage Counts:**  
Broad discovery queries regressed to zero rows because Groq extraction 429s were being silently converted into empty entity lists. Added extractor-provider fallback (Groq -> OpenAI when configured) and `pipeline_counts` metadata covering search, extraction, merge, prune, verifier, and final-row counts. Live validation: "top pizza places in Brooklyn" went from 0 rows / 0 extracted to 30 final rows / 102 extracted, and "AI startups in healthcare" returned 23 final rows.

**Phase 14 (Iteration 21) — Simplification for Recall and Stability:**  
Added lightweight query normalization, constrained query-family planning, discovery-first extraction, canonical/official-site resolution, official-site-aware focused fill, softer late filtering, and expanded evaluation metrics. Live validation: "top pizza places in Brooklyn" returned 31 final rows from 115 discovered candidates / 56 merged rows, and "AI startups in healthcare" returned 31 final rows from 157 discovered candidates / 130 merged rows. Tests increased to 150 passing.

**Phase 15 (Iteration 22) — Demo Hardening and Semantic Cleanup:**  
Reverted the default demo extractor path to OpenAI, added a targeted late pseudo-entity filter for list/category artifacts, and tightened website semantics so final `website` values prefer canonical homepages over article or directory URLs. Validation: 157/157 tests passing, startup logs confirmed `planner=openai ... extractor=openai ...`, and the final saved healthcare smoke-test result no longer contained the pseudo row `ai-copilots-agents-for-psychiatry`.

**Remaining gaps:**  
No ground-truth labels — metrics are proxy signals, not precision/recall. The OpenAI-first demo path is more stable, but optional secondary-provider fallback can still trade latency/cost for correctness preservation when the primary provider is degraded (see Known Limitation #6 and #9).

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

### Decision: Constrained query families over fully free-form planning

**Context:** Free-form planner output drifted toward generic schemas (`entity`, `description`, `location`) and weakened downstream recall.  
**Why chosen:** A small query-family classifier plus schema templates keeps the pipeline predictable while still allowing typed facet generation and light domain adaptation.  
**Tradeoffs:** Some unusual queries now route through `fallback_generic` instead of a bespoke one-off schema. This is acceptable because it favors stability over planner creativity.  
**Status:** Kept and strengthened in Iteration 21.

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

### Decision: Rank first, kill late

**Context:** Early prune/verifier checks were removing plausible discovery candidates before official-site resolution and focused fill had a chance to strengthen them.  
**Why chosen:** Discovery systems degrade more gracefully when ranking absorbs uncertainty early and hard rejection happens later, after evidence has had a chance to accumulate.  
**Tradeoffs:** More candidates survive longer, which increases downstream work and can surface a little more noise before final verification.  
**Status:** Strengthened in Iteration 21.

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

### Issue: Every query returns zero entities after Groq migration

**Detected in:** Iteration 14, user report  
**Symptoms:** All queries return "No entities found." Pipeline completes in ~1s. Planner produces fallback plan (generic `entity` type). Extraction returns 0 entities from 8 scraped pages.  
**Root cause:** `llama-3.1-70b-versatile` decommissioned by Groq (2025-01-24). Every LLM call returns HTTP 400 `model_decommissioned`. Planner silently falls back to hardcoded plan. Extractor silently returns `[]` per page. Pipeline completes "successfully" with 0 rows.  
**Fix:** Updated model to `llama-3.3-70b-versatile` in config.py, .env, .env.example. Added 0-entity safeguard log in routes_search.py.  
**Status:** Resolved.

---

### Issue: Planner always falls back to generic plan — silent `search_angles` validation failure

**Detected in:** Iteration 19, user report (entity_type="entity", generic columns, 1 entity returned)  
**Symptoms:** Every query returns entity_type `"entity"`, generic columns, weak facets, and few extracted entities. Planner appears to work but always uses the hardcoded fallback plan. No WARNING or ERROR log emitted.  
**Root cause:** `PlannerOutput.search_angles` was a required Pydantic field (`List[str]` with no default). The planner prompt does not ask the LLM to return `search_angles` — the field is derived from facets post-validation. But `model_validate()` always raised `ValidationError` for the missing field before derivation could run. The exception was caught silently by `plan_schema()`'s `try/except`, triggering `_fallback_plan()`.  
**Fix:** `search_angles: List[str] = Field(default_factory=list)` — one-line default addition. Post-validation derivation then populates the field from facets.  
**Impact:** Affected every query since the `search_angles` field was added. The planner was never returning its actual LLM-inferred plan.  
**Status:** Resolved.

---

### Issue: Broad queries complete with zero rows when extractor provider is rate-limited

**Detected in:** Iteration 20, user report (`"top pizza places in Brooklyn"` returns "No entities found")  
**Symptoms:** Broad queries reached `done` status with `rows=[]`, `entities_extracted=0`, and the frontend no-results banner, even though the planner output, scraped pages, and reranked pages all looked healthy.  
**Root cause:** `extractor.py` routed every chunk to Groq and caught any provider failure by returning `[]`. Under sustained Groq 429 `rate_limit_exceeded`, every extraction call failed, so merge/prune/verifier received zero drafts and the backend returned an empty result set.  
**Fix:** Added ordered extractor-provider fallback (primary extractor provider first, then a configured secondary provider), plus `pipeline_counts` metadata/logging so the first collapsing stage is visible in smoke tests and final responses.  
**Status:** Resolved.

---

### Issue: Broad-query recall was still brittle even after the zero-row bug was fixed

**Detected in:** Iteration 21, architecture audit + live trace review  
**Symptoms:** The pipeline no longer returned zero rows under provider failure, but broad discovery quality still depended on a free-form planner, a single extraction pass doing both discovery and fill, and early viability checks that could suppress plausible candidates before ranking.  
**Root cause:** The system had accumulated quality controls around a precision-first shape. Query normalization was absent, planner schemas were too unconstrained, canonical/official sites were not a first-class step, and prune/verifier logic sat too early in the flow.  
**Fix:** Added lightweight query normalization, constrained query-family planning, discovery-first extraction mode, official-site resolution, official-site-aware fill, softer early viability checks, and later verification/pruning.  
**Status:** Resolved for the current architecture direction; remaining gaps are now mainly heuristic quality/latency issues rather than structural recall collapse.

---

### Issue: Reviewer demo still defaulted to Groq despite repeated throttling

**Detected in:** Iteration 22, startup log audit + live smoke validation  
**Symptoms:** Runtime still started with `extractor=groq/...`, Groq 429s appeared early in extraction, and OpenAI fallback was rescuing many pages anyway.  
**Root cause:** Config defaults and local demo env still pointed `EXTRACTOR_PROVIDER` at Groq, even after prior traces showed OpenAI was the more stable reviewer-facing default.  
**Fix:** Switched default extractor routing to OpenAI in config and env docs, while keeping Groq as an optional alternate/fallback provider.  
**Status:** Resolved.

---

### Issue: Category/list labels could survive as final entity rows

**Detected in:** Iteration 22, `"AI startups in healthcare"` live trace  
**Symptoms:** A row like `"AI Copilots & Agents for Psychiatry"` appeared in final output even though it was a category/list label, not a company.  
**Root cause:** Existing late filtering focused on marketplace/thin-evidence rows and did not combine suspicious name patterns with source/listing heuristics strongly enough.  
**Fix:** Added a targeted pseudo-entity rule in `verifier.py` that combines category-style names, suspicious source/title/path hints, weak actionable evidence, and non-entity website behavior.  
**Status:** Resolved for the observed patterns; remains heuristic by design.

---

### Issue: `website` field could contain article or directory URLs

**Detected in:** Iteration 22, website audit during smoke/debug review  
**Symptoms:** Final `website` cells could point to editorial articles, blog posts, or listing roots instead of the entity's homepage.  
**Root cause:** `field_validator.py` validated URL structure but not website meaning, and `official_site.py` could trust an existing website cell too early when deriving canonical domains.  
**Fix:** Strengthened website normalization/acceptance rules, passed source URL/title context into website validation, sanitized existing website cells before canonical resolution, and prevented directory/article URLs from bootstrapping `canonical_domain`.  
**Status:** Resolved for the targeted failure modes.

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

### Improvement: Broad-query resilience under provider failure

**Before:** A quota-limited extractor provider produced the same user-visible result as a genuinely empty query: 0 extracted entities, 0 merged rows, and "No entities found." No per-stage counts in the final response made the collapse point hard to prove.  
**After:** Extraction retries on a configured secondary provider, and `pipeline_counts` shows search, extraction, merge, prune, verifier, and final-row counts in the response metadata. Broad queries now degrade to slower extraction rather than empty output.  
**What caused improvement:** Iteration 20 provider fallback + stage-count observability.

---

### Improvement: Broad-query recall stability

**Before:** Broad discovery queries depended too heavily on one extraction pass and on early row viability checks. Small planner drift, query typos, or thin first-pass evidence could remove plausible rows before canonical sources and focused fill had a chance to help.  
**After:** Query normalization feeds a constrained planner, discovery mode preserves candidate lists first, official-site resolution strengthens later fills, and late verification trims ranked rows instead of choking recall at the front of the pipeline.  
**What caused improvement:** Iteration 21 normalization + constrained planning + discovery-first extraction + late-filtering refactor.

---

### Improvement: Reviewer-facing runtime stability

**Before:** The demo still started extraction on Groq by default, so repeated 429 recovery made runs look fragile and slower than necessary even when OpenAI fallback preserved correctness.  
**After:** The default demo path now uses OpenAI for both planning and extraction, with Groq left as an optional alternate/fallback path instead of the primary runtime.  
**What caused improvement:** Iteration 22 provider default change.

---

### Improvement: Final-row semantic correctness

**Before:** Category/list labels could survive as rows, and `website` could point to editorial articles or directory roots rather than an entity homepage.  
**After:** Late pseudo-entity filtering removes obvious category artifacts, and website validation/canonical resolution now prefer official homepages while rejecting article/directory values.  
**What caused improvement:** Iteration 22 pseudo-entity + website semantics fixes.

---

## Current Architecture

### Pipeline stages (in order)

```
POST /api/search
  │
  ▼
1. query_normalizer.py — safe query cleanup; preserve original + normalized query
2. planner.py          — query-family classification + schema template + typed retrieval facets
3. brave_search.py     — parallel Brave API calls (facet queries × 5 results), URL dedup
4. scraper.py          — async fetch (semaphore=5), trafilatura→BS4, SQLite cache (24h TTL)
5. reranker.py         — cross-encoder rerank (ms-marco-MiniLM-L-6-v2) or Jaccard fallback
6. extractor.py        — discovery-mode extraction per page/chunk; OpenAI-first demo path with optional provider fallback
7. merger.py           — rapidfuzz + domain dedup, best-confidence cell wins, lookup refresh
8. official_site.py    — canonical/official-site resolution from explicit fields and page signals
9. rank_rows()         — completeness(0.25) + confidence(0.20) + source_quality(0.32)
                         + source_support(0.08) + actionable(0.07) + source_diversity(0.08)
10. gap_fill.py        — fill-mode extraction for top sparse rows; prefer official/about/contact pages
11. cell_verifier.py   — per-cell entity-alignment check, 0.6× confidence penalty
12. verify_rows()      — late filter for obvious junk, weak marketplace-only rows, safe fallback
13. prune + re-rank    — light final cleanup with final ordering
14. complete_job()     — write result JSON + `pipeline_counts` metadata to SQLite
```

### Module status

| Module                 | Author        | Status     | Notes                                                                                             |
| ---------------------- | ------------- | ---------- | ------------------------------------------------------------------------------------------------- |
| `query_normalizer.py`  | Claude        | Stable     | Bounded cleanup only; improves retrieval without rewriting user intent                             |
| `planner.py`           | Claude        | Stable     | Constrained families + schema templates; avoids generic planner drift                              |
| `brave_search.py`      | Claude        | Stable     | 15–25 unique URLs typical                                                                         |
| `scraper.py`           | Claude        | Stable     | trafilatura handles most pages; JS-rendered pages skipped                                         |
| `extractor.py`         | Claude + user | Stable     | Discovery + fill modes, chunk semaphore, OpenAI-first demo path, optional provider fallback       |
| `merger.py`            | Claude + user | Stable     | Lookup refresh fix applied                                                                        |
| `official_site.py`     | Claude        | Functional | Conservative heuristic resolver; now sanitizes bad website cells before attaching canonical sites  |
| `ranker.py`            | Claude + user | Stable     | 6-component score; early prune softened in Iteration 21                                           |
| `reranker.py`          | Claude        | Stable     | Cross-encoder ms-marco-MiniLM-L-6-v2 + Jaccard fallback                                           |
| `cell_verifier.py`     | Claude        | Stable     | Three-rule alignment, 0.6× penalty                                                                |
| `field_validator.py`   | Claude        | Stable     | URL/phone/rating normalization; website semantics reject article/directory URLs                    |
| `gap_fill.py`          | Claude + user | Functional | Focused fill now prefers official/about/contact pages first                                       |
| `source_quality.py`    | User          | Functional | Domain lists hand-curated; food/startup-biased                                                    |
| `verifier.py`          | User          | Functional | Late filter; now also removes obvious pseudo-entity/category-label rows                           |
| `exporter.py`          | Claude        | Stable     | JSON + CSV with per-column provenance                                                             |
| `llm.py`               | Claude + user | Stable     | Dual-provider pool (OpenAI demo default; Groq optional alternate/fallback), markdown fence extraction, 60s timeout |

### What is stable

- Full pipeline from normalized query to ranked table
- Per-cell provenance (source_url, title, snippet, confidence)
- Discovery-first extraction for broad queries
- Per-stage `pipeline_counts` metadata for live debugging
- SQLite caching of scraped pages (24h TTL in `/tmp`)
- JSON/CSV export with provenance columns
- Dark-theme UI with phase tracker, trust badges, quality/stats panels, evidence modal, and full-width sticky-header results table

### What is still weak

- **Official-site resolution recall:** `official_site.py` is intentionally conservative and still misses many rows that really do have a canonical website
- **Pseudo-entity heuristics:** obvious category/list artifacts are now filtered late, but subtler taxonomy phrases may still slip through or be merely downranked
- **Cell-level entity consistency:** mostly mitigated by `cell_verifier.py`, but short entity names can still false-match on fuzzy `partial_ratio`
- **JS-rendered pages:** SPAs return empty content — skipped by `_MIN_TEXT_LENGTH` threshold
- **Source domain lists:** `source_quality.py` is calibrated for food/startup queries; other domains get neutral `unknown` score
- ~~**No URL validation:**~~ **Resolved** in Iteration 10 — `field_validator.py` rejects bare words, normalizes scheme/host
- ~~**Schema planner edge cases:**~~ **Resolved** in Iteration 19 — `PlannerOutput.search_angles` required-field bug caused permanent fallback on all queries. Fixed by adding `default_factory=list`.
- ~~**No source diversity constraint:**~~ **Resolved** in Iteration 10 — `_source_diversity()` in `ranker.py` with 0.08 weight
- ~~**No automated evaluation:**~~ **Resolved** in Iteration 11 / expanded in Iteration 21 — `scripts/eval.py` provides a repeatable CLI harness with 10 queries and expanded recall-oriented proxy metrics

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

6. **Latency:** 25–60s is still typical on the default OpenAI demo path. If an optional secondary provider is enabled and fallback is exercised, discovery and fill can still take longer.

7. **Single-round focused fill:** Entities still sparse after one enrichment round remain sparse. A second round would improve completeness at doubled cost.

8. **No query caching:** Re-running the same query re-executes the full pipeline. Only page scraping is cached.

9. **Optional Groq rate limits:** Groq is no longer the default demo extractor, but if you enable it as an alternate/fallback provider its free tier still has per-minute request and token caps. Sustained Groq throttling now shows up as higher latency and higher recovery cost instead of empty output.

10. **Llama JSON reliability:** Llama 3.3 models occasionally wrap valid JSON in markdown code fences even with `json_object` response format. The `_extract_json()` fallback handles this, but very complex schemas may still produce parse failures more often than GPT-4o-mini.

11. **Frontend source classification drift:** The UI trust badges classify source URLs using domain sets that mirror `source_quality.py`. If the backend domain lists are updated without updating the JS, the UI badges may disagree with backend scoring. This is informational only — the backend scoring is what matters for ranking.

12. **Model deprecation risk:** Groq (and other providers) can decommission models without notice. The system detects the downstream symptom (0 entities from many pages) via the safeguard log, but does not detect the upstream cause (`model_decommissioned` error code) specifically. A future improvement could validate the model on startup or detect the specific error code in `chat_json()` and raise a non-swallowable error.

13. **Silent planner fallback:** `plan_schema()` catches all exceptions from LLM schema inference and falls back to a generic plan. This is intentional for resilience, but it masks permanent configuration errors (e.g., Iteration 19's required-field bug ran silently for an unknown number of queries). The fallback only logs at WARNING and does not currently surface structured planner-fallback metadata to the client.

14. **Official-site heuristics are conservative:** Canonical-domain resolution only fires when the page/domain signals look strong. This avoids attaching the wrong website, but it also means many valid rows still rely on editorial/directory evidence instead of an explicit official-site match.

15. **Homepage semantics prefer blank over wrong:** The stricter `website` rules now reject article/blog/directory URLs as final websites. This improves trust, but it also means some real entities will show no website until a cleaner canonical homepage can be resolved.
