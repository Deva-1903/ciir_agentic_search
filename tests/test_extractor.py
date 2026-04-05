"""Tests for extractor concurrency and LLM call settings."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.models.schema import PlannerOutput, ScrapedPage
from app.services import extractor


@pytest.mark.asyncio
async def test_extract_from_chunk_uses_extraction_timeout_settings(monkeypatch):
    captured: dict[str, float | int] = {}

    async def fake_chat_json(system: str, user: str, **kwargs):
        captured.update(kwargs)
        return {"entities": []}

    monkeypatch.setattr(extractor, "chat_json", fake_chat_json)
    monkeypatch.setattr(
        extractor,
        "get_settings",
        lambda: SimpleNamespace(
            extract_llm_timeout_seconds=12.5,
            extract_llm_max_attempts=1,
            extractor_provider="groq",
        ),
    )

    plan = PlannerOutput(entity_type="startup", columns=["name"], search_angles=["query"])
    page = ScrapedPage(url="https://example.com", title="Example", cleaned_text="content")

    drafts = await extractor._extract_from_chunk("query", plan, page, "chunk text")

    assert drafts == []
    assert captured["timeout"] == 12.5
    assert captured["attempts"] == 1


@pytest.mark.asyncio
async def test_extract_from_chunk_backfills_name_cell(monkeypatch):
    async def fake_chat_json(system: str, user: str, **kwargs):
        return {
            "entities": [
                {
                    "entity_name": "Lucali",
                    "cells": {
                        "address": {
                            "value": "575 Henry St, Brooklyn, NY 11231",
                            "evidence_snippet": "575 Henry St, Brooklyn, NY 11231",
                            "confidence": 1.0,
                        }
                    },
                }
            ]
        }

    monkeypatch.setattr(extractor, "chat_json", fake_chat_json)
    monkeypatch.setattr(
        extractor,
        "get_settings",
        lambda: SimpleNamespace(
            extract_llm_timeout_seconds=12.5,
            extract_llm_max_attempts=1,
            extractor_provider="groq",
        ),
    )

    plan = PlannerOutput(entity_type="restaurant", columns=["name", "address"], search_angles=["query"])
    page = ScrapedPage(url="https://example.com", title="Example", cleaned_text="content")

    drafts = await extractor._extract_from_chunk("query", plan, page, "chunk text")

    assert len(drafts) == 1
    assert drafts[0].cells["name"].value == "Lucali"
    assert drafts[0].cells["name"].confidence == 0.75


@pytest.mark.asyncio
async def test_extract_from_pages_limits_global_llm_concurrency(monkeypatch):
    monkeypatch.setattr(
        extractor,
        "get_settings",
        lambda: SimpleNamespace(
            chunk_token_limit=500,
            max_chunks_per_page=2,
            max_concurrent_extractions=2,
            extract_llm_timeout_seconds=30.0,
            extract_llm_max_attempts=1,
        ),
    )
    monkeypatch.setattr(
        extractor,
        "chunk_text",
        lambda text, max_tokens, max_chunks=None: ["chunk-a", "chunk-b"],
    )

    active = 0
    peak = 0

    async def fake_extract_from_chunk(query: str, plan: PlannerOutput, page: ScrapedPage, chunk: str, mode="fill", stats=None):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return []

    monkeypatch.setattr(extractor, "_extract_from_chunk", fake_extract_from_chunk)

    plan = PlannerOutput(entity_type="startup", columns=["name"], search_angles=["query"])
    pages = [
        ScrapedPage(url=f"https://example.com/{i}", title=f"Page {i}", cleaned_text="content")
        for i in range(3)
    ]

    drafts = await extractor.extract_from_pages("query", plan, pages)

    assert drafts == []
    assert peak == 2


@pytest.mark.asyncio
async def test_extract_from_chunk_preserves_multiple_entities(monkeypatch):
    """Regression test: extractor must preserve ALL entities from LLM response,
    not just the first one."""
    async def fake_chat_json(system: str, user: str, **kwargs):
        return {
            "entities": [
                {
                    "entity_name": "Startup Alpha",
                    "cells": {
                        "name": {"value": "Startup Alpha", "evidence_snippet": "Startup Alpha is a...", "confidence": 0.9},
                        "website": {"value": "https://alpha.com", "evidence_snippet": "alpha.com", "confidence": 0.8},
                    },
                },
                {
                    "entity_name": "Startup Beta",
                    "cells": {
                        "name": {"value": "Startup Beta", "evidence_snippet": "Startup Beta focuses...", "confidence": 0.85},
                        "website": {"value": "https://beta.io", "evidence_snippet": "beta.io", "confidence": 0.75},
                    },
                },
                {
                    "entity_name": "Startup Gamma",
                    "cells": {
                        "name": {"value": "Startup Gamma", "evidence_snippet": "Gamma was founded...", "confidence": 0.8},
                    },
                },
            ]
        }

    monkeypatch.setattr(extractor, "chat_json", fake_chat_json)
    monkeypatch.setattr(
        extractor,
        "get_settings",
        lambda: SimpleNamespace(
            extract_llm_timeout_seconds=30.0,
            extract_llm_max_attempts=1,
            extractor_provider="groq",
        ),
    )

    plan = PlannerOutput(entity_type="startup", columns=["name", "website"])
    page = ScrapedPage(url="https://example.com/list", title="Top Startups", cleaned_text="content")

    drafts = await extractor._extract_from_chunk("AI startups in healthcare", plan, page, "chunk text")

    assert len(drafts) == 3, f"Expected 3 entities, got {len(drafts)}"
    names = {d.entity_name for d in drafts}
    assert names == {"Startup Alpha", "Startup Beta", "Startup Gamma"}


@pytest.mark.asyncio
async def test_extract_from_chunk_falls_back_to_secondary_provider(monkeypatch):
    providers_seen: list[str] = []

    async def fake_chat_json(system: str, user: str, **kwargs):
        provider = kwargs["provider"]
        providers_seen.append(provider)
        if provider == "groq":
            raise RuntimeError("rate_limit_exceeded")
        return {
            "entities": [
                {
                    "entity_name": "Lucali",
                    "cells": {
                        "address": {
                            "value": "575 Henry St, Brooklyn, NY 11231",
                            "evidence_snippet": "575 Henry St, Brooklyn, NY 11231",
                            "confidence": 1.0,
                        }
                    },
                }
            ]
        }

    monkeypatch.setattr(extractor, "chat_json", fake_chat_json)
    monkeypatch.setattr(
        extractor,
        "get_settings",
        lambda: SimpleNamespace(
            extract_llm_timeout_seconds=30.0,
            extract_llm_max_attempts=1,
            extractor_provider="groq",
            provider_config=lambda provider: (
                "groq-key" if provider == "groq" else "openai-key",
                "model",
                None,
            ),
        ),
    )

    plan = PlannerOutput(entity_type="restaurant", columns=["name", "address"])
    page = ScrapedPage(url="https://example.com/list", title="Top Pizza", cleaned_text="content")
    stats: dict[str, int] = {}

    drafts = await extractor._extract_from_chunk(
        "top pizza places in Brooklyn",
        plan,
        page,
        "chunk text",
        stats=stats,
    )

    assert providers_seen == ["groq", "openai"]
    assert len(drafts) == 1
    assert drafts[0].entity_name == "Lucali"
    assert stats["llm_calls_attempted"] == 2
    assert stats["provider_fallback_attempts"] == 1
    assert stats["provider_fallback_successes"] == 1


@pytest.mark.asyncio
async def test_extract_from_pages_accumulates_across_pages(monkeypatch):
    """Regression test: entities from multiple pages must be accumulated, not overwritten."""
    call_count = 0

    async def fake_extract_from_chunk(query, plan, page, chunk, mode="fill", stats=None):
        nonlocal call_count
        call_count += 1
        return [
            extractor.EntityDraft(
                entity_name=f"Entity from {page.title}",
                cells={"name": extractor.CellDraft(value=f"Entity from {page.title}", evidence_snippet="...", confidence=0.9)},
                source_url=page.url,
                source_title=page.title,
            )
        ]

    monkeypatch.setattr(extractor, "_extract_from_chunk", fake_extract_from_chunk)
    monkeypatch.setattr(
        extractor,
        "get_settings",
        lambda: SimpleNamespace(
            chunk_token_limit=50000,
            max_chunks_per_page=1,
            max_concurrent_extractions=5,
        ),
    )
    monkeypatch.setattr(extractor, "chunk_text", lambda text, max_tokens, max_chunks=None: ["single_chunk"])

    plan = PlannerOutput(entity_type="startup", columns=["name"])
    pages = [
        ScrapedPage(url=f"https://example.com/{i}", title=f"Page {i}", cleaned_text="content")
        for i in range(4)
    ]

    drafts = await extractor.extract_from_pages("query", plan, pages)
    assert len(drafts) == 4, f"Expected 4 entities (one per page), got {len(drafts)}"


def test_build_candidate_discovery_plan_prefers_lightweight_columns():
    plan = PlannerOutput(
        query_family="local_business",
        entity_type="pizza place",
        columns=["name", "website", "address", "phone_number", "category", "rating"],
        search_angles=["top pizza places in Brooklyn"],
    )

    discovery_plan = extractor.build_candidate_discovery_plan(plan)

    assert discovery_plan.columns == ["name", "website", "address", "phone_number", "category", "rating"]
