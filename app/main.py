import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.config import AppConfig, load_config
from app.db import init_db, make_engine, make_session_factory, ping
from app.scheduler import create_scheduler
from app.settings import Settings, get_settings

logger = logging.getLogger("discover_feed")

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")

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


def create_app(settings: Settings | None = None) -> FastAPI:
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
        scheduler = create_scheduler()
        if settings.scheduler_enabled:
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

    @app.get("/health", tags=["meta"])
    def health(request: Request) -> JSONResponse:
        db_ok = ping(request.app.state.engine)
        body = {
            "status": "ok" if db_ok else "degraded",
            "version": __version__,
            "database": "ok" if db_ok else "error",
            "profiles": len(request.app.state.config.profiles),
        }
        return JSONResponse(body, status_code=200 if db_ok else 503)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {"profiles": request.app.state.config.profiles, "version": __version__},
        )

    return app


app = create_app()
