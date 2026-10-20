"""Shared setup for the perspectives command-line tools (``diagnose`` / ``evaluate``)."""

from sqlalchemy.orm import sessionmaker

from app.config import load_config
from app.db import init_db, make_engine, make_session_factory
from app.perspectives.service import PerspectiveService
from app.settings import get_settings


def build() -> tuple[sessionmaker, PerspectiveService | None]:
    from app.pipeline import perspectives_from_profile, summarize_profile

    settings = get_settings()
    engine = make_engine(settings.database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    config = load_config(settings.config_path)
    profile = summarize_profile(config, settings)
    return factory, perspectives_from_profile(factory, config, profile)
