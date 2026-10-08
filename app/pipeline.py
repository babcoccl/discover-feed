"""Post-ingestion pipeline: extract article text, then cluster articles into stories."""

import asyncio
import logging

import httpx
from sqlalchemy.orm import sessionmaker

from app.cluster import Clusterer, TfidfClusterer
from app.cluster.service import ClusterRunResult, run_clustering
from app.config import AppConfig, LLMRole
from app.extract import Extractor, ExtractRunResult
from app.ingest import USER_AGENT
from app.llm import LLMClient
from app.runs import record_run
from app.settings import Settings
from app.summarize.service import Summarizer
from app.summarize.worker import SummaryWorker

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


def summarizer_role(config: AppConfig, settings: Settings) -> LLMRole | None:
    """The ``llm.summarizer`` of DISCOVER_SUMMARIZE_PROFILE, else of the first profile."""
    if not settings.summarize_enabled or not config.profiles:
        return None
    if settings.summarize_profile:
        profile = config.get_profile(settings.summarize_profile)
        if profile is None:
            raise ValueError(
                f"DISCOVER_SUMMARIZE_PROFILE: unknown profile {settings.summarize_profile!r}"
            )
        return profile.llm.summarizer
    return config.profiles[0].llm.summarizer


def summary_worker_from_settings(
    session_factory: sessionmaker,
    settings: Settings,
    config: AppConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SummaryWorker:
    """``transport`` replaces the network (the fake LLM in tests and the demo)."""
    role = summarizer_role(config, settings)
    summarizer = None
    if role is not None:
        summarizer = Summarizer(
            LLMClient(role, transport=transport),
            max_articles=settings.summarize_max_articles_per_story,
            max_words=settings.summarize_max_words_per_article,
        )
    return SummaryWorker(
        session_factory,
        summarizer,
        concurrency=settings.summarize_concurrency,
        max_per_run=settings.summarize_max_per_run,
        debounce_minutes=settings.summarize_resummarize_min_minutes,
        failure_limit=settings.summarize_failure_limit,
        pause_minutes=settings.summarize_pause_minutes,
    )


class Pipeline:
    def __init__(
        self,
        session_factory: sessionmaker,
        extractor: Extractor,
        clusterer: Clusterer,
        summaries: SummaryWorker | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.extractor = extractor
        self.clusterer = clusterer
        self.summaries = summaries

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
            self._record("cluster", result.model_dump(exclude={"joins", "story_ids"}))
            self._queue_summaries(result)
        return result

    def _queue_summaries(self, result: ClusterRunResult) -> None:
        if self.summaries is None or not self.summaries.enabled:
            return
        try:
            if result.rebuild:
                self.summaries.enqueue_all()
            else:
                self.summaries.enqueue(result.story_ids)
        except Exception:
            logger.exception("could not queue stories for summaries")

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
            clustered.model_dump(exclude={"joins", "story_ids"}),
        )

    def _record(self, name: str, result: dict) -> None:
        try:
            record_run(self.session_factory, name, result)
        except Exception:
            logger.exception("could not record %s run", name)
