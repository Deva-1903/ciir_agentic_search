"""
Search API routes.

POST /api/search          → enqueue a job, return job_id immediately
GET  /api/search/{job_id} → poll job status + result
GET  /api/health          → liveness check
"""

from __future__ import annotations

import asyncio
import json
import time
import traceback
import uuid

from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.core.logging import get_logger
from app.models.db import complete_job, create_job, fail_job, get_job, update_job_phase
from app.models.schema import (
    JobStatus,
    PlannerOutput,
    RequirementSpec,
    SearchMetadata,
    SearchRequest,
    SearchResponse,
)
from app.core.config import get_settings
from app.services.brave_search import run_brave_search
from app.services.cell_verifier import verify_rows_cells
from app.services.extractor import build_candidate_discovery_plan, extract_from_pages
from app.services.gap_fill import run_gap_fill
from app.services.merger import merge_entities
from app.services.official_site import resolve_official_sites
from app.services.planner import plan_schema
from app.services.query_normalizer import normalize_query
from app.services.ranker import prune_rows, rank_rows
from app.services.reranker import rerank_pages
from app.services.requirement_parser import parse_requirements_deterministic
from app.services.requirement_scorer import attach_requirement_summaries
from app.services.scraper import scrape_pages
from app.services.verifier import verify_rows

log = get_logger(__name__)
router = APIRouter()


# ── Pipeline ──────────────────────────────────────────────────────────────────

async def _run_pipeline(job_id: str, query: str) -> None:
    """Full end-to-end pipeline, runs as a background task."""
    t0 = time.monotonic()
    stage_timings_ms: dict[str, float] = {}
    log.info("=== Pipeline START  job=%s  query=%r ===", job_id, query)

    async def _phase(name: str) -> None:
        elapsed = round(time.monotonic() - t0, 1)
        log.info("--- Phase: %s  (+%.1fs) ---", name, elapsed)
        await update_job_phase(job_id, name)

    try:
        normalized = normalize_query(query)
        retrieval_query = normalized.normalized_query or query
        planner_stats: dict[str, int] = {}
        scrape_stats: dict[str, int] = {}
        extraction_stats: dict[str, int] = {}

        # 1. Plan schema
        await _phase("planning")
        stage_start = time.monotonic()
        plan: PlannerOutput = await plan_schema(retrieval_query, stats=planner_stats)
        stage_timings_ms["planning"] = round((time.monotonic() - stage_start) * 1000, 1)

        # 1b. Parse requirements from the query (fast, deterministic — no extra latency)
        requirements: list[RequirementSpec] = parse_requirements_deterministic(retrieval_query)
        if requirements:
            log.info(
                "Requirements parsed: %d — %s",
                len(requirements),
                [r.original_text for r in requirements],
            )

        pipeline_counts: dict[str, int] = {
            "search_angles": len(plan.search_angles),
            "search_facets": len(plan.facets),
            "requirements_parsed": len(requirements),
        }
        log.info(
            "Plan: family=%s entity_type=%r columns=%s facets=%d normalized_query=%r",
            plan.query_family,
            plan.entity_type,
            plan.columns,
            len(plan.facets),
            retrieval_query,
        )

        # 2. Search
        await _phase("searching")
        stage_start = time.monotonic()
        brave_results = await run_brave_search(plan.search_angles)
        stage_timings_ms["searching"] = round((time.monotonic() - stage_start) * 1000, 1)
        urls_considered = len(brave_results)
        pipeline_counts["urls_after_dedupe"] = urls_considered
        log.info("Search: %d URLs to scrape", urls_considered)

        # 3. Scrape
        await _phase("scraping")
        stage_start = time.monotonic()
        scraped_pages = await scrape_pages(brave_results, stats=scrape_stats)
        stage_timings_ms["scraping"] = round((time.monotonic() - stage_start) * 1000, 1)
        pages_scraped = len(scraped_pages)
        pipeline_counts["pages_scraped"] = pages_scraped
        log.info("Scrape: %d/%d pages OK", pages_scraped, urls_considered)

        if pages_scraped == 0:
            raise RuntimeError("No pages could be scraped. Check network / Brave API.")

        # 3.5 Rerank: focus extraction budget on top-K query-relevant pages
        settings = get_settings()
        pages_for_discovery = scraped_pages
        rerank_info: dict = {"scorer": None, "pages_after": pages_scraped}
        if settings.rerank_enabled and pages_scraped > settings.rerank_top_k:
            await _phase("reranking")
            stage_start = time.monotonic()
            pages_for_discovery, rerank_info = await rerank_pages(
                retrieval_query,
                scraped_pages,
                settings.rerank_top_k,
            )
            stage_timings_ms["reranking"] = round((time.monotonic() - stage_start) * 1000, 1)
        else:
            rerank_info = {"scorer": "disabled", "pages_after": pages_scraped}
        pipeline_counts["pages_after_rerank"] = len(pages_for_discovery)

        # 4. Candidate discovery
        await _phase("extracting")
        discovery_plan = build_candidate_discovery_plan(plan)
        log.info(
            "Discovering candidates from %d reranked pages using columns=%s",
            len(pages_for_discovery),
            discovery_plan.columns,
        )
        stage_start = time.monotonic()
        drafts = await extract_from_pages(
            retrieval_query,
            discovery_plan,
            pages_for_discovery,
            mode="discovery",
            stats=extraction_stats,
        )
        stage_timings_ms["extracting"] = round((time.monotonic() - stage_start) * 1000, 1)
        entities_extracted = len(drafts)
        pipeline_counts["extraction_calls"] = extraction_stats.get("llm_calls_attempted", 0)
        pipeline_counts["chunks_extracted"] = extraction_stats.get("chunks_seen", 0)
        pipeline_counts["pages_extracted"] = extraction_stats.get("pages_seen", 0)
        pipeline_counts["pages_with_entities"] = extraction_stats.get("pages_with_entities", 0)
        # provider_fallback_attempts is per-chunk (one page may have 2 chunks).
        # provider_fallback_pages is a coarser but more intuitive per-page count.
        pipeline_counts["provider_fallback_attempts"] = extraction_stats.get("provider_fallback_attempts", 0)
        pipeline_counts["provider_fallback_successes"] = extraction_stats.get("provider_fallback_successes", 0)
        pipeline_counts["provider_skipped_cooldown"] = extraction_stats.get("provider_skipped_cooldown", 0)
        pipeline_counts["entities_before_merge"] = entities_extracted
        pipeline_counts["pages_routed_deterministic"] = extraction_stats.get("pages_routed_deterministic", 0)
        pipeline_counts["pages_routed_hybrid"] = extraction_stats.get("pages_routed_hybrid", 0)
        pipeline_counts["pages_routed_llm"] = extraction_stats.get("pages_routed_llm", 0)
        pipeline_counts["deterministic_entities"] = extraction_stats.get("deterministic_entities", 0)
        log.info("Candidate discovery done: %d extracted candidates", entities_extracted)

        if entities_extracted == 0 and len(pages_for_discovery) >= 3:
            log.error(
                "Extraction returned 0 entities from %d pages (llm_calls=%d fallback_attempts=%d) — this likely "
                "indicates a systemic failure (model misconfiguration, API error, "
                "or prompt incompatibility). Check LLM logs above for errors.",
                len(pages_for_discovery),
                pipeline_counts["extraction_calls"],
                pipeline_counts["provider_fallback_attempts"],
            )

        # 5. Merge candidate rows
        await _phase("merging")
        stage_start = time.monotonic()
        rows = merge_entities(drafts, plan)
        rows_after_merge = len(rows)
        pipeline_counts["rows_after_merge"] = rows_after_merge
        entities_after_merge = rows_after_merge
        pipeline_counts["rows_after_initial_prune"] = rows_after_merge

        rows, official_sites_resolved = resolve_official_sites(rows, scraped_pages)
        stage_timings_ms["merging"] = round((time.monotonic() - stage_start) * 1000, 1)
        pipeline_counts["official_sites_resolved"] = official_sites_resolved
        pipeline_counts["candidate_rows"] = len(rows)
        log.info("Merge done: %d canonical candidate rows", rows_after_merge)

        # 6. Rank candidates before filling
        log.info("Ranking candidate rows…")
        rows = rank_rows(rows, plan, retrieval_query)

        # 7. Focused attribute filling
        await _phase("gap_filling")
        stage_start = time.monotonic()
        rows, gap_fill_used = await run_gap_fill(rows, plan, retrieval_query, stats=extraction_stats)
        stage_timings_ms["gap_filling"] = round((time.monotonic() - stage_start) * 1000, 1)
        pipeline_counts["rows_after_gap_fill"] = len(rows)
        log.info("Gap-fill done: used=%s", gap_fill_used)
        # Cell verification runs after filling so we do not penalize discovery-only rows too early.
        if settings.cell_verifier_enabled:
            rows = verify_rows_cells(rows)

        # Evaluate requirements against each row now that gap-fill has populated fields.
        attach_requirement_summaries(rows, requirements)

        rows = rank_rows(rows, plan, retrieval_query)

        await _phase("verifying")
        stage_start = time.monotonic()
        rows = verify_rows(rows, plan, query)
        stage_timings_ms["verifying"] = round((time.monotonic() - stage_start) * 1000, 1)
        pipeline_counts["rows_after_verifier"] = len(rows)

        rows = prune_rows(rows, plan)
        pipeline_counts["rows_after_final_prune"] = len(rows)
        rows = rank_rows(rows, plan, retrieval_query)
        pipeline_counts["final_rows"] = len(rows)
        pipeline_counts["planner_llm_calls"] = planner_stats.get("llm_calls", 0)
        pipeline_counts["planner_llm_tokens"] = planner_stats.get("llm_total_tokens", 0)
        pipeline_counts["llm_calls_total"] = planner_stats.get("llm_calls", 0) + extraction_stats.get("llm_calls_attempted", 0)
        pipeline_counts["llm_tokens_total"] = planner_stats.get("llm_total_tokens", 0) + extraction_stats.get("llm_total_tokens", 0)

        for key, value in scrape_stats.items():
            pipeline_counts[key] = value
        for key in (
            "deterministic_pages_with_entities",
            "deterministic_entities",
            "pages_routed_deterministic",
            "pages_routed_hybrid",
            "pages_routed_llm",
            "llm_prompt_tokens",
            "llm_completion_tokens",
            "llm_total_tokens",
        ):
            if key in extraction_stats:
                pipeline_counts[key] = extraction_stats[key]

        if entities_extracted > 0 and not rows:
            log.warning(
                "All extracted entities were filtered out before response; counts=%s",
                pipeline_counts,
            )

        duration = round(time.monotonic() - t0, 2)

        response = SearchResponse(
            query_id=job_id,
            query=query,
            entity_type=plan.entity_type,
            columns=plan.columns,
            rows=rows,
            metadata=SearchMetadata(
                original_query=query,
                normalized_query=retrieval_query,
                query_family=plan.query_family,
                search_angles=plan.search_angles,
                facets=plan.facets,
                urls_considered=urls_considered,
                pages_scraped=pages_scraped,
                pages_after_rerank=rerank_info.get("pages_after"),
                rerank_scorer=rerank_info.get("scorer"),
                entities_extracted=entities_extracted,
                entities_after_merge=entities_after_merge,
                gap_fill_used=gap_fill_used,
                duration_seconds=duration,
                pipeline_counts=pipeline_counts,
                pipeline_timings_ms=stage_timings_ms,
                requirements=requirements,
                requirements_parsed=len(requirements),
            ),
        )

        log.info("Pipeline counts: %s", pipeline_counts)
        log.info("Saving result to DB…")
        await complete_job(job_id, response.model_dump())
        log.info("=== Pipeline DONE  job=%s  rows=%d  duration=%.1fs ===",
                 job_id, len(rows), duration)

    except Exception as exc:
        elapsed = round(time.monotonic() - t0, 1)
        log.error("=== Pipeline FAILED  job=%s  phase=see above  elapsed=%.1fs ===",
                  job_id, elapsed)
        log.error("Error: %s", exc)
        log.error("Traceback:\n%s", traceback.format_exc())
        try:
            await fail_job(job_id, str(exc))
        except Exception as db_exc:
            log.error("Also failed to update job status: %s", db_exc)


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/search", response_model=JobStatus, status_code=202)
async def submit_search(
    body: SearchRequest,
    background_tasks: BackgroundTasks,
) -> JobStatus:
    """Enqueue a new search job. Returns job_id for polling."""
    query = body.query.strip()
    if not query:
        raise HTTPException(status_code=422, detail="Query must not be empty")

    job_id = str(uuid.uuid4())
    await create_job(job_id, query)
    background_tasks.add_task(_run_pipeline, job_id, query)
    log.info("Job created: %s  query=%r", job_id, query)

    return JobStatus(job_id=job_id, status="pending", phase="queued")


@router.get("/search/{job_id}", response_model=JobStatus)
async def get_search_status(job_id: str) -> JobStatus:
    """Poll job status. When status='done', result is included."""
    row = await get_job(job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")

    status = row["status"]
    phase = row.get("phase")
    error = row.get("error")

    result = None
    if status == "done" and row.get("result_json"):
        result = SearchResponse(**json.loads(row["result_json"]))

    return JobStatus(
        job_id=job_id,
        status=status,
        phase=phase,
        result=result,
        error=error,
    )


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}
