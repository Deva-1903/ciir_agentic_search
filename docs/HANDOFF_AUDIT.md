# Handoff Audit — AgenticSearch

**Date:** 2026-04-04  
**Auditor:** New session (no prior context; reconstructed from repo evidence)  
**Method:** Full file-by-file code + docs + test inspection. No live API calls.

---

## 1. Current Architecture Snapshot

The system is a clean, linear FastAPI pipeline. Architecture is disciplined — no multi-agent theater, no unnecessary abstractions. Every service is a focused module with a single public entry point.

### Actual pipeline as wired in `app/api/routes_search.py`:

```
POST /api/search → background task:
  1.  planner.py          → PlannerOutput (entity_type, columns, facets → search_angles)
  2.  brave_search.py     → BraveResult[] (parallel, URL dedup)
  3.  scraper.py          → ScrapedPage[] (async, semaphore=5, trafilatura→BS4, SQLite cache)
  3.5 reranker.py         → top-K ScrapedPage[] (cross-encoder or Jaccard fallback)
  4.  extractor.py        → EntityDraft[] (LLM per-page, chunked, field_validator at cell boundary)
  5.  merger.py           → EntityRow[] (fuzzy dedup, best-confidence cell wins)
      prune_rows()
  5.5 cell_verifier.py    → confidence penalty on weakly-aligned cells
  6.  rank_rows()         → 6-component weighted score
  7.  gap_fill.py         → targeted enrichment of top-3 sparse rows
      cell_verifier.py    → second pass (post gap-fill)
  8.  verify_rows()       → marketplace/low-quality filter
      prune + re-rank     → final ordering
  9.  complete_job()      → SQLite result storage
```

### Key services found:

| File                 | Purpose                                      | Status                        |
| -------------------- | -------------------------------------------- | ----------------------------- |
| `planner.py`         | Facet-typed schema planning (LLM + fallback) | Present, wired                |
| `reranker.py`        | Cross-encoder reranking + Jaccard fallback   | Present, wired                |
| `cell_verifier.py`   | Per-cell entity-alignment penalty            | Present, wired (2 call sites) |
| `field_validator.py` | URL/phone/rating normalization               | Present, wired into extractor |
| `ranker.py`          | 6-component scoring incl. source_diversity   | Present, wired                |
| `source_quality.py`  | Domain-based source classification           | Present, wired via ranker     |
| `verifier.py`        | Row-level marketplace/quality filter         | Present, wired                |
| `scripts/eval.py`    | CLI evaluation harness                       | Present, standalone           |

**Architecture verdict:** Clean and disciplined. No unnecessary rewrites occurred. All additions are incremental and wired into the existing pipeline at the correct seams.

---

## 2. Status by Improvement Phase

### Phase 1 — Facet-typed planning

**Status: DONE**

**Evidence:**

- **Schema model:** `SearchFacet` in `app/models/schema.py` (lines 37–50) — has `type`, `query`, `expected_fill_columns`, `rationale`. Pydantic `field_validator` normalizes type to canonical set or "other".
- **Canonical facet types:** `_CANONICAL_FACET_TYPES` in `schema.py` — 7 types (`entity_list`, `official_source`, `editorial_review`, `attribute_specific`, `news_recent`, `comparison`, `other`).
- **PlannerOutput:** `facets: List[SearchFacet]` field present (line 55). `search_angles` derived from facet queries for backward compat.
- **Planner prompt:** `app/services/planner.py` `_SYSTEM` prompt explicitly instructs 3–5 facets with distinct retrieval intent. Prompt describes all 6 non-other facet types.
- **Sanitization:** `_sanitize_facets()` drops empty queries, filters `expected_fill_columns` to schema columns, caps at 5.
- **Fallback:** `_fallback_plan()` produces 3 typed facets when LLM fails.
- **"name" preserved:** `_ensure_name_first()` guarantees "name" is first column.
- **Bounded:** facets capped at `[:5]`, columns at `[:8]`.
- **Metadata propagated:** `SearchMetadata.facets` field exists; populated in `routes_search.py`.
- **Tests:** `tests/test_planner.py` — 7 tests covering normalization, sanitization, fallback, LLM parse, empty-facet-fallback.

**Missing:** Nothing. Fully implemented and tested.

---

### Phase 2 — Cross-encoder reranking before extraction

**Status: DONE**

**Evidence:**

- **Module:** `app/services/reranker.py` — 160 lines, well-documented.
- **Model:** `cross-encoder/ms-marco-MiniLM-L-6-v2` via `sentence-transformers.CrossEncoder`. This is a real learned cross-encoder, not heuristic scoring.
- **Placement:** Called at step 3.5 in `routes_search.py`, between scrape (3) and extract (4). Confirmed by reading `_run_pipeline()` — reranking happens _before_ extraction, exactly as specified.
- **Top-K configurable:** `settings.rerank_top_k` (default 10) in `app/core/config.py`. `settings.rerank_enabled` toggle exists (default True).
- **Conditional activation:** Only triggers when `pages_scraped > rerank_top_k`.
- **Graceful degradation:** Three fallback paths: (1) `sentence-transformers` not importable → lexical, (2) model load fails → `_model_load_failed = True`, never retries → lexical, (3) `model.predict()` raises → lexical. All paths logged with warnings.
- **Lexical fallback:** Jaccard recall of query tokens in document (`_lexical_score()`). Simple and deterministic.
- **Async-safe:** `asyncio.to_thread()` wraps the sync CPU-bound `model.predict()`.
- **Metadata:** `rerank_info` dict with `scorer`, `pages_before`, `pages_after`, `top_scores` propagated to `SearchMetadata`.
- **Dependency:** `sentence-transformers>=2.7.0` in `pyproject.toml` dependencies.
- **Tests:** `tests/test_reranker.py` — 8 tests covering lexical scoring, page doc construction, top-K selection, empty input, cross-encoder failure fallback, top-K > input.

**Missing:** Nothing. Fully implemented, tested, configurable, and gracefully degrading.

---

### Phase 3 — Cell-level verification + field validation + source diversity

#### 3A — Cell-level verification

**Status: DONE**

**Evidence:**

- **Module:** `app/services/cell_verifier.py` — 130 lines.
- **Cell-level, not row-level:** `verify_row_cells()` iterates `row.cells.items()`, checking each non-skip cell individually.
- **Three rules:** (1) evidence snippet mentions entity name (exact normalized substring OR `partial_ratio ≥ 80`), (2) source title mentions entity name, (3) cell's source URL shares domain with entity's own website. Code in `_cell_is_aligned()`.
- **Penalty, not deletion:** Failing all three → `confidence *= 0.6`. Value and provenance preserved. `row.aggregate_confidence` recomputed.
- **Skip columns:** `_SKIP_COLS = {"name", "cuisine_type", "category", "type", "description", "overview", "summary"}`.
- **Wired twice:** Called at step 5.5 (post-merge) and again after gap-fill in `routes_search.py`. Both call sites verified.
- **No optional LLM verifier:** Not present. Only rule-based checks. This is explicitly by design per BUILD_JOURNAL rationale.
- **Tests:** `tests/test_cell_verifier.py` — 7 tests: name-in-evidence kept, no-name-penalized (0.54), own-domain aligned, skip-cols, aggregate recompute, no-name-row skipped, title-based verification.

**Missing:** No LLM verifier for borderline cases (acknowledged as intentionally excluded to avoid cost/latency).

#### 3B — Field validation / normalization

**Status: DONE**

**Evidence:**

- **Module:** `app/services/field_validator.py` — 130 lines.
- **Website:** `normalize_website()` — adds `https://`, lowercases host, strips trailing slash + fragment, rejects bare words without TLD (the `robertaspizza` case). `_BARE_DOMAIN_RE` regex validates structure.
- **Phone:** `validate_phone()` — requires ≥7 digits after stripping junk.
- **Rating:** `validate_rating()` — extracts first number, requires 0–10 range.
- **Dispatch:** `validate_and_normalize(col, value)` routes by column name sets.
- **Wired into extractor:** `app/services/extractor.py` calls `validate_and_normalize(col, value)` inside the cell-parse loop, dropping malformed cells with debug log. Confirmed at line ~131.
- **Tests:** `tests/test_field_validator.py` — 16 tests across website (6), phone (4), rating (4), dispatch (2+).

**Missing:** No address validation (acknowledged as not applicable — addresses are free-form text).

#### 3C — Source diversity / consensus

**Status: DONE**

**Evidence:**

- **In ranker:** `_source_diversity(row)` in `app/services/ranker.py` — computes `1 - (max_domain_share / total_cells)`. Returns 0.0 for single-domain rows, approaches 1.0 for fully diverse rows.
- **In weights:** `_WEIGHTS["source_diversity"] = 0.08` — a tie-breaker, not a gate. Total weights sum to 1.0.
- **Still interpretable:** All 6 components are simple, documented, and produce [0,1] values.
- **Test:** `TestSourceDiversity::test_multi_domain_row_beats_single_domain_row_when_others_equal` in `test_ranker.py`.

**Note:** This is source diversity, not consensus. There is no cross-source value agreement check (e.g. "did two sources give the same phone number?"). The improvement plan asked for "source diversity / consensus" — the diversity part is done, the consensus part is absent. However, BUILD_JOURNAL and README are honest about this being a diversity signal, not consensus.

**Missing:** True cross-source consensus checking (e.g., comparing values across sources for the same cell). This was not explicitly committed to in the plan — "consensus" was listed with a slash, suggesting diversity OR consensus.

---

### Phase 4 — Evaluation harness / benchmark

**Status: DONE (with caveats)**

**Evidence:**

- **Script:** `scripts/eval.py` — 260 lines, well-structured CLI with argparse.
- **Queries:** `docs/eval_queries.json` — 10 queries across 3 categories (food: 4, tech: 4, travel: 2).
- **Metrics computed:** rows_returned, avg_cells_per_row, fill_rate, actionable_rate, multi_source_rate, avg_aggregate_confidence, avg_source_diversity, duration_seconds, pages_scraped, pages_after_rerank, rerank_scorer, entities_extracted/after_merge, gap_fill_used.
- **Aggregate summary:** Mean across all successful queries for key metrics.
- **Output:** JSON report (`data/eval_<tag>_<ts>.json`) and CSV (`data/eval_<tag>_<ts>.csv`).
- **Tag-based comparison:** `--tag` flag supports labeling runs (e.g. `no-rerank` vs `full`).
- **Category filter:** `--category` flag for subset evaluation.

**Caveats:**

| Expected              | Found                                                                                                                                                                               |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 8–12 queries          | 10 queries — ✅                                                                                                                                                                     |
| Multiple query types  | 3 categories (food/tech/travel) — ✅                                                                                                                                                |
| rows returned         | ✅                                                                                                                                                                                  |
| avg filled cells      | ✅ (avg_cells_per_row + fill_rate)                                                                                                                                                  |
| actionable fields     | ✅ (actionable_rate)                                                                                                                                                                |
| multi-source row rate | ✅                                                                                                                                                                                  |
| avg source quality    | ❌ Not computed (declared in `_compute_metrics` return stub as `avg_source_quality: 0.0` but never populated from actual row data)                                                  |
| avg source diversity  | ✅ (recomputed from raw cell URLs)                                                                                                                                                  |
| Ablation mode         | ⚠️ Partial — tag-based only. No in-script toggle. Requires server restart with env var `RERANK_ENABLED=false`. No docs on which env vars to toggle for verifier/diversity ablation. |

**Missing:**

1. `avg_source_quality` metric is declared but always returns 0.0 for failed queries and is not computed from successful result rows. The server does not expose per-row source_quality in the API response, so the eval harness cannot compute it without internal access. This is an **implementation gap**.
2. No formal ablation instructions. The `--tag` pattern works but there are no documented env vars for toggling cell_verifier or source_diversity independently.
3. No tests for `eval.py` itself.

---

### Phase 5 — README / documentation updates

**Status: DONE**

**Evidence:**

| Requirement                            | Present in README?                                                                                                            |
| -------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| Current pipeline accurately described  | ✅ Architecture diagram updated with reranker, cell verifier, 6-component ranker                                              |
| Facet-typed planning documented        | ✅ Feature #1 rewritten for facets                                                                                            |
| Reranking before extraction documented | ✅ Feature #7                                                                                                                 |
| Cell-level verification documented     | ✅ Feature #8                                                                                                                 |
| Source diversity documented            | ✅ Feature #10                                                                                                                |
| Evaluation setup documented            | ✅ "Evaluation" section with CLI examples                                                                                     |
| Why improvements prioritized           | ✅ "Why these improvements were prioritized" section                                                                          |
| Honest / no overclaiming               | ✅ — "All improvements are heuristic", "not precision/recall against labeled data", explicit caveats on fuzzy matching limits |
| Ranker formula updated                 | ✅ 6 components with exact weights                                                                                            |
| Known limitations updated              | ✅ 8 items, includes cell verifier false matches, no ground-truth eval                                                        |
| Module map updated                     | ✅ Includes reranker, cell_verifier, field_validator, scripts/, docs/                                                         |
| API response example updated           | ✅ Includes facets, pages_after_rerank, rerank_scorer                                                                         |

**Missing:** Nothing material. README is comprehensive and honest.

---

## 3. Build Journal Audit

### Structure quality:

BUILD_JOURNAL.md is 863 lines with 11 iterations, plus summary sections:

- ✅ Evolution Snapshot
- ✅ Attribution Note
- ✅ Overall Progress Summary (6 phases)
- ✅ Key Design Decisions
- ✅ Failure / Debug Log
- ✅ Before vs After Improvements
- ✅ Current Architecture (pipeline + module status table)
- ✅ Known Limitations

### Per-iteration quality (Iterations 8–11, the new work):

Each of Iterations 8, 9, 10 records: date, author, goal, issue discovered, why old approach was insufficient, what was built, why chosen (with alternatives rejected), what happened when tested, failures observed, tradeoffs introduced, files affected, next step. This is excellent engineering journal quality.

Iteration 11 (eval harness) is slightly thinner — no "failures observed" section — but still has design choices and tradeoffs.

### Staleness issues found:

1. **Evolution Snapshot (lines 9–28):** Stops at "Current state: full 8-stage pipeline". Does **not** mention Iterations 8–11 (facets, reranker, cell verifier, field validator, diversity, eval harness). **STALE.**

2. **"Remaining gap" (line 578):** Says "README not yet updated for Phases 4–6". But README **was** updated. **STALE.**

3. **"What is still weak" (line 835):** Says "No automated evaluation: improvements validated by unit tests and spot-checks, not systematic benchmarks". But `scripts/eval.py` exists. **STALE.**

4. **"Current state" in Evolution Snapshot:** Says "8-stage pipeline" — should say 11+ stages given reranker, cell_verifier × 2.

### Journal verdict:

- **Trustworthy as a handoff artifact:** Yes, for Iterations 1–11 content. The iteration entries themselves are thorough and honest.
- **Stale in 3 places:** Evolution Snapshot, "Remaining gap", and one "What is still weak" bullet were not updated after the README and eval harness were completed in the same session.
- **Implementation ahead of documentation:** Yes, in those 3 spots. The code is ahead of the journal's summary sections.

---

## 4. Latency / Responsiveness Audit

### Implemented latency-minded design choices:

| Feature                  | Implementation                                                    | File                                |
| ------------------------ | ----------------------------------------------------------------- | ----------------------------------- |
| Async job model          | POST returns job_id immediately; pipeline runs as background task | `routes_search.py`                  |
| Polling UX               | GET poll endpoint; UI polls every 2s                              | `routes_search.py`, `static/app.js` |
| Bounded concurrency      | `max_concurrent_scrapes=5`, `max_concurrent_extractions=3`        | `config.py`                         |
| SQLite page cache        | 24h TTL; avoids re-scraping                                       | `scraper.py`, `config.py`           |
| LLM timeouts             | 30s per extraction call, 60s general LLM timeout                  | `config.py`, `llm.py`               |
| Chunk caps               | `max_chunks_per_page=2`, `chunk_token_limit=3000`                 | `config.py`                         |
| Fail-fast extraction     | `extract_llm_max_attempts=1`                                      | `config.py`                         |
| Bounded gap-fill         | 3 entities × 2 URLs max                                           | `config.py`                         |
| Pre-extraction filtering | Reranker reduces page set before expensive LLM extraction         | `reranker.py`, `routes_search.py`   |
| DB outside project root  | `/tmp/agentic_search.db` avoids `--reload` restart loops          | `config.py`                         |

### Documented latency story:

- README "Latency and cost" section: ✅ — planner ~0.5s, extractor ~1-2s/page, gap-fill 5-15s, total 25-60s.
- README "Known limitations": ✅ — "25-60 seconds", "cross-encoder adds ~1-2s; gap-fill adds 10-20s."
- BUILD_JOURNAL latency point: ✅ — Known Limitation #6.

### Implemented but under-documented:

- The reranker's `asyncio.to_thread()` for CPU-bound model inference — not called out as a latency decision.
- The conditional activation of reranking (`pages_scraped > rerank_top_k`) — avoids overhead for small scrapes.

### Documented but not implemented:

- None found. All documented latency features are actually present in code.

---

## 5. Tests / Validation Run

### What was executed:

```
python -m pytest tests/ -v --tb=short
```

**Result: 121 passed in 0.39s**

### Test coverage by module:

| Module                     | Test file                                             | Test count                 | Covers new features?                                      |
| -------------------------- | ----------------------------------------------------- | -------------------------- | --------------------------------------------------------- |
| planner.py                 | test_planner.py                                       | 7                          | ✅ Facet normalization, sanitization, fallback, LLM parse |
| reranker.py                | test_reranker.py                                      | 8                          | ✅ Lexical scoring, top-K, empty, fallback, truncation    |
| cell_verifier.py           | test_cell_verifier.py                                 | 7                          | ✅ Alignment, penalty, domain, skip-cols, aggregate       |
| field_validator.py         | test_field_validator.py                               | 16                         | ✅ URL, phone, rating, dispatch                           |
| ranker.py (diversity)      | test_ranker.py                                        | 1 (in TestSourceDiversity) | ✅ Multi-domain beats single-domain                       |
| extractor.py               | test_extractor.py                                     | present                    | ✅ (field_validator wired in)                             |
| merger.py                  | test_merger.py                                        | 7                          | ✅                                                        |
| verifier.py                | test_verifier.py                                      | 3                          | ✅                                                        |
| source_quality.py          | test_source_quality.py                                | 3                          | ✅                                                        |
| gap_fill.py                | test_gap_fill.py                                      | 1                          | ✅                                                        |
| url.py, text.py, dedupe.py | test_url_utils.py, test_text_utils.py, test_dedupe.py | 20+                        | ✅                                                        |
| scripts/eval.py            | —                                                     | 0                          | ❌ No tests                                               |

### What could not be verified:

1. **Live pipeline execution:** Requires Brave + OpenAI API keys. Not attempted to avoid cost.
2. **Eval harness live run:** Requires running server. Not attempted.
3. **Cross-encoder model load:** Requires `sentence-transformers` and model download. Tests monkeypatch around it; actual model load not tested in this session.
4. **Lint/type checks:** No `mypy` or `ruff` configuration found in `pyproject.toml`. No configured linter to run.

---

## 6. Gaps and Risks

### Priority-ordered gaps:

1. **BUILD_JOURNAL staleness (3 spots) — Documentation**
   - Evolution Snapshot doesn't mention Iterations 8–11.
   - "Remaining gap" says README not updated — it was.
   - "What is still weak" says no automated evaluation — eval harness exists.
   - **Why it matters:** Journal is the primary handoff artifact. Stale summary sections undermine trust.

2. **`avg_source_quality` metric missing from eval harness — Code**
   - Declared in the return dict stub but never computed from actual result data.
   - Server API response doesn't include per-row `source_quality`, so external eval can't compute it.
   - **Why it matters:** Source quality is the dominant ranking signal (0.32 weight). Not measuring it in eval is a blind spot.

3. **No tests for `scripts/eval.py` — Quality**
   - The eval script has 260 lines of logic including metric computation. Zero test coverage.
   - **Why it matters:** The harness is meant to validate the system. If the harness itself has bugs (e.g. in `_compute_metrics`), the validation is unreliable.

4. **Ablation mode is underdocumented — Documentation**
   - Tag-based comparison works, but there are no instructions for which env vars to toggle for each ablation (reranker, cell_verifier, diversity).
   - Only `RERANK_ENABLED` exists as a config toggle. Cell verifier and diversity have no config toggles.
   - **Why it matters:** Without toggleable config for each feature, ablation requires code changes — making it impractical.

5. **No consensus checking — Code (minor)**
   - Source diversity measures domain count. No check for cross-source value agreement.
   - **Why it matters:** Low — diversity is a reasonable proxy. But the original plan listed "consensus" as a possibility.

6. **No lint/type configuration — Quality (minor)**
   - No `mypy`, `ruff`, or `flake8` configured. Code quality relies on tests alone.

---

## 7. Minimal Next Actions

In priority order (smallest, safest changes first):

1. **Fix BUILD_JOURNAL staleness** — Update 3 stale lines:
   - Evolution Snapshot: add bullets for facet planning, reranker, cell verifier, field validator, diversity, eval harness.
   - "Remaining gap": remove "README not yet updated" (it was).
   - "What is still weak": strike/update "No automated evaluation" (eval harness exists).

2. **Fix `avg_source_quality` in eval harness** — Either:
   - (a) Add `source_quality` to `SearchMetadata` or `EntityRow` in server response so eval can read it, or
   - (b) Remove the `avg_source_quality` field from eval metrics to avoid a misleading zero.

3. **Add ablation config toggles** — Add `cell_verifier_enabled: bool = True` and `source_diversity_weight: float = 0.08` to `config.py` so they can be toggled via env vars for ablation.

4. **Add basic test for eval metrics** — Unit test `_compute_metrics()` with a mock result dict.

---

## 8. Optional Patch Plan

If applying fixes, this is the safe order:

```
Step 1: Fix BUILD_JOURNAL stale lines        (docs only, zero code risk)
Step 2: Fix avg_source_quality in eval.py     (one file, no pipeline change)
Step 3: Add ablation config toggles           (config.py + routes_search.py + ranker.py, low risk)
Step 4: Add eval metrics test                 (tests/ only, additive)
```

Each step is independently shippable. No step depends on another. None touch the core pipeline logic.

---

_End of audit._
