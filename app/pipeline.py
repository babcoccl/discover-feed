"""Post-ingestion pipeline: extract article text, then cluster articles into stories."""

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

import httpx
from sqlalchemy.orm import sessionmaker

from app import images, telemetry
from app.cluster import Clusterer, TfidfClusterer
from app.cluster.service import ClusterRunResult, run_clustering
from app.config import AppConfig, ContextConfig, EnrichmentConfig, LLMRole, Profile, SummariesConfig
from app.extract import Extractor, ExtractRunResult
from app.images.service import ImageRunResult, ImageWorker
from app.ingest import USER_AGENT, build_user_agent
from app.llm import LLMClient
from app.llm.client import set_max_inflight
from app.publish import Policy, sync_story
from app.runs import record_run
from app.settings import Settings
from app.summarize.service import Summarizer, summarizer_options
from app.summarize.worker import SummaryWorker

if TYPE_CHECKING:
    from app.context.service import ContextFinder
    from app.enrich.service import Enricher
    from app.perspectives.service import PerspectiveService

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
        opts = summarizer_options(options)
        if "summarize_max_articles_per_story" not in options.model_fields_set:
            # env default wins unless the profile's summaries: section sets it
            opts["max_articles"] = settings.summarize_max_articles_per_story
        summarizer = Summarizer(
            LLMClient(profile.llm.summarizer, transport=transport),
            max_words=settings.summarize_max_words_per_article,
            **opts,
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
        perspectives=perspectives_from_profile(session_factory, config, profile, transport),
    )


def perspectives_from_profile(
    session_factory: sessionmaker,
    config: AppConfig,
    profile: Profile | None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> "PerspectiveService | None":
    """The perspectives service for the summarize profile's ``perspectives`` settings;
    ``llm.perspectives`` when set, else the summarizer role."""
    if profile is None:
        return None
    from app.perspectives.service import PerspectiveService, perspectives_defaults

    role = profile.llm.perspectives or profile.llm.summarizer
    return PerspectiveService(
        session_factory,
        LLMClient(role, transport=transport),
        profile.perspectives,
        profile_defaults=perspectives_defaults(config),
        max_age_hours=profile.perspectives.max_age_hours,
    )


def images_from_settings(
    session_factory: sessionmaker,
    settings: Settings,
    config: AppConfig,
    extractor: Extractor | None = None,
    **kwargs,
) -> ImageWorker:
    images.configure(config.images, settings.no_image_style)
    return ImageWorker(
        session_factory,
        config.images,
        settings.images_dir,
        pages=extractor.page_images if extractor is not None else None,
        allow_hosts=[h for h in settings.images_allow_hosts.split(",") if h.strip()],
        **kwargs,
    )


def enricher_from_settings(
    session_factory: sessionmaker,
    settings: Settings,
    config: AppConfig,
    *,
    search_transport: httpx.AsyncBaseTransport | None = None,
    page_transport: httpx.AsyncBaseTransport | None = None,
    llm_transport: httpx.AsyncBaseTransport | None = None,
    resolver=None,
) -> "Enricher":
    """Web-search enrichment with the summarize profile's ``enrichment`` settings; each
    profile's ``enrichment.enabled`` is its default (the Settings toggle overrides it)."""
    from app.enrich.fetch import CandidateFetcher
    from app.enrich.service import Enricher
    from app.images.safety import Guard, system_resolver
    from app.search.service import SearchService

    profile = summarize_profile(config, settings)
    cfg = profile.enrichment if profile else EnrichmentConfig()
    allow = [h for h in settings.images_allow_hosts.split(",") if h.strip()]
    fetcher = CandidateFetcher(
        session_factory,
        guard=Guard(resolver or system_resolver, allow_hosts=allow),
        user_agent=build_user_agent(settings.contact_email),
        timeout=settings.extract_timeout_seconds,
        max_bytes=settings.extract_max_bytes,
        min_words=settings.extract_min_words,
        domain_delay=settings.extract_domain_delay_seconds,
        transport=page_transport,
    )
    return Enricher(
        session_factory,
        cfg,
        profile_defaults={p.id: p.enrichment.enabled for p in config.profiles},
        search=SearchService(session_factory, transport=search_transport),
        fetcher=fetcher,
        llm_role=profile.llm.summarizer if profile else None,
        llm_transport=llm_transport,
    )


def context_from_settings(
    session_factory: sessionmaker,
    settings: Settings,
    config: AppConfig,
    *,
    search_transport: httpx.AsyncBaseTransport | None = None,
    page_transport: httpx.AsyncBaseTransport | None = None,
    llm_transport: httpx.AsyncBaseTransport | None = None,
    resolver=None,
) -> "ContextFinder":
    """Background-and-context links with the summarize profile's ``context`` settings;
    each profile's ``context.enabled`` is its default (the Settings toggle overrides it).
    Domain deny/social/aggregator/alias rules come from the ``enrichment`` block."""
    from app.context.service import ContextFinder
    from app.enrich.fetch import CandidateFetcher
    from app.images.safety import Guard, system_resolver
    from app.search.service import SearchService

    profile = summarize_profile(config, settings)
    cfg = profile.context if profile else ContextConfig()
    enrich_cfg = profile.enrichment if profile else EnrichmentConfig()
    allow = [h for h in settings.images_allow_hosts.split(",") if h.strip()]
    fetcher = CandidateFetcher(
        session_factory,
        guard=Guard(resolver or system_resolver, allow_hosts=allow),
        user_agent=build_user_agent(settings.contact_email),
        timeout=settings.extract_timeout_seconds,
        max_bytes=settings.extract_max_bytes,
        min_words=settings.extract_min_words,
        domain_delay=settings.extract_domain_delay_seconds,
        transport=page_transport,
    )
    return ContextFinder(
        session_factory,
        cfg,
        enrichment=enrich_cfg,
        profile_defaults={p.id: p.context.enabled for p in config.profiles},
        search=SearchService(session_factory, transport=search_transport),
        fetcher=fetcher,
        llm_role=(profile.llm.context or profile.llm.summarizer) if profile else None,
        llm_transport=llm_transport,
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
        self.alerts = None
        """An ``AlertEngine``: text rules after extraction, updates after clustering."""
        self.images: ImageWorker | None = None
        """Checks new articles' images after clustering (``app.images``)."""
        self.enricher: Enricher | None = None
        """Web-search enrichment between clustering and the brief (``app.enrich``)."""
        self.context: ContextFinder | None = None
        """Background-and-context links after the brief (``app.context``); never blocks."""

    def attach_enricher(self, enricher: "Enricher") -> "Enricher":
        self.enricher = enricher
        enricher.on_done = self._enriched
        if self.summaries is not None:
            self.summaries.enrich_wait = timedelta(seconds=enricher.config.wait_seconds)
            enricher.backfill = self.summaries.backfill
        return enricher

    def attach_context(self, finder: "ContextFinder") -> "ContextFinder":
        self.context = finder
        # context waits for enrichment passes (they share provider slots + cooldowns)
        finder.peer_running = lambda: self.enricher is not None and self.enricher.running
        # planner-mode LLM calls yield to briefs/reports/perspectives (chat already
        # takes priority slots inside INFLIGHT)
        finder.llm_busy = lambda: self.summaries is not None and self.summaries.state.running
        return finder

    def find_context(self, story_ids: list[int] | None = None, *, force: bool = False) -> int:
        """Queue the background-and-context search (published stories only); never
        blocks the brief — it only schedules work for already-published stories."""
        if self.context is None:
            return 0
        try:
            return self.context.schedule(story_ids, force=force)
        except Exception:
            logger.exception("could not schedule context search")
            return 0

    def _enriched(self, story_ids: list[int]) -> None:
        if self.summaries is not None and self.summaries.enabled:
            self.summaries.start()

    def enrich(self, story_ids: list[int] | None = None, *, force: bool = False) -> int:
        """Queue enrichment (new / under-covered stories); never blocks the LLM worker."""
        if self.enricher is None:
            return 0
        try:
            return self.enricher.schedule(story_ids, force=force)
        except Exception:
            logger.exception("could not schedule enrichment")
            return 0

    def attach_images(self, worker: ImageWorker) -> ImageWorker:
        worker.on_changed = self._republish
        self.images = worker
        return worker

    def _republish(self, session, story_ids: list[int]) -> None:
        """A member's image changed: rebuild those stories' views (no LLM call)."""
        summarizer = self.summaries.summarizer if self.summaries is not None else None
        policy = self.summaries.policy if self.summaries is not None else Policy()
        for sid in story_ids:
            sync_story(session, sid, summarizer=summarizer, policy=policy)

    async def check_images(self) -> ImageRunResult | None:
        if self.images is None:
            return None
        with telemetry.stage("images") as ev:
            result = await self.images.run()
            ev.detail.update(result.model_dump())
        if result.status == "ok" and result.checked:
            self._record("images", result.model_dump())
        return result

    def _alert(self, hook: str, ids: list[int]) -> None:
        if self.alerts is None or not ids:
            return
        try:
            with telemetry.stage("alerts", detail={"hook": hook, "stories": len(ids)}):
                getattr(self.alerts, hook)(ids)
        except Exception:
            logger.exception("alert evaluation (%s) failed", hook)

    async def extract(self, limit: int | None = None) -> ExtractRunResult:
        with telemetry.stage("extract") as ev:
            result = await self.extractor.run(limit)
            ev.detail.update(result.model_dump(exclude={"results"}))
        if result.status == "ok":
            self._record("extract", result.model_dump(exclude={"results"}))
            ok = [r.article_id for r in result.results if r.status == "ok"]
            self._alert("on_extracted", ok)
        return result

    @property
    def extraction_wait(self) -> timedelta:
        policy = self.summaries.policy if self.summaries is not None else Policy()
        return policy.extraction_wait

    def cluster(self, *, rebuild: bool = False) -> ClusterRunResult:
        """Stories whose text is final (extracted, failed, or ``extraction_wait_seconds``
        passed) join an existing story or start their own."""
        try:
            with telemetry.stage("cluster") as ev:
                result = run_clustering(
                    self.session_factory,
                    self.clusterer,
                    rebuild=rebuild,
                    extraction_wait=self.extraction_wait,
                )
                ev.detail.update(result.model_dump(exclude={"joins", "story_ids"}))
        except Exception:
            logger.exception("clustering failed")
            return ClusterRunResult(status="error", rebuild=rebuild)
        if result.status == "ok":
            self._record("cluster", result.model_dump(exclude={"joins", "story_ids"}))
            self._alert("on_clustered", list(result.story_ids))
            self.enrich()  # before the worker looks: new stories' briefs wait for it
            self.find_context()  # published stories only; never blocks the brief
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
        await self.check_images()
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
