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
- **Current state:** full 8-stage pipeline, dark-theme UI, per-cell evidence modal, JSON/CSV export

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

| # | Problem | Symptom |
|---|---|---|
| 1 | `--reload` restarts on DB writes | Job stuck at `scraping` forever after every scrape |
| 2 | No LLM timeout | Extraction phase hangs indefinitely, no error |
| 3 | No extraction semaphore | 26 simultaneous LLM calls saturate rate limit, most stall |
| 4 | Polling stops on any error | `ERR_NETWORK_CHANGED` shows "Polling failed", stops forever |
| 5 | Stale `running` jobs on restart | Old job IDs return `status=running` on fresh server start |
| 6 | Silent extraction phase | Logs stop after scraping with no indication of progress |

**Fixes applied:**

| Problem | Fix | File |
|---|---|---|
| --reload restart | Moved DB to `/tmp/agentic_search.db`; `--reload-dir app` | `config.py` |
| LLM hangs | `AsyncOpenAI(timeout=60.0, max_retries=0)` | `llm.py` |
| Too many LLM calls | `asyncio.Semaphore(3)` at page level | `extractor.py` |
| Polling dies on blip | `_pollErrors` counter, retry up to 8 before giving up | `app.js` |
| Stale jobs | `UPDATE query_jobs SET status='failed' WHERE status IN ('running','pending')` on startup | `db.py` |
| Silent extraction | `log.debug` → `log.info` per page | `extractor.py` |

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

| Issue | Example | Root cause |
|---|---|---|
| Cell-level entity contamination | `Espresso Pizzeria` received address + phone belonging to `F&F Pizzeria` | Gap-fill still not filtering tightly enough at cell level |
| Mixed entity rows | `Paulie Gee's` mixes the restaurant and the slice shop | Same page mentions both; name similarity threshold too loose |
| Website not normalized | `Roberta's` website value is `robertaspizza` not a valid URL | No URL validation on extracted website values |
| Single-source domination | `foodieflashpacker.com` contributes most contact/rating fields across many rows | No source diversity constraint in merger/ranker |
| Weak generic fields still present | `cuisine_type: Pizza` appearing without useful context | Correctly pruned by prune_rows but still extracted |

**Conclusion:**
Source quality scoring and verifier worked at the row level — low-trust row selection improved. But cell-level entity consistency is still imperfect. One source can still dominate a large share of a row's evidence without penalty.

**Recommended next improvements (not yet implemented):**
1. Stricter cell-level entity verification — reject cells whose evidence does not clearly name the target entity
2. Website URL validation — require `http(s)://` prefix, reject fragments
3. Source diversity constraint — penalize rows where >60% of cells come from a single domain
4. Domain-aware column weighting — restaurants prioritize address/phone/rating; startups prioritize funding/investors/website

---

## Overall Progress Summary

**Phase 1 (Iterations 1–2) — Reliability:**  
Built and stabilised the pipeline. Fixed crashes from the reload watcher, LLM hangs, extraction semaphore, stale job state, and polling fragility. Outcome: pipeline reliably reaches completion on a clean start.

**Phase 2 (Iterations 3–5) — Extraction Precision:**  
Fixed the chunker OOM crash, tightened the extraction semaphore to the chunk level, refocused gap-fill to target-entity queries, backfilled missing `name` cells, refreshed merger lookup after absorptions, and introduced row pruning with actionable-field scoring. Outcome: more rows keep usable name + actionable attributes; wrong-entity enrichment substantially reduced.

**Phase 3 (Iterations 6–7) — Source Trust:**  
Added `source_quality.py` (confidence-weighted source scoring) and `verifier.py` (row-level filter for marketplace-only and low-evidence rows). Wired into pipeline as a second prune/rank pass after gap-fill. Outcome: marketplace-only rows removed for strict queries; top results now backed by editorial or official sources more often.

**Remaining gap:**  
Source quality works at the row level. Cell-level entity consistency — wrong entity evidence absorbed at the cell level — and single-source domination are still open. These require cell-level verification and source diversity constraints (see Iteration 7 recommendations).

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
1. planner.py         — LLM infers entity_type, columns (5–8), search_angles (3–5)
2. brave_search.py    — parallel Brave API calls (5 angles × 5 results), URL dedup
3. scraper.py         — async fetch (semaphore=5), trafilatura→BS4, SQLite cache (24h TTL)
4. extractor.py       — LLM extraction per page, global semaphore=3 across all chunks
                        30s timeout per call, 1 attempt (fail-fast per page)
5. merger.py          — rapidfuzz + domain dedup, best-confidence cell wins, lookup refresh
6. prune_rows()       — remove non-viable rows (no name, or name-only with weak columns)
7. rank_rows()        — completeness(0.28) + confidence(0.22) + source_quality(0.32)
                        + source_support(0.10) + actionable(0.08)
8. gap_fill.py        — focused queries for top-3 sparse rows, name-verified extraction
9. verify_rows()      — marketplace-only filter, source quality threshold, fallback
10. prune + re-rank   — second cleanup pass with final ordering
11. complete_job()    — write result JSON to SQLite
```

### Module status
| Module | Author | Status | Notes |
|---|---|---|---|
| `planner.py` | Claude | Stable | Good schema quality on gpt-4o-mini |
| `brave_search.py` | Claude | Stable | 15–25 unique URLs typical |
| `scraper.py` | Claude | Stable | trafilatura handles most pages; JS-rendered pages skipped |
| `extractor.py` | Claude + user | Stable | Semaphore + chunker fix in place |
| `merger.py` | Claude + user | Stable | Lookup refresh fix applied |
| `ranker.py` | Claude + user | Stable | source_quality dominant signal (0.32) |
| `gap_fill.py` | Claude + user | Functional | Entity-focused; residual contamination in edge cases |
| `source_quality.py` | User | Functional | Domain lists hand-curated; food/startup-biased |
| `verifier.py` | User | Functional | Conservative; fallback prevents empty results |
| `exporter.py` | Claude | Stable | JSON + CSV with per-column provenance |
| `llm.py` | Claude + user | Stable | 60s timeout, explicit retry loop, configurable per caller |

### What is stable
- Full pipeline from query to ranked table
- Per-cell provenance (source_url, title, snippet, confidence)
- SQLite caching of scraped pages (24h TTL in `/tmp`)
- JSON/CSV export with provenance columns
- Dark-theme UI with evidence modal, search examples, metadata bar

### What is still weak
- **Cell-level entity consistency:** gap-fill can still attach cells from a co-mentioned entity in edge cases
- **JS-rendered pages:** SPAs return empty content — skipped by `_MIN_TEXT_LENGTH` threshold
- **Source domain lists:** `source_quality.py` is calibrated for food/startup queries; other domains get neutral `unknown` score
- **No URL validation:** website values like `robertaspizza` (without protocol) are accepted
- **Schema planner edge cases:** very broad queries produce generic columns
- **No source diversity constraint:** one domain can contribute most cells across many rows

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

5. **No URL validation:** Extracted website values are not validated for format. Partial values like `robertaspizza` pass through.

6. **Latency:** 25–60s typical, plus 10–20s for gap-fill. Not suitable for real-time UX without streaming.

7. **Single-round gap-fill:** Entities still sparse after one enrichment round remain sparse. Second round would improve completeness at doubled cost.

8. **No query caching:** Re-running the same query re-executes the full pipeline. Only page scraping is cached.
