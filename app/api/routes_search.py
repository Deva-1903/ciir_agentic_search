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
    SearchMetadata,
    SearchRequest,
    SearchResponse,
)
from app.core.config import get_settings
from app.services.brave_search import run_brave_search
from app.services.cell_verifier import verify_rows_cells
from app.services.extractor import extract_from_pages
from app.services.gap_fill import run_gap_fill
from app.services.merger import merge_entities
from app.services.planner import plan_schema
from app.services.ranker import prune_rows, rank_rows
from app.services.reranker import rerank_pages
from app.services.scraper import scrape_pages
from app.services.verifier import verify_rows

log = get_logger(__name__)
router = APIRouter()


# ── Pipeline ──────────────────────────────────────────────────────────────────

async def _run_pipeline(job_id: str, query: str) -> None:
    """Full end-to-end pipeline, runs as a background task."""
    t0 = time.monotonic()
    log.info("=== Pipeline START  job=%s  query=%r ===", job_id, query)

    async def _phase(name: str) -> None:
        elapsed = round(time.monotonic() - t0, 1)
        log.info("--- Phase: %s  (+%.1fs) ---", name, elapsed)
        await update_job_phase(job_id, name)

    try:
        # 1. Plan schema
        await _phase("planning")
        plan: PlannerOutput = await plan_schema(query)
        pipeline_counts: dict[str, int] = {
            "search_angles": len(plan.search_angles),
            "search_facets": len(plan.facets),
        }
        log.info("Plan: entity_type=%r  columns=%s  facets=%d",
                 plan.entity_type, plan.columns, len(plan.facets))

        # 2. Search
        await _phase("searching")
        brave_results = await run_brave_search(plan.search_angles)
        urls_considered = len(brave_results)
        pipeline_counts["urls_after_dedupe"] = urls_considered
        log.info("Search: %d URLs to scrape", urls_considered)

        # 3. Scrape
        await _phase("scraping")
        pages = await scrape_pages(brave_results)
        pages_scraped = len(pages)
        pipeline_counts["pages_scraped"] = pages_scraped
        log.info("Scrape: %d/%d pages OK", pages_scraped, urls_considered)

        if pages_scraped == 0:
            raise RuntimeError("No pages could be scraped. Check network / Brave API.")

        # 3.5 Rerank: focus extraction budget on top-K query-relevant pages
        settings = get_settings()
        rerank_info: dict = {"scorer": None, "pages_after": pages_scraped}
        if settings.rerank_enabled and pages_scraped > settings.rerank_top_k:
            await _phase("reranking")
            pages, rerank_info = await rerank_pages(query, pages, settings.rerank_top_k)
        else:
            rerank_info = {"scorer": None, "pages_after": pages_scraped}
        pipeline_counts["pages_after_rerank"] = len(pages)

        # 4. Extract
        await _phase("extracting")
        log.info("Extracting from %d pages (this is the slow step)…", len(pages))
        extraction_stats: dict[str, int] = {}
        drafts = await extract_from_pages(query, plan, pages, stats=extraction_stats)
        entities_extracted = len(drafts)
        pipeline_counts["extraction_calls"] = extraction_stats.get("llm_calls_attempted", 0)
        pipeline_counts["chunks_extracted"] = extraction_stats.get("chunks_seen", 0)
        pipeline_counts["pages_extracted"] = extraction_stats.get("pages_seen", 0)
        pipeline_counts["pages_with_entities"] = extraction_stats.get("pages_with_entities", 0)
        pipeline_counts["provider_fallback_attempts"] = extraction_stats.get("provider_fallback_attempts", 0)
        pipeline_counts["provider_fallback_successes"] = extraction_stats.get("provider_fallback_successes", 0)
        pipeline_counts["entities_before_merge"] = entities_extracted
        log.info("Extraction done: %d candidate entities", entities_extracted)

        if entities_extracted == 0 and len(pages) >= 3:
            log.error(
                "Extraction returned 0 entities from %d pages (llm_calls=%d fallback_attempts=%d) — this likely "
                "indicates a systemic failure (model misconfiguration, API error, "
                "or prompt incompatibility). Check LLM logs above for errors.",
                len(pages),
                pipeline_counts["extraction_calls"],
                pipeline_counts["provider_fallback_attempts"],
            )

        # 5. Merge
        await _phase("merging")
        rows = merge_entities(drafts, plan)
        rows_after_merge = len(rows)
        pipeline_counts["rows_after_merge"] = rows_after_merge
        rows = prune_rows(rows, plan)
        rows_after_initial_prune = len(rows)
        pipeline_counts["rows_after_initial_prune"] = rows_after_initial_prune
        entities_after_merge = rows_after_merge
        log.info(
            "Merge done: %d canonical rows (%d after initial prune)",
            rows_after_merge,
            rows_after_initial_prune,
        )

        # 5.5 Cell-level verification (name-alignment penalty)
        if settings.cell_verifier_enabled:
            rows = verify_rows_cells(rows)

        # 6. Rank
        log.info("Ranking rows…")
        rows = rank_rows(rows, plan)

        # 7. Gap-fill
        await _phase("gap_filling")
        rows, gap_fill_used = await run_gap_fill(rows, plan, query)
        pipeline_counts["rows_after_gap_fill"] = len(rows)
        log.info("Gap-fill done: used=%s", gap_fill_used)
        # Re-run cell verification because gap-fill adds new cells.
        if settings.cell_verifier_enabled:
            rows = verify_rows_cells(rows)

        await _phase("verifying")
        rows = verify_rows(rows, plan, query)
        pipeline_counts["rows_after_verifier"] = len(rows)

        rows = prune_rows(rows, plan)
        pipeline_counts["rows_after_final_prune"] = len(rows)
        rows = rank_rows(rows, plan)
        pipeline_counts["final_rows"] = len(rows)

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
