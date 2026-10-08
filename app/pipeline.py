"""Post-ingestion pipeline: extract article text, then cluster articles into stories."""

import asyncio
import logging

from sqlalchemy.orm import sessionmaker

from app.cluster import Clusterer, TfidfClusterer
from app.cluster.service import ClusterRunResult, run_clustering
from app.extract import Extractor, ExtractRunResult
from app.ingest import USER_AGENT
from app.runs import record_run
from app.settings import Settings

logger = logging.getLogger(__name__)


def clusterer_from_settings(settings: Settings) -> TfidfClusterer:
    return TfidfClusterer(
        threshold=settings.cluster_threshold,
        window_hours=settings.cluster_window_hours,
        same_source_threshold=settings.cluster_same_source_threshold,
        min_shared_tokens=settings.cluster_min_shared_tokens,
    )


def extractor_from_settings(
    session_factory: sessionmaker, settings: Settings, *, user_agent: str = USER_AGENT, **kwargs
) -> Extractor:
    options = {
        "user_agent": user_agent,
        "timeout": settings.extract_timeout_seconds,
        "max_bytes": settings.extract_max_bytes,
        "min_words": settings.extract_min_words,
        "domain_delay": settings.extract_domain_delay_seconds,
        "max_articles": settings.extract_max_articles,
    }
    return Extractor(session_factory, **{**options, **kwargs})


class Pipeline:
    def __init__(
        self, session_factory: sessionmaker, extractor: Extractor, clusterer: Clusterer
    ) -> None:
        self.session_factory = session_factory
        self.extractor = extractor
        self.clusterer = clusterer

    async def extract(self, limit: int | None = None) -> ExtractRunResult:
        result = await self.extractor.run(limit)
        if result.status == "ok":
            self._record("extract", result.model_dump(exclude={"results"}))
        return result

    def cluster(self, *, rebuild: bool = False) -> ClusterRunResult:
        try:
            result = run_clustering(self.session_factory, self.clusterer, rebuild=rebuild)
        except Exception:
            logger.exception("clustering failed")
            return ClusterRunResult(status="error", rebuild=rebuild)
        if result.status == "ok":
            self._record("cluster", result.model_dump(exclude={"joins"}))
        return result

    async def run(self) -> tuple[ExtractRunResult, ClusterRunResult]:
        extracted = await self.extract()
        clustered = await asyncio.to_thread(self.cluster)
        return extracted, clustered

    def run_blocking(self) -> None:
        """Entry point for the scheduler thread."""
        extracted, clustered = asyncio.run(self.run())
        logger.info(
            "pipeline extract=%s cluster=%s",
            extracted.model_dump(exclude={"results"}),
            clustered.model_dump(exclude={"joins"}),
        )

    def _record(self, name: str, result: dict) -> None:
        try:
            record_run(self.session_factory, name, result)
        except Exception:
            logger.exception("could not record %s run", name)
