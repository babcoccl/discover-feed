from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-level settings, read from environment variables prefixed with ``DISCOVER_``."""

    model_config = SettingsConfigDict(env_prefix="DISCOVER_", env_file=".env", extra="ignore")

    config_path: Path = Path("config/profiles.yaml")
    database_url: str = "sqlite:///./data/discover.db"
    scheduler_enabled: bool = True
    fetch_timeout_seconds: float = 10.0
    refresh_jitter_seconds: int = 60
    contact_email: str | None = None
    log_level: str = "info"

    # Text extraction (app/extract.py)
    extract_interval_minutes: int = 15
    extract_max_articles: int = 50
    extract_timeout_seconds: float = 10.0
    extract_max_bytes: int = 2 * 1024 * 1024
    extract_min_words: int = 80
    extract_domain_delay_seconds: float = 2.0

    # Story clustering (app/cluster); see README "Tuning clustering"
    cluster_threshold: float = 0.45
    cluster_window_hours: float = 72
    cluster_same_source_threshold: float = 0.7
    cluster_min_shared_tokens: int = 2


@lru_cache
def get_settings() -> Settings:
    return Settings()
