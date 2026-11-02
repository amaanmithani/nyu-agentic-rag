"""All runtime configuration, read from environment variables (see .env.example)."""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://rag:change-me@localhost:5432/rag"
    redis_url: str = "redis://localhost:6379/0"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"
    llm_provider: str = "anthropic"  # "anthropic" | "stub"

    embedding_model: str = "BAAI/bge-small-en-v1.5"
    rerank_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    upload_dir: str = "/data/uploads"

    # Agent safety limits
    max_steps: int = 8
    tool_timeout_s: float = 5.0
    tool_max_retries: int = 2
    max_tokens_per_run: int = 60_000
    run_deadline_s: float = 60.0
    fault_inject: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
