"""Tests for provider routing config and LLM client dispatch."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.config import Settings


# ── Config routing ────────────────────────────────────────────────────────────


def _make_settings(**overrides) -> Settings:
    defaults = dict(
        brave_api_key="test",
        openai_api_key="oai-key",
        openai_model="gpt-4o-mini",
        groq_api_key="groq-key",
        groq_model="llama-3.3-70b-versatile",
        groq_base_url="https://api.groq.com/openai/v1",
        planner_provider="openai",
        extractor_provider="groq",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_provider_config_returns_openai():
    s = _make_settings()
    api_key, model, base_url = s.provider_config("openai")
    assert api_key == "oai-key"
    assert model == "gpt-4o-mini"
    assert base_url is None  # default OpenAI URL


def test_provider_config_returns_groq():
    s = _make_settings()
    api_key, model, base_url = s.provider_config("groq")
    assert api_key == "groq-key"
    assert model == "llama-3.3-70b-versatile"
    assert base_url == "https://api.groq.com/openai/v1"


def test_default_routing_is_openai_planner_groq_extractor():
    s = _make_settings()
    assert s.planner_provider == "openai"
    assert s.extractor_provider == "groq"


def test_routing_can_be_overridden():
    s = _make_settings(planner_provider="groq", extractor_provider="openai")
    assert s.planner_provider == "groq"
    assert s.extractor_provider == "openai"


def test_legacy_active_properties_still_work():
    s = _make_settings()
    assert s.llm_provider == "groq"
    assert s.active_api_key == "groq-key"
    assert s.active_model == "llama-3.3-70b-versatile"


def test_legacy_fallback_when_no_groq_key():
    s = _make_settings(groq_api_key="")
    assert s.llm_provider == "openai"
    assert s.active_api_key == "oai-key"
    assert s.active_model == "gpt-4o-mini"


# ── Planner uses planner_provider ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_planner_passes_planner_provider(monkeypatch):
    """planner.plan_schema() should pass provider=settings.planner_provider."""
    from app.services import planner

    captured_kwargs: dict = {}

    async def fake_chat_json_validated(system, user, model_class, **kwargs):
        captured_kwargs.update(kwargs)
        return model_class(
            entity_type="tool",
            facets=[
                {
                    "type": "entity_list",
                    "query": "best developer tools",
                    "expected_fill_columns": ["name", "website"],
                    "rationale": "discover candidates",
                }
            ],
        )

    monkeypatch.setattr(planner, "chat_json_validated", fake_chat_json_validated)
    monkeypatch.setattr(
        planner,
        "get_settings",
        lambda: SimpleNamespace(planner_provider="openai"),
    )
    await planner.plan_schema("test query")
    assert captured_kwargs.get("provider") == "openai"


# ── Extractor uses extractor_provider ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_extractor_passes_extractor_provider(monkeypatch):
    """extractor._extract_from_chunk() should pass provider=settings.extractor_provider."""
    from app.models.schema import PlannerOutput, ScrapedPage
    from app.services import extractor

    captured_kwargs: dict = {}

    async def fake_chat_json(system, user, **kwargs):
        captured_kwargs.update(kwargs)
        return {"entities": []}

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

    plan = PlannerOutput(entity_type="tool", columns=["name"], search_angles=["q"])
    page = ScrapedPage(url="https://example.com", title="Ex", cleaned_text="text")
    await extractor._extract_from_chunk("test", plan, page, "chunk")

    assert captured_kwargs.get("provider") == "groq"
