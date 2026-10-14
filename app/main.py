import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app.alerts.api import router as alerts_router
from app.alerts.engine import AlertEngine
from app.alerts.repo import seed_alerts
from app.alerts.sender import AlertSender
from app.alerts.web import router as alerts_web_router
from app.api import router as api_router
from app.api_v1 import PREFIX as V1_PREFIX
from app.api_v1 import router as v1_router
from app.auth import AuthConfig, legacy_api_guard, web_guard
from app.chat.api import router as chat_router
from app.chat.service import build_hub
from app.config import AppConfig, Source, load_config
from app.db import init_db, make_engine, make_session_factory, ping
from app.ingest import Ingestor, build_user_agent, sources_requiring_contact
from app.llm.health import check_endpoint
from app.login import router as login_router
from app.models import ProfileRecord
from app.pipeline import (
    Pipeline,
    clusterer_from_settings,
    extractor_from_settings,
    images_from_settings,
    summarize_profile,
    summarizer_role,
    summary_worker_from_settings,
)
from app.profiles import active_sources, seed_from_config, sync_source_flags
from app.publish import migrate
from app.scheduler import (
    create_scheduler,
    schedule_alerts,
    schedule_chat,
    schedule_image_backfill,
    schedule_pipeline,
    schedule_summaries,
    sync_ingestion_jobs,
)
from app.secret_warnings import log_missing_secrets
from app.settings import Settings, get_settings
from app.sources_api import router as sources_router
from app.sources_web import router as sources_web_router
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


_SUCCESSOR = {
    "/api/profiles": "/api/v1/profiles",
    "/api/stories": "/api/v1/stories",
}


class DeprecatedAliases:
    """``Deprecation`` / ``Link: rel=successor-version`` headers on the unversioned read API
    (kept for one release; ``/api/admin`` is not versioned and not deprecated)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        legacy = (
            scope.get("method") == "GET"
            and path.startswith("/api/")
            and not path.startswith((V1_PREFIX + "/", "/api/admin/"))
        )
        if not legacy:
            return await self.app(scope, receive, send)
        successor = next(
            (new + path[len(old) :] for old, new in _SUCCESSOR.items() if path.startswith(old)),
            V1_PREFIX + "/profiles",
        )

        async def with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers += [
                    (b"deprecation", b"true"),
                    (b"link", f'<{successor}>; rel="successor-version"'.encode()),
                ]
                message = {**message, "headers": headers}
            await send(message)

        return await self.app(scope, receive, with_headers)


def create_app(
    settings: Settings | None = None,
    *,
    llm_transport: httpx.AsyncBaseTransport | None = None,
    demo_sink=None,
) -> FastAPI:
    """``llm_transport`` replaces the summarizer's network calls (the demo's fake LLM);
    ``demo_sink`` (an ``app.alerts.sink.FakeSink``) enables ``/demo/sink``."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(level=settings.log_level.upper())
        logger.info("%s", app.state.auth.describe())
        engine = make_engine(settings.database_url)
        init_db(engine)
        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = make_session_factory(engine)
        app.state.config = _load_profiles(settings)
        with app.state.session_factory() as session:
            if seed_from_config(session, app.state.config):
                logger.info("Seeded profiles, sources and topics from %s", settings.config_path)
            sync_source_flags(session, app.state.config)
            if seed_alerts(session, app.state.config):
                logger.info("Seeded alert rules and channels from %s", settings.config_path)
            sources = active_sources(session)
        app.state.ingestor = Ingestor(
            app.state.session_factory,
            timeout=settings.fetch_timeout_seconds,
            user_agent=build_user_agent(settings.contact_email),
            auto_disable_failures=settings.source_auto_disable_failures,
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
        app.state.pipeline.attach_images(
            images_from_settings(
                app.state.session_factory,
                settings,
                app.state.config,
                app.state.pipeline.extractor,
            )
        )
        app.state.alert_sender = AlertSender(app.state.session_factory, app.state.config)
        app.state.alerts = AlertEngine(
            app.state.session_factory,
            app.state.config,
            clusterer=app.state.pipeline.clusterer,
            on_queued=app.state.alert_sender.kick,
        )
        app.state.ingestor.on_new_articles = app.state.alerts.on_ingested
        app.state.pipeline.alerts = app.state.alerts
        app.state.alert_sender.kick()  # anything still queued from before a restart
        migrate(
            app.state.session_factory,
            summarizer=None,
            policy=app.state.summaries.policy,
        )
        app.state.llm_health = await check_endpoint(
            summarizer_role(app.state.config, settings), transport=llm_transport
        )
        app.state.chat = build_hub(
            app.state.session_factory,
            app.state.config,
            summarize_profile(app.state.config, settings),
            transport=llm_transport,
        )
        await app.state.chat.check_all()
        app.state.chat.cleanup_blocking()
        _warn_missing_contact(sources, settings)
        with app.state.session_factory() as session:
            log_missing_secrets(session)
        scheduler = create_scheduler()
        if settings.scheduler_enabled:
            sync_ingestion_jobs(
                scheduler,
                sources,
                app.state.ingestor,
                jitter_seconds=settings.refresh_jitter_seconds,
            )
            schedule_pipeline(
                scheduler,
                app.state.pipeline,
                interval_seconds=settings.pipeline_interval_seconds,
            )
            schedule_image_backfill(
                scheduler,
                app.state.pipeline.images,
                interval_minutes=app.state.config.images.backfill_interval_minutes,
            )
            if app.state.summaries.enabled:
                schedule_summaries(
                    scheduler,
                    app.state.summaries,
                    interval_seconds=settings.summarize_interval_seconds,
                )
            schedule_chat(scheduler, app.state.chat)
            schedule_alerts(scheduler, app.state.alerts, app.state.alert_sender)
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
    app.state.auth = AuthConfig.from_settings(settings)
    app.state.demo_sink = demo_sink
    app.include_router(v1_router)
    app.include_router(alerts_router)
    app.include_router(sources_router)
    app.include_router(chat_router)
    app.include_router(api_router, dependencies=[Depends(legacy_api_guard)])
    app.include_router(login_router)
    app.include_router(web_router, dependencies=[Depends(web_guard)])
    app.include_router(alerts_web_router, dependencies=[Depends(web_guard)])
    app.include_router(sources_web_router, dependencies=[Depends(web_guard)])
    for route in api_router.routes:
        if "GET" in getattr(route, "methods", ()) and "/admin/" not in route.path:
            route.deprecated = True
    app.add_middleware(DeprecatedAliases)

    @app.get(V1_PREFIX + "/openapi.json", include_in_schema=False)
    def openapi_v1(request: Request) -> JSONResponse:
        from app.openapi import v1_schema

        return JSONResponse(v1_schema(request.app))

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
