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

    # Groq (fast extraction via OpenAI-compatible API)
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    groq_base_url: str = "https://api.groq.com/openai/v1"

    # Provider routing — which provider each pipeline stage uses
    planner_provider: str = "openai"
    extractor_provider: str = "groq"

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

    # Reranker (cross-encoder before extraction)
    rerank_enabled: bool = True
    rerank_top_k: int = 10  # extract only from top-K reranked pages

    # Ablation toggles
    cell_verifier_enabled: bool = True
    source_diversity_weight: float = 0.08

    # DB — lives outside the project root so --reload doesn't detect SQLite
    # writes as file changes and restart the worker mid-pipeline
    db_path: str = "/tmp/agentic_search.db"

    @property
    def llm_provider(self) -> str:
        """Return 'groq' if GROQ_API_KEY is set, else 'openai'."""
        return "groq" if self.groq_api_key else "openai"

    @property
    def active_api_key(self) -> str:
        return self.groq_api_key if self.groq_api_key else self.openai_api_key

    @property
    def active_model(self) -> str:
        return self.groq_model if self.groq_api_key else self.openai_model

    @property
    def active_base_url(self) -> str | None:
        if self.groq_api_key:
            return self.groq_base_url
        return self.openai_base_url

    def provider_config(self, provider: str) -> tuple[str, str, str | None]:
        """Return (api_key, model, base_url) for the named provider."""
        if provider == "groq":
            return self.groq_api_key, self.groq_model, self.groq_base_url
        return self.openai_api_key, self.openai_model, self.openai_base_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
