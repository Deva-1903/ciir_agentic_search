"""
Thin wrapper around the OpenAI-compatible chat completions API.

Supports Groq (primary) and OpenAI (fallback) via base URL switching.

Handles:
- JSON mode responses
- Automatic retries with exponential backoff
- Basic JSON validation before returning
- Fallback JSON extraction from markdown fences
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Optional, Type

from openai import AsyncOpenAI, APIError, RateLimitError, APITimeoutError
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

# ── Client singleton ──────────────────────────────────────────────────────────

_client: Optional[AsyncOpenAI] = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        settings = get_settings()
        kwargs: dict = {
            "api_key": settings.active_api_key,
            "timeout": 60.0,   # hard 60s per call — no silent hangs
            "max_retries": 0,  # retries are handled explicitly in chat_json
        }
        if settings.active_base_url:
            kwargs["base_url"] = settings.active_base_url
        log.info("LLM client: provider=%s  model=%s  base_url=%s",
                 settings.llm_provider, settings.active_model,
                 settings.active_base_url or "(default)")
        _client = AsyncOpenAI(**kwargs)
    return _client


def _extract_json(raw: str) -> dict[str, Any]:
    """Parse JSON from raw LLM output, handling markdown fences."""
    raw = raw.strip()
    # Try direct parse first
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Try extracting from ```json ... ``` fences
    m = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    raise ValueError(f"LLM returned unparseable JSON: {raw[:300]}")


# ── Core call ─────────────────────────────────────────────────────────────────

async def chat_json(
    system: str,
    user: str,
    *,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    timeout: float | None = None,
    attempts: int = 3,
) -> dict[str, Any]:
    """
    Call the LLM in JSON mode. Returns the parsed dict.
    Raises ValueError if the response cannot be parsed as JSON.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    client = _get_client()

    for attempt in range(1, attempts + 1):
        try:
            settings = get_settings()
            response = await client.chat.completions.create(
                model=settings.active_model,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                timeout=timeout,
            )
            raw = response.choices[0].message.content or ""
            try:
                return _extract_json(raw)
            except ValueError as exc:
                log.error("LLM returned invalid JSON: %s …", raw[:200])
                raise ValueError(f"LLM returned invalid JSON: {exc}") from exc
        except APIError as exc:
            log.error("LLM API error: %s", exc)
            is_retryable = isinstance(exc, (RateLimitError, APITimeoutError))
            if not is_retryable or attempt >= attempts:
                raise

            delay = min(30.0, float(2**attempt))
            log.warning(
                "Retrying %s in %.1f seconds as it raised %s: %s.",
                f"{__name__}.chat_json",
                delay,
                exc.__class__.__name__,
                exc,
            )
            await asyncio.sleep(delay)

    raise RuntimeError("unreachable")


async def chat_json_validated(
    system: str,
    user: str,
    model_class: Type[BaseModel],
    *,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    timeout: float | None = None,
    attempts: int = 3,
) -> BaseModel:
    """
    Like chat_json but also validates against a Pydantic model.
    Returns the validated model instance.
    """
    raw = await chat_json(
        system,
        user,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        attempts=attempts,
    )
    return model_class.model_validate(raw)
