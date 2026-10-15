"""Shared setup for the enrichment command-line tools (``diagnose`` / ``evaluate``)."""

from sqlalchemy.orm import sessionmaker

from app.config import load_config
from app.db import init_db, make_engine, make_session_factory
from app.enrich.service import Enricher
from app.settings import get_settings


def build() -> tuple[sessionmaker, Enricher]:
    from app.pipeline import enricher_from_settings

    settings = get_settings()
    engine = make_engine(settings.database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    config = load_config(settings.config_path)
    return factory, enricher_from_settings(factory, settings, config)
