# AgenticSearch — System Design Document

> End-to-end explanation, decision rationale, demo vs. production trade-offs, and generalization guide.

---

## Table of Contents

1. [What the system does](#1-what-the-system-does)
2. [Pipeline walk-through](#2-pipeline-walk-through)
3. [Key design decisions and why](#3-key-design-decisions-and-why)
4. [What could have been done better](#4-what-could-have-been-done-better)
5. [Demo vs. production: what changes](#5-demo-vs-production-what-changes)
6. [Generalization: where domain assumptions crept in and how to fix them](#6-generalization-where-domain-assumptions-crept-in-and-how-to-fix-them)

---

## 1. What the system does

AgenticSearch takes a free-text query like `"top open source observability platforms"` or `"best pizza restaurants in Brooklyn"` and returns a structured, ranked table of real-world entities. Every cell in the table carries a source URL, evidence snippet, and confidence score — the system never invents data.

The core promise is **provenance-first entity discovery**: instead of returning a paragraph or a ranked list of links, it returns structured rows like a database table, where each value is grounded in a specific page on the web.

```
Query: "AI safety researchers"

name             | affiliation          | role_or_title       | notable_work
Yoshua Bengio    | MILA                 | Professor           | Responsible Scaling Policy
Paul Christiano  | ARC (Alignment RC)   | Research Director   | Eliciting Latent Knowledge
...
```

---

## 2. Pipeline walk-through

The pipeline is a linear sequence of async stages, each feeding the next. Understanding each stage is essential for understanding every design choice.

### Stage 1 — Query normalization (`query_normalizer.py`)

**What it does**: light cleanup before retrieval. Strips filler like "give me" or "find me", normalizes casing, removes redundant punctuation.

**Why**: LLMs and search APIs are sensitive to query phrasing. A query like `"Can you find me the best startups"` should retrieve the same results as `"best startups"`. This normalization happens before the plan is built.

**Current limitation**: it's rule-based, not semantic. Synonyms aren't resolved. `"VC-backed companies"` and `"venture-funded startups"` take different paths.

---

### Stage 2 — Schema planning (`planner.py`)

**What it does**: classifies the query into one of 6 structural entity-kind families and selects a fixed schema for the output table.

The 6 families:
| Family | Shape | Example queries |
|---|---|---|
| `organization_company` | Orgs with focus area, HQ, website | startups, nonprofits, agencies |
| `place_venue` | Physical locations with contact info | restaurants, museums, parks |
| `software_project` | Code repos with license, stack, maintainer | open source tools, frameworks |
| `product_offering` | Products with price, brand, features | laptops, SaaS products |
| `person_group` | People with affiliation, role, work | researchers, founders, artists |
| `generic_entity_list` | Fallback — generic 5-column schema | anything else |

**Classification is done in two passes:**
1. **Deterministic** (`classify_query_family`): keyword signals on word boundaries. Fast, always produces a result, used as the primary classifier.
2. **LLM-assisted facet generation**: given the family and fixed columns, ask the LLM to produce 4–5 typed retrieval facets (entity_list, official_source, editorial_review, attribute_specific, comparison/news). The schema itself is NOT delegated to the LLM — only the search angle refinement is.

**Why the schema is fixed, not LLM-generated**: LLMs asked to freely design a schema produce inconsistent column names across runs (`"founded_year"` vs. `"year_founded"` vs. `"founding_year"`). Merging, ranking, and gap-fill all rely on stable column names. Fixing the schema per family gives consistency at the cost of some column specificity.

**Deterministic fallback**: if the LLM call fails, the planner generates facets procedurally from the query topic string. The system works without LLM access for this stage.

---

### Stage 3 — Search (`brave_search.py`)

**What it does**: takes the 4–5 search angles from the plan and runs them in parallel through the Brave Search API. Deduplicates results by URL.

**Why Brave**: unlike Google Search API (expensive, rate-limited) and SerpAPI (costly at scale), Brave's API is affordable and returns raw web results without heavy curation. This suits a retrieval-pipeline approach: we want to see diverse source types, not pre-filtered "answer" results.

**Why parallel angle queries**: a single query for `"AI safety researchers"` returns mostly list/overview pages. The facet system fires separate queries like:
- `"AI safety researchers"` (entity list)
- `"AI safety researcher official page"` (official source)
- `"AI safety research overview"` (editorial)
- `"AI safety researcher affiliation role"` (attribute-specific)

Each angle surfaces different source types. Combined, they give the pipeline enough diverse evidence to fill multiple columns reliably.

**Current limitation**: 5 results per angle × 5 angles = 25 candidates. This is a small retrieval set. In production you'd want a larger pool and smarter deduplication (e.g., prefer canonical URLs over tracking variants).

---

### Stage 4 — Scraping (`scraper.py`)

**What it does**: fetches each URL, extracts clean text using `trafilatura`, extracts lightweight HTML metadata (headings, structured items), detects the page's evidence regime, and caches the result in SQLite.

**Evidence regime classification** (`evidence_regimes.py`): each scraped page is labelled with one of:
- `official_site` — entity's own homepage
- `directory_listing` — aggregator/directory page (YC, Crunchbase, Yelp)
- `editorial_article` — review, blog, news
- `local_business_listing` — Google Maps-style listing
- `software_repo_or_docs` — GitHub, GitLab, readthedocs
- `marketplace_aggregator` — Amazon, App Store
- `unknown`

This label is used downstream to route extraction (deterministic vs. LLM), weight source quality, and accept/reject official-site candidates. It is classified without reference to the query — purely from URL shape, domain signals, and page structure.

**Why trafilatura over BeautifulSoup**: trafilatura is purpose-built for extracting main-content text from web pages (removing nav, ads, footer, cookie banners). BeautifulSoup gives you the full DOM — useful for structured extraction but requires custom clean-text logic per site layout. For our use case (pass cleaned text to an LLM), trafilatura's output is far better.

**JS rendering**: optional, budget-capped. Some pages (SPAs, Yelp, dynamic menus) return near-empty HTML to static fetchers. A small Playwright-based fallback is available but deliberately limited to 2 pages/query to avoid blowing up latency.

**Caching**: SQLite cache with 24-hour TTL. This matters for development iteration — re-running a query reuses pages already fetched. In production this cache also prevents re-scraping the same content within a cache window across different jobs.

---

### Stage 5 — Reranking (`reranker.py`)

**What it does**: if more pages were scraped than the extraction budget allows (configurable, default 10), a cross-encoder model (`ms-marco-MiniLM-L-6-v2`) scores each page against the original query and selects the top-K most relevant.

**Why**: LLM extraction is expensive. If you scraped 20 pages but only have budget to send 10 to the LLM, you want to send the 10 most relevant — not the first 10 by URL order. The cross-encoder gives a semantically grounded relevance score that's much better than BM25 or simple keyword overlap.

**Why a cross-encoder, not a bi-encoder**: bi-encoders (embedding similarity) are fast but produce coarse relevance scores. Cross-encoders are slower but evaluate the query-document pair jointly, giving better ranking precision. At 20 pages with ~500 tokens each, the latency is acceptable.

---

### Stage 6 — Candidate discovery (`extractor.py`, mode="discovery")

**What it does**: the first of two extraction passes. Sends each page to the LLM with a lightweight schema (first 4 columns only: name + 3 anchor fields) and asks for all plausible entity candidates. Recall-first: better to extract 30 candidates and filter later than to miss 5 real entities at this stage.

**Two-pass architecture (discovery + fill)**:
- **Discovery** (this stage): high recall, lightweight columns, from all reranked pages
- **Fill** (gap-fill stage): high precision, full columns, from 2 targeted follow-up URLs per sparse entity

This was a deliberate architectural choice. Doing full-schema extraction in a single pass causes the LLM to hallucinate missing fields rather than omitting them. Discovery mode asks for far less, so the LLM's output is more reliable.

**Deterministic extractors first**: before calling the LLM, the page is checked against pattern-based deterministic extractors. If the page is a GitHub repository page, structured metadata (stars, license, language, maintainer) is extracted directly from the HTML — no LLM needed. This is faster, cheaper, and more accurate for structured source types.

**Provider fallback**: if the primary provider (default: OpenAI) fails or times out, the system attempts a fallback provider (Groq). A 60-second in-process cooldown prevents all concurrent extractions from hammering a rate-limited fallback simultaneously.

---

### Stage 7 — Merge (`merger.py`, `dedupe.py`)

**What it does**: collapses all `EntityDraft` objects (one per page per entity) into canonical `EntityRow` objects. Two entities are merged if:
1. Their website domains match (strong signal), OR
2. Their names are fuzzy-similar at threshold 82% (rapidfuzz `token_set_ratio`)

When merging cells for the same column, the cell with higher confidence wins (with a tie-break for longer evidence snippet).

**Why fuzzy name matching**: the same entity appears differently across sources. "Prometheus" on GitHub vs. "Prometheus monitoring" on a review site vs. "CNCF Prometheus" on a directory. Pure exact matching would create 3 separate rows. Fuzzy matching at 82% collapses these correctly in most cases.

**Known limitation**: 82% threshold can merge entities that are similar but distinct (e.g., "Pizza Palace" and "Pizza Place"). This is a recall-precision trade-off. We accept occasional bad merges and rely on cell-level alignment verification to catch the resulting inconsistency.

---

### Stage 8 — Official-site resolution (`official_site.py`)

**What it does**: for each candidate row, scans all scraped pages to find the best-guess official website/homepage. Attaches a `canonical_domain` to the row and sets/replaces the `website` cell.

**Acceptance criteria**:
- The entity name must appear in the page title or first 200 chars of body
- The page must not be a directory/listing/marketplace page (regime filter)
- Score must exceed 0.7 (base quality + bonuses for shallow path, official-hint words in title)
- The page must be from a non-editorial, non-marketplace source

**Why this is hard**: a page about "Best Pizza in Brooklyn" mentions 8 restaurant names in the first 200 chars. Without tight entity-name matching, the wrong restaurant's homepage gets attached. The 200-char window (tightened from 500 in this last pass) reduces this.

---

### Stage 9 — Gap-fill (`gap_fill.py`)

**What it does**: identifies the top 3–5 sparsest rows (most empty columns) after the initial merge. For each sparse row, builds targeted search queries for the missing fields (e.g., `"Prometheus license"`, `"Prometheus maintainer organization"`), fetches 1–2 URLs, and runs fill-mode extraction to populate only the missing cells.

**Why a separate fill pass**: discovery extraction from list/editorial pages gives you entity names and light attributes. But fields like `license`, `headquarters`, `phone_number` are rarely on list pages — they're on the entity's own page or a structured directory. A second targeted pass for sparse rows dramatically improves fill rate without blowing up the discovery budget.

**Bounds**: hardcoded at ≤5 entities, ≤2 URLs per entity, 1 round. This prevents the system from spending unbounded time on gap-filling. In production, these would be configurable per query type or per time budget.

---

### Stage 10 — Cell verification (`cell_verifier.py`)

**What it does**: for each row, checks whether each cell's evidence snippet actually mentions the entity name. Cells that don't mention the entity name get their confidence multiplied by 0.6.

**Why**: merge contamination. Entity A and Entity B are merged (fuzzy name match). Some of Entity B's cells get absorbed. Cell verifier catches that the evidence snippet for those cells never mentions Entity A's name — they're penalized. This doesn't prevent the bad merge but it makes the contaminated cells rank lower.

---

### Stage 11 — Row verification (`verifier.py`)

**What it does**: hard-rejects rows that are clearly bad. Applied AFTER ranking so the ranker has had a chance to surface good rows even from sparse evidence.

Rejection reasons:
- `not_viable`: no name cell, or generic name with no actionable fields
- `cta_text`: entity name is a CTA phrase ("Order Online", "Book Now")
- `article_title`: entity name is a list-article heading ("Best Pizza Places in Brooklyn")
- `pseudo_entity`: abstract category label masquerading as an entity ("AI Copilots & Agents for Healthcare")
- `marketplace_only`: only marketplace source evidence for a strict ("best/top") query
- `low_quality_sparse`: source quality < 0.2, no actionable fields, < 2 sources

**Fallback behaviour**: if ALL rows would be rejected, the original set is returned. This prevents empty results from over-filtering.

**Final row cap**: strict queries ("best", "top", "leading") are capped at 15 rows; others at 20. This prevents 25+ row tables from bloating reviewer demos.

---

### Stage 12 — Ranking (`ranker.py`)

**What it does**: scores each row on a weighted sum of 12 dimensions, then sorts descending.

The 12 dimensions:
| Dimension | Weight | What it measures |
|---|---|---|
| `source_quality` | 0.18 | Weighted average of source quality scores |
| `completeness` | 0.16 | Fraction of schema columns filled |
| `avg_confidence` | 0.16 | Mean confidence of all cells |
| `local_fit` | 0.08 | Location match between row and query location phrase |
| `source_support` | 0.06 | log₂(1 + sources_count) — multi-source bonus |
| `source_diversity` | 0.05–0.08 | Fraction of cells from distinct domains |
| `actionable` | 0.05 | Has at least one non-weak-signal column filled |
| `reputation` | 0.04 | Proxy from source diversity + rating fields |
| `freshness` | 0.04 | Year mentions in source URLs/titles |
| `official_fit` | 0.04 | Has official/canonical domain (family-weighted) |
| `field_importance` | 0.12 | Per-family important columns filled |
| `structured_fit` | 0.02 | Has evidence from structured source type |

**Ranking is transparent**: the weights are in a single dict, the breakdown is exposed per row, and it's a simple weighted sum — no learned model.

---

## 3. Key design decisions and why

### Decision 1: Fixed schema families, not free-form LLM planning

**What was decided**: instead of letting the LLM freely design columns, the planner classifies the query into 6 structural families and returns columns from a fixed template for that family.

**Why**: free-form LLM planning produces inconsistent column names across runs. "founded_year" vs "year_founded" vs "founding_year" — three column names for the same concept means the merger can't merge them, the ranker can't weight them, and the gap-fill can't query for them. Downstream code breaks.

**The trade-off**: a restaurant and a hiking trail share the same `place_venue` schema even though their relevant attributes differ. The fixed schema is correct on average but loses specificity for edge cases.

**What could be better**: a small set of "schema variants" per family (e.g., `place_venue_dining` vs `place_venue_cultural`) that share a base schema but add 1–2 family-specific columns. Still deterministic and stable, but more expressive.

---

### Decision 2: Evidence regimes as first-class pipeline objects

**What was decided**: every scraped page is labelled with an evidence regime before extraction. That label routes extraction (deterministic vs. LLM), weights source quality, and gates official-site resolution.

**Why**: the same extraction strategy doesn't work for all page types. A GitHub page has structured metadata (JSON-LD, `<meta>` tags, HTML sections) that deterministic parsers can extract reliably. An editorial article is running prose that requires the LLM. A Yelp listing has structured business data. Treating all pages identically wastes LLM calls and gets worse results.

**Why it's general**: the regime labels are URL-shape + structure-based, not query-dependent. The same classifier works for a software query and a restaurant query. It doesn't know or care what you're searching for.

---

### Decision 3: Deterministic extractors before LLM fallback

**What was decided**: before sending a page to the LLM, check a library of deterministic extractors. If a deterministic extractor yields sufficient coverage, skip the LLM for that page.

**Why**: deterministic is cheaper, faster, and more precise for structured source types. A GitHub page consistently returns stars, license, language, maintainer from the same HTML locations. An LLM might get these right 95% of the time. The deterministic extractor gets them right 99%+ of the time, in milliseconds, for free.

**The regime coverage**:
- `software_repo_or_docs` → structured GitHub/GitLab metadata
- `directory_listing` → structured JSON-LD, microdata, or repeated listing patterns
- `local_business_listing` → schema.org LocalBusiness markup
- `official_site` + `editorial_article` → hybrid (deterministic for structured parts, LLM for prose)

---

### Decision 4: Async pipeline, background jobs, SQLite polling

**What was decided**: POST /api/search immediately returns a job_id. The pipeline runs as a FastAPI background task. The frontend polls GET /api/search/{job_id} every 2 seconds.

**Why**: a web search + scraping pipeline takes 15–60 seconds. A synchronous HTTP request that holds for 60 seconds is a bad user experience and ties up server resources. The background job + polling pattern decouples pipeline execution from HTTP request lifecycle.

**Why SQLite and not Redis**: for a single-instance reviewer demo, SQLite is zero-infrastructure. Redis would require a separate service, environment variables, and dependency. SQLite in `/tmp` is fine for a demo. This is the main thing that must change for production (see Section 5).

---

### Decision 5: Two-pass extraction (discovery + fill)

**What was decided**: first pass extracts candidates with a lightweight schema (4 columns). Second pass (gap-fill) runs targeted queries for sparse rows with full-schema fill-mode extraction.

**Why**: single-pass extraction with a full schema causes the LLM to hallucinate missing values — it tries to fill every column rather than omitting what it doesn't know. The discovery pass is structurally recall-optimized: the model sees fewer columns and is less tempted to invent. The fill pass runs on fewer rows (sparse only) with targeted queries that are more likely to return the specific attribute needed.

---

### Decision 6: Source quality from URL shape, not domain curated lists

**What was decided**: source quality scores are computed primarily from URL structure (path depth, editorial segments like `/article/`, directory segments like `/category/`, homepage indicators) with a small curated domain list as a secondary boost.

**Why**: a hard-coded curated list requires constant maintenance and is vertical-specific. "techcrunch.com is editorial" is correct but doesn't generalize. URL shape is structural: any domain with `/blog/post-slug` is editorial-shaped regardless of who owns it.

**The curated list's role**: it only provides a quality boost (0.8 vs. 0.7 for shape-detected editorial), not hard rejection. Hard rejection uses `is_curated_third_party()` which only rejects known non-entity-page domains (YCombinator company pages used as website cells, etc.).

---

## 4. What could have been done better

### 4a. Merge contamination is detected, not prevented

**Current state**: the cell verifier penalizes cells whose evidence doesn't mention the entity name. It's a post-hoc mitigation.

**Root cause**: fuzzy name matching (82% threshold) can merge "Pizza Palace" and "Pizza Place". After merge, cells from the wrong entity get absorbed. The verifier lowers their confidence but doesn't remove them.

**Better approach**: during merge, for each candidate merge check that the entity name token-overlaps with the candidate draft's evidence snippet at the cell level before absorbing. If the overlap is below a threshold, create a new canonical entity instead of merging. This is pre-merge prevention, not post-merge penalization.

---

### 4b. Geographic filtering is only in the ranker, not the verifier

**Current state**: for place/venue queries with a location phrase, a `local_fit` score (0.0–1.0 based on token overlap between query location and row location field) is one of 12 ranking dimensions with weight 0.08.

**Problem**: a London restaurant can still appear in a Brooklyn pizza query output — it just ranks lower. If it has an official site, high confidence cells, and multi-source support, it may still rank top-5.

**Better approach**: for `place_venue` family queries with an explicit location phrase, add a hard filter in `verify_rows` that rejects any row where the `location`/`address` field is present AND has zero token overlap with the query location. This works because geographic mismatches are almost always zero-overlap (London vs. Brooklyn), not low-overlap.

**Why it wasn't done**: fear of false positives. "New York" vs "Brooklyn" has zero overlap but Brooklyn is in New York. Implementing this correctly requires either a geographic containment dataset or a more careful overlap definition.

---

### 4c. Article-title detection is syntactic, not semantic

**Current state**: `_looks_like_article_title` matches patterns like `"Best X in Y"` (4+ words, starting with superlative).

**Problem**: misses patterns like `"The Ultimate Guide to AI Startups"`, `"A Roundup of Observability Tools"`, or `"Our Favorite Brunch Spots"`. These are clearly article titles but don't start with a superlative.

**Better approach**: a small binary classifier trained on entity name vs. article title examples. Even a logistic regression over TF-IDF features would outperform regex. Alternatively, the LLM prompt could include explicit negative examples of article titles to reduce their extraction frequency at the source.

---

### 4d. Official-site resolution doesn't detect cross-entity conflicts

**Current state**: the official site resolver runs per-row and picks the best-scoring page for each row independently. Two different rows can be assigned the same page as their official site.

**Better approach**: after assigning official sites, check for domain conflicts. If two rows have the same canonical domain, the one with the lower assignment score has its website cell cleared (or flagged). This prevents two "Roberta's" variants from both getting `robertaspizza.com`.

---

### 4e. No feedback loop between extraction and planning

**Current state**: if extraction returns 0 entities from a page, that information doesn't feed back into planning for the next run or for this run. The same search angles are always used.

**Better approach**: log extraction failure rates per search angle and use them as a signal for facet generation quality. If the "official website" angle consistently returns 0 entities from official pages, the planner facet generation is wrong for that query type.

---

### 4f. Gap-fill is not adaptive

**Current state**: gap-fill always runs on the top 3–5 sparsest rows, runs 2 URLs each, and stops after 1 round.

**Better approach**: rank-order gap-fill candidates by (columns missing × row importance score). Allocate URL budget dynamically — a row ranked #1 but missing 4 columns deserves more gap-fill budget than a row ranked #15 missing 1 column.

---

### 4g. Evaluation harness is unlabeled

**Current state**: `eval.py` uses proxy metrics (fill rate, official-site rate, multi-source rate). The labeled set (`eval_labeled_queries.json`) has only ~10 queries with expected entity lists.

**Problem**: you can't confidently say "the filtering changes made quality better" without precision/recall measurements over a held-out set. Proxy metrics can all improve while output quality gets worse (e.g., fewer rows with higher fill rate but wrong entities).

**Better approach**: a labeled evaluation set of 50–100 queries with known ground-truth entity sets. Even crowd-sourced labels are far better than none.

---

## 5. Demo vs. production: what changes

This table summarizes what must change, what should change, and what is fine as-is.

### Infrastructure

| Component | Demo (current) | Production |
|---|---|---|
| **Job store** | SQLite `/tmp/agentic_search.db` per-container | PostgreSQL or Redis — shared across all instances |
| **Page cache** | SQLite `/tmp` per-container | Shared cache (Redis + S3, or PostgreSQL) |
| **Deployment** | Single instance (`instance_count: 1`) mandatory | Horizontal scaling behind a load balancer |
| **Polling consistency** | Breaks on rolling deploy | Session-affinity routing OR shared job store |
| **Background tasks** | FastAPI `BackgroundTasks` (in-process) | Celery, ARQ, or a task queue (runs out-of-process, survives restarts) |
| **LLM client pool** | In-process `_clients` dict | Per-worker singleton, connection pooling |

### Provider management

| Aspect | Demo | Production |
|---|---|---|
| **Timeout** | 30s per LLM call | Tiered: 10s for fast models, 30s for large models, with adaptive fallback |
| **Fallback** | In-process cooldown (60s) | Distributed circuit breaker (circuit state shared across workers via Redis) |
| **Rate limiting** | None (each worker tracks independently) | Shared token bucket per provider per tier |
| **Cost tracking** | Token counts in pipeline_counts | Per-query cost accounting, budget guards, alerts |
| **Model selection** | Fixed model per provider (`gpt-4o-mini`) | Dynamic selection based on query complexity and latency budget |

### Data and correctness

| Aspect | Demo | Production |
|---|---|---|
| **Merge contamination** | Mitigated by cell verifier | Prevented at merge time with cross-entity conflict detection |
| **Geographic filtering** | Ranking-only (soft penalty) | Hard rejection for clear mismatches with geographic containment lookup |
| **Official-site conflicts** | Not detected | Post-resolution conflict resolution (deduplicate domains across rows) |
| **Schema consistency** | Fixed 6-family templates | Versioned schema families; schema changes gated by migration |
| **Evaluation** | Proxy metrics only | Labeled precision/recall on 100+ query benchmark; automated regression |

### Observability and reliability

| Aspect | Demo | Production |
|---|---|---|
| **Logging** | Python logging to stdout | Structured JSON logs → log aggregation (Datadog, Cloudwatch, etc.) |
| **Tracing** | None | Distributed tracing per job (OpenTelemetry) |
| **Metrics** | `pipeline_counts` dict in result JSON | Prometheus metrics on extraction success rate, provider fallback rate, latency p50/p95/p99 |
| **Alerting** | None | Alert when provider fallback rate > threshold, when extraction yield < threshold |
| **Health check** | Simple `/api/health` → `{"status": "ok"}` | Health check that validates DB connectivity, LLM provider reachability, cache availability |
| **Graceful shutdown** | None — in-flight jobs are lost | Drain in-flight jobs before shutdown; mark interrupted jobs as failed with recovery path |

### Scaling

| Aspect | Demo | Production |
|---|---|---|
| **Concurrency** | `max_concurrent_extractions: 3`, `max_concurrent_scrapes: 5` | Dynamic, driven by provider rate limits and queue depth |
| **Extraction budget** | Fixed 10 pages per query | Per-query budget based on query complexity and tier |
| **Gap-fill bounds** | ≤5 entities, ≤2 URLs each, 1 round | Dynamic based on remaining time budget |
| **Cache TTL** | 24h for all pages | Per-domain TTL (official sites: 7 days; editorial: 1 day; directory: 12h) |

---

## 6. Generalization: where domain assumptions crept in and how to fix them

This is the most subtle section. Many choices that feel architectural are actually implicit domain assumptions.

### 6a. The "website" column assumption

**Where it appears**: merger, official_site, field_validator, cell_verifier, ranker, gap_fill — all have a hardcoded list of `_WEBSITE_COLS` (`"website"`, `"url"`, `"homepage"`, `"website_or_repo"`, `"website_or_profile"`, `"site"`).

**Why it's a problem**: this is a reasonable generalization — every entity has some canonical online presence. But for a `person_group` query, the canonical presence might be a LinkedIn profile or an academic publication list. For `software_project`, it's a GitHub repo URL. The column name is different but the concept is the same: "the authoritative URL for this entity."

**How to generalize**: define a `canonical_url_semantics` tag on schema columns at the family level. The official-site resolver, ranker, and merger would query by semantic tag rather than column name. This avoids duplicating the `_WEBSITE_COLS` set across 6 files.

---

### 6b. The location column assumption

**Where it appears**: ranker (`_local_fit` checks `location`, `address`, `headquarters`), verifier (geographic filtering logic), gap_fill (`_COLUMN_QUERY_HINTS` for address/location/headquarters).

**Why it's a problem**: for `place_venue`, the relevant location field is `location`. For `organization_company`, it's `headquarters`. For `person_group`, it's `location`. These are all the same concept ("where is this entity?") but with different column names.

**How to generalize**: define `location_semantics` as a column tag. Any column tagged as `location_semantics` feeds the `local_fit` scorer and geographic filter. No more hardcoded column name lists.

---

### 6c. The evidence regime labels are partly domain-shaped

**The regimes**: `official_site`, `directory_listing`, `editorial_article`, `local_business_listing`, `software_repo_or_docs`, `marketplace_aggregator`, `unknown`.

**Where domain crept in**: `local_business_listing` and `software_repo_or_docs` are specific to two verticals. They're implemented with domain-specific signals:
- `local_business_listing` detects schema.org LocalBusiness markup, Google Maps domain, Yelp URL shapes
- `software_repo_or_docs` detects GitHub/GitLab domains, `/blob/`, `/tree/`, readthedocs patterns

**How to generalize**: replace these two with structural regimes:
- `local_business_listing` → `structured_entity_page` (any page with schema.org entity markup, regardless of type)
- `software_repo_or_docs` → `structured_technical_page` (any page with technical structured metadata: repo hosting, API docs, package registries)

The deterministic extractors can still specialize on GitHub vs. npm vs. PyPI — but the regime label would be regime-structural, not domain-specific.

---

### 6d. The `_FIELD_IMPORTANCE` dict in ranker.py

**What it is**: per-family weights for which columns matter most to the ranking score. E.g., `software_project` → `website_or_repo: 0.30` (highest weight because a repo URL is the most important fact about an open-source project).

**Why it's not domain-specific per se**: the weights encode structural knowledge about what makes a row "good" for each entity kind. A `place_venue` entity without a `location` field is almost certainly wrong or incomplete — that's structural, not vertical-specific.

**Where it could creep**: if you add new column names to a family's template but forget to add them to `_FIELD_IMPORTANCE`, they get zero field importance weight. The system silently downgrades rows that fill those columns.

**How to fix**: derive default field importance from schema column position (column 2 is most important after name, column 6 is least important). Override only when you have a specific reason. This prevents silent omissions.

---

### 6e. The verifier's `_PSEUDO_ENTITY_FAMILY_TERMS`

**What it is**: per-family sets of terms that indicate a name is a category label, not an entity. E.g., for `place_venue`: `{"bars", "cafes", "destinations", "places", "restaurants", "shops", "venues"}`.

**Why it's domain-shaped**: adding "bars" and "restaurants" to the `place_venue` set makes the verifier understand that "Best Bars and Restaurants in NYC" is a category label. But "bars" wouldn't be in the `software_project` set, so `"Top Logging Frameworks and Libraries"` might not be caught.

**How to generalize**: instead of per-family term sets, detect pseudo-entities from query family terms: if the entity name contains plural nouns that belong to the query family's `_PLACE_SIGNALS`, `_ORG_SIGNALS`, or `_SOFTWARE_SIGNALS`, it's a category label, not an entity. The signal lists already exist — reuse them here instead of maintaining a separate per-family set.

---

### 6g. Summary: structural generalizations worth prioritizing

| Current pattern | Files affected | Better approach |
|---|---|---|
| `_WEBSITE_COLS` sets duplicated across 6 files | merger, official_site, verifier, cell_verifier, field_validator, gap_fill | Column semantic tags on schema templates |
| Location column hardcoded to `location`/`address`/`headquarters` | ranker, verifier, gap_fill | Location semantic tag |
| `_PSEUDO_ENTITY_FAMILY_TERMS` duplicates signal lists | verifier, planner | Reuse planner signal lists for pseudo-entity detection |
| `local_business_listing` regime is Yelp/Maps specific | evidence_regimes, deterministic_extractors | Replace with `structured_entity_page` (schema.org-based) |
| `_FIELD_IMPORTANCE` requires manual update per new column | ranker | Derive from column position; override only for known-important columns |
| Gap-fill bounds are hardcoded constants | gap_fill | Per-family or per-query time-budget-driven allocation |

The highest-ROI generalization is column semantic tags. It would remove duplicate `_WEBSITE_COLS` lists from 6 files and make both the location logic and official-site logic self-consistent as the schema templates evolve.
