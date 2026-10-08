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


@lru_cache
def get_settings() -> Settings:
    return Settings()
