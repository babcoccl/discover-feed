"""Post-ingestion pipeline: extract article text, then cluster articles into stories."""

import asyncio
import logging
from datetime import timedelta

import httpx
from sqlalchemy.orm import sessionmaker

from app.cluster import Clusterer, TfidfClusterer
from app.cluster.service import ClusterRunResult, run_clustering
from app.config import AppConfig, LLMRole, Profile, SummariesConfig
from app.extract import Extractor, ExtractRunResult
from app.ingest import USER_AGENT
from app.llm import LLMClient
from app.llm.client import set_max_inflight
from app.publish import Policy
from app.runs import record_run
from app.settings import Settings
from app.summarize.service import Summarizer, summarizer_options
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


def summarize_profile(config: AppConfig, settings: Settings) -> Profile | None:
    """DISCOVER_SUMMARIZE_PROFILE, else the first profile (its ``llm.summarizer`` and
    ``summaries`` settings drive the summary worker)."""
    if not settings.summarize_enabled or not config.profiles:
        return None
    if settings.summarize_profile:
        profile = config.get_profile(settings.summarize_profile)
        if profile is None:
            raise ValueError(
                f"DISCOVER_SUMMARIZE_PROFILE: unknown profile {settings.summarize_profile!r}"
            )
        return profile
    return config.profiles[0]


def summarizer_role(config: AppConfig, settings: Settings) -> LLMRole | None:
    profile = summarize_profile(config, settings)
    return profile.llm.summarizer if profile else None


def summary_worker_from_settings(
    session_factory: sessionmaker,
    settings: Settings,
    config: AppConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SummaryWorker:
    """``transport`` replaces the network (the fake LLM in tests and the demo)."""
    profile = summarize_profile(config, settings)
    options = profile.summaries if profile else SummariesConfig()
    summarizer = None
    if profile is not None:
        summarizer = Summarizer(
            LLMClient(profile.llm.summarizer, transport=transport),
            max_articles=settings.summarize_max_articles_per_story,
            max_words=settings.summarize_max_words_per_article,
            **summarizer_options(options),
        )
    if summarizer is not None:
        set_max_inflight(options.max_inflight)
    return SummaryWorker(
        session_factory,
        summarizer,
        max_per_run=settings.summarize_max_per_run,
        failure_limit=settings.summarize_failure_limit,
        pause_minutes=settings.summarize_pause_minutes,
        policy=Policy.from_config(options, settings.report_mode),
        backfill_hours=options.summarize_backfill_hours,
        backfill_max=options.summarize_backfill_max,
        followups=bool(profile and profile.llm.chat and options.followups),
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

    @property
    def extraction_wait(self) -> timedelta:
        policy = self.summaries.policy if self.summaries is not None else Policy()
        return policy.extraction_wait

    def cluster(self, *, rebuild: bool = False) -> ClusterRunResult:
        """Stories whose text is final (extracted, failed, or ``extraction_wait_seconds``
        passed) join an existing story or start their own."""
        try:
            result = run_clustering(
                self.session_factory,
                self.clusterer,
                rebuild=rebuild,
                extraction_wait=self.extraction_wait,
            )
        except Exception:
            logger.exception("clustering failed")
            return ClusterRunResult(status="error", rebuild=rebuild)
        if result.status == "ok":
            self._record("cluster", result.model_dump(exclude={"joins", "story_ids"}))
            self._queue_summaries(result)
        return result

    def _queue_summaries(self, result: ClusterRunResult) -> None:
        """Kick the generation worker (it derives what to do from story states)."""
        if self.summaries is None or not self.summaries.enabled:
            return
        try:
            if result.rebuild:  # new story ids: regenerate all (cached input -> no LLM call)
                self.summaries.enqueue_all()
            if result.story_ids or result.rebuild:
                self.summaries.start()
        except Exception:
            logger.exception("could not start the summary worker")

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
