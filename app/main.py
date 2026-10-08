import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app.api import router as api_router
from app.config import AppConfig, Source, load_config
from app.db import init_db, make_engine, make_session_factory, ping
from app.ingest import Ingestor, build_user_agent, sources_requiring_contact
from app.models import ProfileRecord
from app.pipeline import (
    Pipeline,
    clusterer_from_settings,
    extractor_from_settings,
    summary_worker_from_settings,
)
from app.profiles import active_sources, seed_from_config
from app.scheduler import (
    create_scheduler,
    schedule_ingestion,
    schedule_pipeline,
    schedule_summaries,
)
from app.settings import Settings, get_settings
from app.web import router as web_router

logger = logging.getLogger("discover_feed")

try:
    __version__ = version("discover-feed")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0"


def _load_profiles(settings: Settings) -> AppConfig:
    path = settings.config_path
    if not path.exists():
        logger.warning("Config file %s not found; starting with no profiles", path)
        return AppConfig()
    config = load_config(path)
    logger.info("Loaded %d profile(s) from %s", len(config.profiles), path)
    return config


def _warn_missing_contact(sources: list[Source], settings: Settings) -> None:
    if settings.contact_email:
        return
    for source in sources_requiring_contact(sources):
        logger.warning(
            "Source %r (%s) expects a contact in the User-Agent and may block requests "
            "without one; set DISCOVER_CONTACT_EMAIL",
            source.id,
            source.url.host,
        )


def create_app(
    settings: Settings | None = None, *, llm_transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    """``llm_transport`` replaces the summarizer's network calls (the demo's fake LLM)."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(level=settings.log_level.upper())
        engine = make_engine(settings.database_url)
        init_db(engine)
        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = make_session_factory(engine)
        app.state.config = _load_profiles(settings)
        with app.state.session_factory() as session:
            if seed_from_config(session, app.state.config):
                logger.info("Seeded profiles, sources and topics from %s", settings.config_path)
            sources = active_sources(session)
        app.state.ingestor = Ingestor(
            app.state.session_factory,
            timeout=settings.fetch_timeout_seconds,
            user_agent=build_user_agent(settings.contact_email),
        )
        app.state.pipeline = Pipeline(
            app.state.session_factory,
            extractor_from_settings(
                app.state.session_factory,
                settings,
                user_agent=build_user_agent(settings.contact_email),
            ),
            clusterer_from_settings(settings),
            summary_worker_from_settings(
                app.state.session_factory, settings, app.state.config, transport=llm_transport
            ),
        )
        app.state.summaries = app.state.pipeline.summaries
        if app.state.summaries.enabled:
            app.state.summaries.enqueue_missing()
        _warn_missing_contact(sources, settings)
        scheduler = create_scheduler()
        if settings.scheduler_enabled:
            schedule_ingestion(
                scheduler,
                sources,
                app.state.ingestor,
                jitter_seconds=settings.refresh_jitter_seconds,
            )
            schedule_pipeline(
                scheduler,
                app.state.pipeline,
                interval_minutes=settings.extract_interval_minutes,
            )
            if app.state.summaries.enabled:
                schedule_summaries(
                    scheduler,
                    app.state.summaries,
                    interval_minutes=settings.summarize_interval_minutes,
                )
            scheduler.start()
        app.state.scheduler = scheduler
        try:
            yield
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)
            engine.dispose()

    app = FastAPI(
        title="Discover Feed",
        version=__version__,
        description="Personal Discover-style news-feed monitor.",
        lifespan=lifespan,
    )
    app.include_router(api_router)
    app.include_router(web_router)

    @app.get("/health", tags=["meta"])
    def health(request: Request) -> JSONResponse:
        db_ok = ping(request.app.state.engine)
        profiles = 0
        if db_ok:
            with request.app.state.session_factory() as session:
                profiles = session.scalar(select(func.count()).select_from(ProfileRecord))
        body = {
            "status": "ok" if db_ok else "degraded",
            "version": __version__,
            "database": "ok" if db_ok else "error",
            "profiles": profiles,
        }
        return JSONResponse(body, status_code=200 if db_ok else 503)

    return app


app = create_app()
