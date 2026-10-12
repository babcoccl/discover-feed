"""Throwaway demo DB seeded from the example profiles and fixture feeds (`make demo`, demo.cmd).

No network is used: every source URL is served from ``tests/fixtures/demo/<id>.xml`` and
article pages from ``tests/fixtures/demo/pages/<id>.html`` (items link to the local
``/demo-article/<id>`` route; ``pages/robots.txt`` is served for every host) through
``httpx.MockTransport``.
After ingesting, the demo extracts text, clusters stories and summarizes them just like the
scheduled pipeline. Summaries come from a deterministic in-process fake LLM
(``app.summarize.fake``) unless ``--real-llm`` is given, which uses the first profile's
``llm.summarizer`` endpoint (``LOCAL_LLM_BASE_URL``, ``LOCAL_LLM_API_KEY``, ``LOCAL_LLM_MODEL``).
The DB lives in ``.demo/`` and is recreated on every run, so the real database (``./data`` or
the Docker volume) is never touched.
"""

import argparse
import asyncio
import os
import re
import threading
import webbrowser
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
from sqlalchemy import func, select

from app.alerts.engine import AlertEngine
from app.alerts.repo import seed_alerts
from app.alerts.sender import AlertSender
from app.alerts.sink import FakeSink
from app.cluster.service import ClusterRunResult
from app.config import Source, load_config
from app.db import init_db, make_engine, make_session_factory
from app.demo_pages import DEMO_PATH, FIXTURE_DIR, PLACEHOLDER, feed_xml, page_html, robots_txt
from app.extract import ExtractRunResult
from app.ingest import Ingestor, SourceRunResult
from app.models import Notification, utcnow
from app.pipeline import (
    Pipeline,
    clusterer_from_settings,
    extractor_from_settings,
    summary_worker_from_settings,
)
from app.profiles import active_sources, seed_from_config
from app.settings import Settings
from app.summarize.fake import FakeLLM
from app.summarize.worker import SummarizeRunResult

ROOT = Path(__file__).resolve().parent.parent
DEMO_DB = ROOT / ".demo" / "demo.db"
DEMO_BASE = "http://localhost:8000"
EXAMPLE_CONFIG = ROOT / "config" / "profiles.example.yaml"
LIVE_DIR = FIXTURE_DIR / "live"
"""Feed items dated ``{{ago:<minutes>}}`` before the demo starts, so keyword alerts fire."""
DEMO_PROFILE = "market-monitor"
_AGO = re.compile(r"\{\{ago:(\d+)\}\}")


def fixture_transport(
    sources: Sequence[Source], fixture_dir: Path, demo_base: str = DEMO_BASE
) -> httpx.MockTransport:
    files = {str(httpx.URL(str(s.url))): s.id for s in sources}

    def handler(request: httpx.Request) -> httpx.Response:
        source_id = files.get(str(request.url))
        body = feed_xml(fixture_dir, source_id, demo_base) if source_id else None
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, content=body, headers={"content-type": "application/rss+xml"})

    return httpx.MockTransport(handler)


def page_transport(fixture_dir: Path) -> httpx.MockTransport:
    """Article pages (``/demo-article/<id>``) and robots.txt for the extractor."""
    robots = robots_txt(fixture_dir)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text=robots, headers={"content-type": "text/plain"})
        body = (
            page_html(fixture_dir, path.removeprefix(DEMO_PATH))
            if path.startswith(DEMO_PATH)
            else None
        )
        if body is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(
            200, content=body, headers={"content-type": "text/html; charset=utf-8"}
        )

    return httpx.MockTransport(handler)


def live_transport(
    sources: Sequence[Source], live_dir: Path, demo_base: str, now: datetime
) -> httpx.MockTransport:
    files = {str(httpx.URL(str(s.url))): s.id for s in sources}

    def handler(request: httpx.Request) -> httpx.Response:
        source_id = files.get(str(request.url))
        path = live_dir / f"{source_id}.xml" if source_id else None
        if path is None or not path.exists():
            return httpx.Response(404)
        text = path.read_text(encoding="utf-8").replace(PLACEHOLDER, demo_base.rstrip("/"))
        text = _AGO.sub(lambda m: format_datetime(now - timedelta(minutes=int(m.group(1)))), text)
        return httpx.Response(
            200, content=text.encode(), headers={"content-type": "application/rss+xml"}
        )

    return httpx.MockTransport(handler)


def wire_demo_alerts(session, config, sink: FakeSink, now: datetime) -> None:
    """Point the demo profile's channels at the local sink and add a channel that always
    fails plus a rule in quiet hours, so the log shows sent, failed and held entries."""
    from zoneinfo import ZoneInfo

    from app.alerts import repo
    from app.alerts.api import AlertRuleIn
    from app.config import AlertChannel
    from app.profiles import get_profile

    profile = get_profile(session, DEMO_PROFILE)
    if profile is None:
        return
    for channel in repo.list_channels(session, profile):
        if channel.type == "webhook":
            channel.config = {**channel.config, "url": sink.webhook_url}
        elif channel.type == "ntfy":
            channel.config = {**channel.config, "server": sink.ntfy_server}
            channel.secret_env = {}
        elif channel.type == "email":
            channel.config = {
                **channel.config,
                "host": sink.host,
                "port": sink.smtp_port,
                "tls": "none",
            }
            channel.secret_env = {}
    pager = AlertChannel(
        id="pager", type="webhook", name="Pager (demo: always fails)", url=f"{sink.http_url}/down"
    )
    session.add(repo.channel_record(profile.id, pager))
    for rule in repo.list_rules(session, profile):
        if rule.name == "SEC enforcement":
            rule.channels = [*(rule.channels or []), "pager"]
    cfg = config.get_profile(DEMO_PROFILE).alerts
    local = now.astimezone(ZoneInfo(cfg.timezone))
    quiet = AlertRuleIn(
        name="Nvidia overnight (demo: quiet hours)",
        include=[{"keyword": "NVDA", "case_sensitive": True}],
        channels=["email"],
        quiet_hours={
            "start": (local - timedelta(hours=1)).strftime("%H:%M"),
            "end": (local + timedelta(hours=2)).strftime("%H:%M"),
        },
    )
    repo.save_rule(session, profile, quiet.model_dump())
    session.commit()


class DemoBuild:
    def __init__(
        self,
        settings: Settings,
        ingested: list[SourceRunResult],
        extracted: ExtractRunResult,
        clustered: ClusterRunResult,
        summarized: SummarizeRunResult | None = None,
        llm_transport: httpx.AsyncBaseTransport | None = None,
        sink: FakeSink | None = None,
        alerts: dict | None = None,
    ) -> None:
        self.settings = settings
        self.ingested = ingested
        self.extracted = extracted
        self.clustered = clustered
        self.summarized = summarized
        self.llm_transport = llm_transport
        """What the app should use for the LLM: the fake, or None for the real endpoint."""
        self.sink = sink
        self.alerts = alerts
        """Notification counts by status after the build (with a sink)."""

    def __iter__(self):  # `settings, results = build_demo()` keeps working
        return iter((self.settings, self.ingested))


def build_demo(
    db_path: Path = DEMO_DB,
    *,
    config_path: Path = EXAMPLE_CONFIG,
    fixture_dir: Path = FIXTURE_DIR,
    real_llm: bool = False,
    summarize_limit: int | None = None,
    report_mode: str | None = None,
    fake_delay: float = 0,
    fake_chat_mode: str | None = None,
    fake_token_delay: float = 0.03,
    demo_base: str = DEMO_BASE,
    sink: FakeSink | None = None,
) -> DemoBuild:
    """``summarize_limit``: summarize only the newest N stories now (default: all); the rest
    stay queued for "Summarize now" in Settings > Pipeline. Every story is generated and
    published now (the fixtures are older than the backfill window) unless ``report_mode``
    is ``off``. ``fake_delay``: seconds the fake LLM takes
    per answer once the app runs (shows the "Generating detailed report" state).
    ``sink``: a started ``FakeSink``; the demo profile's alert channels deliver to it and a
    few "live" market items (``LIVE_DIR``) are ingested so the alerts fire."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-journal", "-wal", "-shm"):
        try:
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)
        except PermissionError as exc:  # Windows keeps files open by a running demo locked
            raise SystemExit(
                f"Cannot replace {db_path}: is another demo still running? Stop it and retry."
            ) from exc

    settings = Settings(
        config_path=config_path,
        database_url=f"sqlite:///{db_path.as_posix()}",
        scheduler_enabled=False,
        report_mode=report_mode,
    )
    engine = make_engine(settings.database_url)
    try:
        init_db(engine)
        session_factory = make_session_factory(engine)
        config = load_config(config_path)
        with session_factory() as session:
            seed_from_config(session, config)
            seed_alerts(session, config)
            sources = active_sources(session)
        ingestor = Ingestor(
            session_factory, transport=fixture_transport(sources, fixture_dir, demo_base)
        )
        results = asyncio.run(ingestor.run(sources, force=True))
        clusterer = clusterer_from_settings(settings)
        alerts, sender = None, None
        if sink is not None:
            now = utcnow()
            with session_factory() as session:
                wire_demo_alerts(session, config, sink, now)
            sender = AlertSender(session_factory, config, backoff_seconds=0)
            alerts = AlertEngine(session_factory, config, clusterer=clusterer)
            live = [s for s in sources if (fixture_dir / "live" / f"{s.id}.xml").exists()]
            ingestor = Ingestor(
                session_factory,
                transport=live_transport(live, fixture_dir / "live", demo_base, now),
            )
            ingestor.on_new_articles = alerts.on_ingested
            results += asyncio.run(ingestor.run(live, force=True))
        pipeline = Pipeline(
            session_factory,
            # Local fixtures: no need for the per-domain politeness delay.
            extractor_from_settings(
                session_factory,
                settings,
                transport=page_transport(fixture_dir),
                domain_delay=0,
                max_articles=10_000,
            ),
            clusterer,
        )
        pipeline.alerts = alerts
        extracted, clustered = asyncio.run(pipeline.run())
        alert_counts = None
        if sender is not None:
            for _ in range(20):  # retries without backoff, so the failing channel gives up
                if not any(sender.send_due().values()):
                    break
            with session_factory() as session:
                alert_counts = dict(
                    session.execute(
                        select(Notification.status, func.count()).group_by(Notification.status)
                    ).all()
                )
        transport = None if real_llm else FakeLLM("demo").transport()
        worker = summary_worker_from_settings(
            session_factory, settings, load_config(config_path), transport=transport
        )
        # The fixtures are older than the backfill window: generate every story.
        worker.backfill, worker.backfill_max = timedelta(days=36500), 100_000
        # Show the "Summary unavailable" fallback right away instead of after hours.
        worker.policy = replace(worker.policy, brief_fallback=timedelta(0))
        summarized = asyncio.run(worker.run(manual=True, limit=summarize_limit or 100_000))
        if not real_llm:
            transport = FakeLLM(
                "demo", delay=fake_delay, chat_mode=fake_chat_mode, token_delay=fake_token_delay
            ).transport()
    finally:
        engine.dispose()
    return DemoBuild(
        settings, results, extracted, clustered, summarized, transport, sink, alert_counts
    )


def main() -> None:
    import uvicorn

    from app.main import create_app

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--open",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="open the demo in the default browser once it starts",
    )
    parser.add_argument(
        "--real-llm",
        action="store_true",
        help="summarize with the real endpoint from LOCAL_LLM_BASE_URL / LOCAL_LLM_API_KEY "
        "instead of the built-in fake LLM",
    )
    parser.add_argument("--model", help="model name for --real-llm (sets LOCAL_LLM_MODEL)")
    parser.add_argument(
        "--summaries",
        type=int,
        default=10,
        help="with --real-llm, stories to summarize before starting (default 10; the rest "
        "can be summarized from Settings > Pipeline)",
    )
    parser.add_argument(
        "--report-mode",
        choices=["auto", "off"],
        help="override the profiles' summaries.report_mode",
    )
    parser.add_argument(
        "--fake-chat-mode",
        choices=[
            "ok",
            "stream_error",
            "stall",
            "empty",
            "invalid_citation",
            "invented_number",
            "http_500",
            "timeout",
        ],
        help="how the fake LLM answers story questions (shows the chat panel's error and "
        "unverified states)",
    )
    parser.add_argument(
        "--fake-llm-delay",
        type=float,
        default=0,
        help="seconds the fake LLM takes per answer while the demo runs (default 0)",
    )
    args = parser.parse_args()
    if args.real_llm:
        if not os.environ.get("LOCAL_LLM_BASE_URL"):
            parser.error("--real-llm needs LOCAL_LLM_BASE_URL, e.g. http://<host>:8080/v1")
        if args.model:
            os.environ["LOCAL_LLM_MODEL"] = args.model
        print(f"Summarizing with {os.environ['LOCAL_LLM_BASE_URL']} (may take a while) ...")
    elif args.model:
        parser.error("--model only applies with --real-llm")

    os.environ.setdefault("ALERT_WEBHOOK_SECRET", "demo-webhook-secret")
    sink = FakeSink(webhook_secret=os.environ["ALERT_WEBHOOK_SECRET"]).start()
    build = build_demo(
        sink=sink,
        real_llm=args.real_llm,
        summarize_limit=args.summaries if args.real_llm else None,
        report_mode=args.report_mode,
        fake_delay=args.fake_llm_delay,
        fake_chat_mode=args.fake_chat_mode,
        demo_base=f"http://localhost:{args.port}",
    )
    settings = build.settings
    for r in build.ingested:
        print(f"  {r.source_id:<32} {r.status:<6} {r.new_articles} articles")
    e, c = build.extracted, build.clustered
    print(f"  text extraction: {e.ok} ok, {e.failed} failed, {e.skipped} skipped")
    print(f"  stories: {c.stories} ({c.multi_source_stories} covered by 2+ sources)")
    if (sm := build.summarized) is not None:
        llm = "real LLM" if args.real_llm else "fake LLM"
        print(
            f"  briefs + reports ({llm}): {sm.ok} ok, {sm.fallback} fallback, "
            f"{sm.failed} failed, {sm.rejected} rejected reports, {sm.queued} queued"
        )
    if build.alerts is not None:
        counts = ", ".join(f"{n} {status}" for status, n in sorted(build.alerts.items()))
        print(f"  keyword alerts: {counts or 'none'} (see /demo/sink and Alerts)")
    url = f"http://localhost:{args.port}/"
    print(f"Demo DB: {DEMO_DB}\nOpen {url}  (Ctrl+C to stop)")
    if args.open:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    app = create_app(settings, llm_transport=build.llm_transport, demo_sink=sink)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
