"""Application configuration loaded from environment / .env file."""

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # External APIs
    brave_api_key: str = ""
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_base_url: Optional[str] = None  # None → use default OpenAI URL

    # App
    app_env: str = "development"
    log_level: str = "INFO"

    # Pipeline tuning
    max_results_per_angle: int = 5
    max_concurrent_scrapes: int = 5
    max_concurrent_extractions: int = 3
    scrape_timeout: int = 15
    max_chunks_per_page: int = 2       # LLM extraction chunks per long page
    chunk_token_limit: int = 3000      # Tokens per extraction chunk
    extract_llm_timeout_seconds: float = 30.0
    extract_llm_max_attempts: int = 1
    cache_ttl_hours: int = 24

    # Gap-fill bounds
    gap_fill_max_entities: int = 3
    gap_fill_max_urls_per_entity: int = 2

    # DB — lives outside the project root so --reload doesn't detect SQLite
    # writes as file changes and restart the worker mid-pipeline
    db_path: str = "/tmp/agentic_search.db"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
