"""Environment-backed settings, read once. No other module reads os.environ."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Pinned upstream base URLs. Deliberately not configurable: the gateway must never
# call a host derived from request content.
OPENAI_BASE_URL = "https://api.openai.com/v1"
ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_API_VERSION = "2023-06-01"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    database_url: str = "sqlite:///data/gateway.db"

    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None

    redis_url: str | None = None

    lgw_embedding_provider: str | None = None
    lgw_embedding_model: str | None = None

    lgw_connect_timeout_seconds: float = 5.0
    lgw_upstream_timeout_seconds: float = 120.0

    lgw_default_max_output_tokens: int = 1024
    lgw_reservation_ttl_seconds: int = 900

    lgw_cache_similarity_threshold: float = 0.93
    lgw_cache_ttl_hours: int = 24
    lgw_cache_scan_limit: int = 512
    lgw_cache_max_entries_per_key: int = 2000

    lgw_max_body_kb: int = 256
    lgw_log_bodies: bool = False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
