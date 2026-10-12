"""Source management: CRUD and sharing, soft delete, auto-disable recovery, health,
feed autodiscovery, read-only test fetches, OPML, the starter catalog and YAML sync."""

import html as html_lib
import io
import time
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import feedparser
import httpx
import yaml
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import FEED_TYPES, AppConfig, Source, SourceType, slugify
from app.ingest import load_quota, usage_periods
from app.models import (
    Article,
    ProfileRecord,
    SourceRecord,
    SourceStatus,
    SourceUsage,
    Topic,
    utcnow,
)
from app.sources import FetchError, FetchState, create_adapter
from app.sources.rss import _to_raw
from app.topics import normalize_keywords

CATALOG_PATH = Path(__file__).resolve().parent.parent / "config" / "source_catalog.yaml"
FEED_MIME = ("application/rss+xml", "application/atom+xml", "application/feed+json")


class SourceError(ValueError):
    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


def validation_message(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        where = ".".join(str(x) for x in error.get("loc", ()) if x != "__root__")
        message = str(error.get("msg", "")).removeprefix("Value error, ")
        parts.append(f"{where}: {message}" if where else message)
    return "; ".join(parts)


def build_source(data: dict[str, Any]) -> Source:
    try:
        return Source.model_validate(data)
    except ValidationError as exc:
        raise SourceError(validation_message(exc)) from None


# --- queries ---------------------------------------------------------------------------------


def list_sources(session: Session, *, include_removed: bool = False) -> list[SourceRecord]:
    stmt = select(SourceRecord).order_by(func.lower(SourceRecord.name))
    if not include_removed:
        stmt = stmt.where(SourceRecord.removed_at.is_(None))
    return list(session.scalars(stmt))


def profile_sources(profile: ProfileRecord) -> list[SourceRecord]:
    return [s for s in profile.sources if s.removed_at is None]


def source_profiles(session: Session, record: SourceRecord) -> list[ProfileRecord]:
    return [p for p in session.scalars(select(ProfileRecord)) if record in p.sources]


def get_record(session: Session, source_id: str) -> SourceRecord:
    record = session.get(SourceRecord, source_id)
    if record is None:
        raise SourceError(f"unknown source {source_id!r}", 404)
    return record


def _profiles(session: Session, slugs: Sequence[str]) -> list[ProfileRecord]:
    found = []
    for slug in dict.fromkeys(slugs):
        profile = session.scalar(select(ProfileRecord).where(ProfileRecord.slug == slug))
        if profile is None:
            raise SourceError(f"unknown profile {slug!r}", 404)
        found.append(profile)
    return found


def _same_target(record: SourceRecord, source: Source) -> bool:
    return (
        record.type == source.type.value
        and record.url == str(source.url)
        and (record.params or {}) == source.params
    )


# --- writes ----------------------------------------------------------------------------------


def create_source(
    session: Session, source: Source, profile_slugs: Sequence[str]
) -> tuple[SourceRecord, bool]:
    """Add a source to profiles. A source with the same type/URL/params is shared instead of
    duplicated (and restored if it had been removed). Returns ``(record, created)``."""
    profiles = _profiles(session, profile_slugs)
    existing = next(
        (r for r in session.scalars(select(SourceRecord)) if _same_target(r, source)), None
    )
    created = existing is None
    if existing is not None:
        record = existing
        if record.removed_at is not None:
            record.removed_at = None
            record.enabled = source.enabled
    else:
        base = source.id
        source_id, n = base, 2
        while session.get(SourceRecord, source_id) is not None:
            source_id, n = f"{base}-{n}", n + 1
        record = SourceRecord.from_config(source.model_copy(update={"id": source_id}))
        record.created_at = utcnow()
        session.add(record)
    for profile in profiles:
        if record not in profile.sources:
            profile.sources.append(record)
    session.commit()
    return record, created


def update_source(
    session: Session,
    record: SourceRecord,
    source: Source,
    profile_slugs: Sequence[str] | None = None,
) -> SourceRecord:
    if record.removed_at is not None:
        raise SourceError(f"source {record.id!r} was removed; restore it first", 409)
    if source.type.value != record.type:
        raise SourceError("a source's type cannot change; add a new source instead")
    record.apply(source.model_copy(update={"id": record.id}))
    if source.enabled:
        _clear_disabled(session, record)
    if profile_slugs is not None:
        assign_profiles(session, record, profile_slugs, commit=False)
    session.commit()
    return record


def assign_profiles(
    session: Session, record: SourceRecord, slugs: Sequence[str], *, commit: bool = True
) -> None:
    wanted = {p.slug for p in _profiles(session, slugs)}
    for profile in session.scalars(select(ProfileRecord)):
        linked = record in profile.sources
        if profile.slug in wanted and not linked:
            profile.sources.append(record)
        elif profile.slug not in wanted and linked:
            profile.sources.remove(record)
    if commit:
        session.commit()


def _clear_disabled(session: Session, record: SourceRecord) -> None:
    record.auto_disabled_at = None
    record.disabled_reason = None
    status = session.get(SourceStatus, record.id)
    if status is not None:
        status.consecutive_failures = 0
        status.next_attempt_at = None


def set_enabled(session: Session, record: SourceRecord, enabled: bool) -> SourceRecord:
    if record.removed_at is not None:
        raise SourceError(f"source {record.id!r} was removed; restore it first", 409)
    if enabled:
        build_source({**record.to_config().model_dump(mode="json"), "enabled": True})
        _clear_disabled(session, record)
    record.enabled = enabled
    session.commit()
    return record


def remove_source(session: Session, record: SourceRecord) -> SourceRecord:
    """Soft delete: stop fetching and hide it; its articles and stories are kept."""
    record.removed_at = record.removed_at or utcnow()
    record.enabled = False
    session.commit()
    return record


def restore_source(session: Session, record: SourceRecord) -> SourceRecord:
    """Undelete and re-enable (an X source without a monthly cap stays disabled)."""
    record.removed_at = None
    try:
        build_source({**record.to_config().model_dump(mode="json"), "enabled": True})
    except SourceError:
        record.enabled = False
    else:
        _clear_disabled(session, record)
        record.enabled = True
    session.commit()
    return record


# --- health ----------------------------------------------------------------------------------


@dataclass
class SourceHealth:
    id: str
    name: str
    type: str
    url: str
    enabled: bool
    removed: bool
    auto_disabled_at: datetime | None
    disabled_reason: str | None
    last_success_at: datetime | None
    last_attempt_at: datetime | None
    consecutive_failures: int
    last_error: str | None
    next_fetch_at: datetime | None
    items_per_day: float
    items_last_run: int | None
    requests_today: int
    daily_request_budget: int | None
    reads_this_month: int
    spend_this_month_usd: float
    monthly_budget_usd: float | None
    profiles: list[str] = field(default_factory=list)

    @property
    def state(self) -> str:
        if self.removed:
            return "removed"
        if self.auto_disabled_at is not None:
            return "auto_disabled"
        if not self.enabled:
            return "disabled"
        if self.consecutive_failures:
            return "failing"
        return "ok" if self.last_success_at else "pending"


def source_health(
    session: Session, records: Sequence[SourceRecord], now: datetime | None = None
) -> list[SourceHealth]:
    now = now or utcnow()
    ids = [r.id for r in records]
    if not ids:
        return []
    statuses = {
        s.source_id: s
        for s in session.scalars(select(SourceStatus).where(SourceStatus.source_id.in_(ids)))
    }
    since = now - timedelta(days=7)
    counts = dict(
        session.execute(
            select(Article.source_id, func.count())
            .where(Article.source_id.in_(ids), Article.fetched_at >= since)
            .group_by(Article.source_id)
        ).all()
    )
    day, month = usage_periods(now)
    usage = {
        (u.source_id, u.period): u
        for u in session.scalars(
            select(SourceUsage).where(
                SourceUsage.source_id.in_(ids), SourceUsage.period.in_([day, month])
            )
        )
    }
    slugs: dict[str, list[str]] = {i: [] for i in ids}
    for profile in session.scalars(select(ProfileRecord).order_by(ProfileRecord.id)):
        for source in profile.sources:
            if source.id in slugs:
                slugs[source.id].append(profile.slug)
    out = []
    for record in records:
        status = statuses.get(record.id)
        limits = record.limits or {}
        today, this_month = usage.get((record.id, day)), usage.get((record.id, month))
        out.append(
            SourceHealth(
                id=record.id,
                name=record.name,
                type=record.type,
                url=record.url,
                enabled=bool(record.enabled),
                removed=record.removed_at is not None,
                auto_disabled_at=record.auto_disabled_at,
                disabled_reason=record.disabled_reason,
                last_success_at=status.last_success_at if status else None,
                last_attempt_at=status.last_attempt_at if status else None,
                consecutive_failures=(status.consecutive_failures or 0) if status else 0,
                last_error=status.last_error if status else None,
                next_fetch_at=_next_fetch(record, status, now),
                items_per_day=round(counts.get(record.id, 0) / 7, 1),
                items_last_run=status.items_last_run if status else None,
                requests_today=today.requests if today else 0,
                daily_request_budget=limits.get("daily_request_budget"),
                reads_this_month=this_month.reads if this_month else 0,
                spend_this_month_usd=round(this_month.spend_usd, 4) if this_month else 0.0,
                monthly_budget_usd=limits.get("monthly_budget_usd"),
                profiles=slugs[record.id],
            )
        )
    return out


def _next_fetch(record: SourceRecord, status: SourceStatus | None, now: datetime):
    if not record.enabled or record.removed_at is not None:
        return None
    if status is None or status.last_attempt_at is None:
        return now
    if status.next_attempt_at is not None:
        return status.next_attempt_at
    return status.last_attempt_at + timedelta(minutes=record.refresh_minutes)


def auto_disabled(session: Session, records: Sequence[SourceRecord]) -> list[SourceRecord]:
    return [r for r in records if r.auto_disabled_at is not None and not r.enabled]


# --- discovery and test fetch ------------------------------------------------------------------


class FeedCandidate(BaseModel):
    url: str
    title: str | None = None
    type: SourceType = SourceType.RSS


class _LinkFinder(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self.base: str | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {k.lower(): (v or "") for k, v in attrs}
        if tag == "base" and values.get("href") and self.base is None:
            self.base = values["href"]
        rel = values.get("rel", "").lower().split()
        feed_type = values.get("type", "").lower() in FEED_MIME
        if tag == "link" and "alternate" in rel and feed_type and values.get("href"):
            self.links.append(values)


def feed_links(html: str, page_url: str) -> list[FeedCandidate]:
    """``<link rel="alternate" type="application/rss+xml|atom+xml">`` feeds of a page."""
    finder = _LinkFinder()
    finder.feed(html)
    base = urljoin(page_url, finder.base) if finder.base else page_url
    found: dict[str, FeedCandidate] = {}
    for link in finder.links:
        url = urljoin(base, link["href"].strip())
        if url.startswith(("http://", "https://")) and url not in found:
            kind = SourceType.ATOM if "atom" in link.get("type", "") else SourceType.RSS
            found[url] = FeedCandidate(url=url, title=link.get("title") or None, type=kind)
    return list(found.values())


def _parse_feed(response: httpx.Response):
    headers = {k.lower(): v for k, v in response.headers.items()}
    headers["content-location"] = str(response.url)
    parsed = feedparser.parse(io.BytesIO(response.content), response_headers=headers)
    is_feed = bool(parsed.entries) or (
        not parsed.bozo and parsed.get("version") and parsed.feed.get("title")
    )
    return parsed if is_feed else None


def _feed_type(parsed) -> SourceType:
    return SourceType.ATOM if str(parsed.get("version", "")).startswith("atom") else SourceType.RSS


async def discover_feeds(client: httpx.AsyncClient, url: str) -> list[FeedCandidate]:
    """``url`` itself if it is a feed, else the feeds its HTML page advertises."""
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        raise SourceError(f"could not load {url}: {type(exc).__name__}", 502) from None
    if response.status_code >= 400:
        raise SourceError(f"HTTP {response.status_code} from {url}", 502)
    parsed = _parse_feed(response)
    if parsed is not None:
        return [
            FeedCandidate(
                url=str(response.url), title=parsed.feed.get("title"), type=_feed_type(parsed)
            )
        ]
    return feed_links(response.text, str(response.url))


class TestItem(BaseModel):
    title: str
    url: str | None
    published_at: datetime | None


class TestFetchResult(BaseModel):
    ok: bool
    status: int | None = Field(description="HTTP status of the (first) request.")
    latency_ms: int
    title: str | None = Field(description="Feed title (or the API source's name).")
    item_count: int
    items: list[TestItem] = Field(description="The five newest items.")
    error: str | None = None
    detected_type: SourceType | None = None


async def test_fetch(
    client: httpx.AsyncClient, source: Source, session: Session | None = None
) -> TestFetchResult:
    """Fetch once and report what would be ingested. Writes nothing (no articles, status,
    validators or usage counters)."""
    start = time.perf_counter()
    status_code: list[int] = []

    async def remember(response: httpx.Response) -> None:
        status_code.append(response.status_code)

    client.event_hooks.setdefault("response", []).append(remember)

    def result(**kw) -> TestFetchResult:
        return TestFetchResult(
            status=status_code[0] if status_code else None,
            latency_ms=int((time.perf_counter() - start) * 1000),
            **kw,
        )

    if source.type is SourceType.X:
        return result(
            ok=False,
            title=source.name,
            item_count=0,
            items=[],
            error="X reads are billed: test fetches are disabled; save the source (disabled) "
            "and refresh it once its monthly budget is set",
        )
    try:
        if source.type in FEED_TYPES:
            response = await client.get(str(source.url))
            if response.status_code >= 400:
                raise FetchError(f"HTTP {response.status_code} from {source.url}")
            parsed = _parse_feed(response)
            if parsed is None:
                raise FetchError("not a feed (no RSS/Atom items found)")
            raws = [_to_raw(entry) for entry in parsed.entries]
            title, detected = parsed.feed.get("title"), _feed_type(parsed)
        else:
            quota = load_quota(session, source, utcnow()) if session is not None else None
            state = FetchState(quota=quota)
            raws = await create_adapter(source.type, client).fetch(source, state)
            title, detected = source.name, source.type
    except Exception as exc:
        message = str(exc) or type(exc).__name__
        if isinstance(exc, httpx.HTTPError):
            message = f"{type(exc).__name__} while fetching"
        return result(ok=False, title=None, item_count=0, items=[], error=message)
    newest = sorted(
        raws, key=lambda r: r.published_at.timestamp() if r.published_at else 0, reverse=True
    )[:5]
    return result(
        ok=True,
        title=title,
        item_count=len(raws),
        items=[
            TestItem(
                title=html_lib.unescape(r.title or r.url or "(untitled)")[:300],
                url=r.url,
                published_at=r.published_at,
            )
            for r in newest
        ],
        detected_type=detected,
    )


# --- OPML --------------------------------------------------------------------------------------


def export_opml(profile: ProfileRecord) -> str:
    root = ET.Element("opml", version="2.0")
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "title").text = f"discover-feed: {profile.name}"
    body = ET.SubElement(root, "body")
    for record in profile_sources(profile):
        if record.type not in {t.value for t in FEED_TYPES}:
            continue
        attrs = {
            "type": "rss",
            "text": record.name,
            "title": record.name,
            "xmlUrl": record.url,
        }
        if record.tags:
            attrs["category"] = ",".join(record.tags)
        ET.SubElement(body, "outline", attrs)
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


class OpmlResult(BaseModel):
    added: list[str] = Field(default_factory=list, description="New source ids.")
    shared: list[str] = Field(
        default_factory=list, description="Existing sources now also in this profile."
    )
    skipped: list[str] = Field(default_factory=list, description="Already in the profile.")
    errors: list[str] = Field(default_factory=list)


def import_opml(session: Session, profile: ProfileRecord, text: str) -> OpmlResult:
    try:
        root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    except ET.ParseError as exc:
        raise SourceError(f"not valid OPML: {exc}") from None
    if root.tag != "opml":
        raise SourceError("not valid OPML: the root element must be <opml>")
    out = OpmlResult()
    for outline in root.iter("outline"):
        url = (outline.get("xmlUrl") or "").strip()
        if not url:
            continue
        name = (outline.get("title") or outline.get("text") or url).strip()
        tags = [t.strip() for t in (outline.get("category") or "").split(",") if t.strip()]
        try:
            source = Source(id=slugify(name)[:90] or "feed", name=name, url=url, tags=tags)
        except ValidationError as exc:
            out.errors.append(f"{name}: {validation_message(exc)}")
            continue
        already = any(_same_target(r, source) for r in profile_sources(profile))
        if already:
            out.skipped.append(name)
            continue
        record, created = create_source(session, source, [profile.slug])
        (out.added if created else out.shared).append(record.id)
    return out


# --- catalog -----------------------------------------------------------------------------------


class CatalogEntry(BaseModel):
    category: str
    name: str
    url: str
    type: SourceType = SourceType.RSS
    tags: list[str] = Field(default_factory=list)
    first_party: bool = False
    aggregator: bool = False
    note: str | None = None

    @property
    def id(self) -> str:
        return slugify(self.name)

    def to_source(self) -> Source:
        return Source(
            name=self.name,
            type=self.type,
            url=self.url,
            tags=self.tags,
            first_party=self.first_party,
            aggregator=self.aggregator,
        )


def load_catalog(path: Path = CATALOG_PATH) -> list[CatalogEntry]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = []
    for category in raw.get("categories") or []:
        for item in category.get("sources") or []:
            entries.append(CatalogEntry(category=category["name"], **item))
    return entries


# --- YAML sync ---------------------------------------------------------------------------------


class SyncResult(BaseModel):
    profiles_added: list[str] = Field(default_factory=list)
    sources_added: list[str] = Field(default_factory=list)
    sources_linked: list[str] = Field(
        default_factory=list, description="`profile/source` links added for existing sources."
    )
    topics_added: list[str] = Field(default_factory=list, description="`profile/topic`.")
    skipped_removed: list[str] = Field(
        default_factory=list, description="Sources removed in the app; left removed."
    )


def sync_from_yaml(session: Session, config: AppConfig) -> SyncResult:
    """Add profiles, sources, profile/source links and topics that the YAML has and the DB
    lacks. Never deletes and never overwrites an existing record."""
    out = SyncResult()
    for profile_cfg in config.profiles:
        profile = session.scalar(select(ProfileRecord).where(ProfileRecord.slug == profile_cfg.id))
        if profile is None:
            profile = ProfileRecord(
                slug=profile_cfg.id, name=profile_cfg.name, description=profile_cfg.description
            )
            session.add(profile)
            out.profiles_added.append(profile_cfg.id)
        for source in profile_cfg.sources:
            record = session.get(SourceRecord, source.id)
            if record is None:
                record = SourceRecord.from_config(source)
                record.created_at = utcnow()
                session.add(record)
                out.sources_added.append(source.id)
            elif record.removed_at is not None:
                out.skipped_removed.append(source.id)
                continue
            if record not in profile.sources:
                profile.sources.append(record)
                if source.id not in out.sources_added:
                    out.sources_linked.append(f"{profile_cfg.id}/{source.id}")
        session.flush()
        names = {t.name.strip().lower() for t in profile.topics}
        ids_by_name = {s.name: s.id for s in profile_cfg.sources}
        for topic in profile_cfg.topics:
            if topic.name.strip().lower() in names:
                continue
            profile.topics.append(
                Topic(
                    name=topic.name.strip(),
                    include_keywords=normalize_keywords(topic.include),
                    exclude_keywords=normalize_keywords(topic.exclude),
                    source_ids=[ids_by_name[n] for n in topic.sources if n in ids_by_name],
                    position=len(profile.topics),
                    enabled=topic.enabled,
                )
            )
            names.add(topic.name.strip().lower())
            out.topics_added.append(f"{profile_cfg.id}/{topic.name.strip()}")
    session.commit()
    return out


# --- scheduling --------------------------------------------------------------------------------


def reschedule(state: Any) -> None:
    """Re-sync the per-source ingestion jobs with the DB after a source change."""
    from app.profiles import active_sources
    from app.scheduler import sync_ingestion_jobs

    scheduler = getattr(state, "scheduler", None)
    settings = getattr(state, "settings", None)
    if scheduler is None or settings is None or not settings.scheduler_enabled:
        return
    if not scheduler.running:
        return
    with state.session_factory() as session:
        sources = active_sources(session)
    sync_ingestion_jobs(
        scheduler, sources, state.ingestor, jitter_seconds=settings.refresh_jitter_seconds
    )
