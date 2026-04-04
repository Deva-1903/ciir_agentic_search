"""SQLite database layer using aiosqlite.

Two tables:
  - scraped_pages  : URL-keyed cache of fetched and cleaned page content.
  - query_jobs     : Records of pipeline runs (status, result JSON).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

_DB_PATH: Optional[str] = None


def _db_path() -> str:
    global _DB_PATH
    if _DB_PATH is None:
        settings = get_settings()
        _DB_PATH = settings.db_path
        os.makedirs(os.path.dirname(_DB_PATH) or ".", exist_ok=True)
    return _DB_PATH


# ── Schema init ───────────────────────────────────────────────────────────────

async def init_db() -> None:
    """Create tables if they don't exist."""
    path = _db_path()
    async with aiosqlite.connect(path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS scraped_pages (
                url           TEXT PRIMARY KEY,
                title         TEXT,
                cleaned_text  TEXT,
                scraped_at    TEXT,
                status        TEXT DEFAULT 'ok'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS query_jobs (
                job_id        TEXT PRIMARY KEY,
                query         TEXT,
                status        TEXT,
                phase         TEXT,
                result_json   TEXT,
                error         TEXT,
                created_at    TEXT,
                completed_at  TEXT
            )
        """)
        await db.commit()

        # Mark any jobs that were running when the server last died as failed
        await db.execute(
            "UPDATE query_jobs SET status='failed', error='Server restarted while job was running' "
            "WHERE status IN ('running', 'pending')"
        )
        await db.commit()
    log.info("Database initialised at %s", path)


# ── Scraped pages cache ────────────────────────────────────────────────────────

async def get_cached_page(url: str) -> Optional[dict]:
    """Return cached page if still fresh, else None."""
    settings = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.cache_ttl_hours)
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM scraped_pages WHERE url = ? AND scraped_at > ?",
            (url, cutoff.isoformat()),
        ) as cur:
            row = await cur.fetchone()
    if row:
        return dict(row)
    return None


async def save_cached_page(url: str, title: str, cleaned_text: str) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """
            INSERT INTO scraped_pages (url, title, cleaned_text, scraped_at, status)
            VALUES (?, ?, ?, ?, 'ok')
            ON CONFLICT(url) DO UPDATE SET
                title = excluded.title,
                cleaned_text = excluded.cleaned_text,
                scraped_at = excluded.scraped_at
            """,
            (url, title, cleaned_text, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


# ── Query jobs ─────────────────────────────────────────────────────────────────

async def create_job(job_id: str, query: str) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            "INSERT INTO query_jobs (job_id, query, status, created_at) VALUES (?, ?, 'pending', ?)",
            (job_id, query, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()


async def update_job_phase(job_id: str, phase: str) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            "UPDATE query_jobs SET status = 'running', phase = ? WHERE job_id = ?",
            (phase, job_id),
        )
        await db.commit()


async def complete_job(job_id: str, result: dict) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            """
            UPDATE query_jobs
            SET status = 'done', result_json = ?, completed_at = ?, phase = 'done'
            WHERE job_id = ?
            """,
            (json.dumps(result), datetime.now(timezone.utc).isoformat(), job_id),
        )
        await db.commit()


async def fail_job(job_id: str, error: str) -> None:
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute(
            "UPDATE query_jobs SET status = 'failed', error = ?, completed_at = ? WHERE job_id = ?",
            (error, datetime.now(timezone.utc).isoformat(), job_id),
        )
        await db.commit()


async def get_job(job_id: str) -> Optional[dict]:
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM query_jobs WHERE job_id = ?", (job_id,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None
