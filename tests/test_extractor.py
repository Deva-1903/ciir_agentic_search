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

    async def fake_extract_from_chunk(query: str, plan: PlannerOutput, page: ScrapedPage, chunk: str):
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
