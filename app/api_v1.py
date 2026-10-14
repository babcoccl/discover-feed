"""The versioned public read API: ``/api/v1``.

Published stories only (``story_views``), never the extracted article text. Within v1 only
additive changes are made (new endpoints, new optional fields or parameters); anything that
breaks a client goes to ``/api/v2``. The contract is ``docs/openapi.v1.json``
(``make openapi``)."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from app import images
from app import profiles as repo
from app import story_views as views
from app.api import BriefOut, ReportOut, _brief_out, _report_out
from app.auth import require_api_key
from app.db import ping
from app.events import StoryEvent, Subscription, bus, events_after, latest_event_id
from app.models import ProfileRecord
from app.topics import TopicRule, matches
from app.web import snippet

API_VERSION = "1"
PREFIX = "/api/v1"
RETRY_MS = 5000
REPLAY_BATCH = 500

router = APIRouter(prefix=PREFIX, tags=["v1"], dependencies=[Depends(require_api_key)])

_NOW = "2026-10-12T08:30:00Z"
_SOURCE_EXAMPLE = {
    "name": "The Verge",
    "title": "Chipmaker unveils agent-ready accelerator",
    "url": "https://www.theverge.com/2026/10/12/chip-launch",
    "published_at": _NOW,
    "publisher": "theverge.com",
    "via": None,
}
_BRIEF_EXAMPLE = {
    "lead": "A chipmaker unveiled an accelerator aimed at AI agents.",
    "lead_citations": [{"index": 1, "source": "The Verge", "headline": "...", "url": "..."}],
    "bullets": [{"text": "It ships next year.", "citations": []}],
    "status": "ok",
    "style": "brief",
    "basis": "full_text",
    "model": "qwen3.5-9b",
    "generated_at": _NOW,
    "sources": [],
    "label": None,
}


class SourceV1(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    type: str
    url: str = Field(description="The feed or page this app reads.")
    enabled: bool


class TopicV1(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": 3,
                    "name": "AI",
                    "include_keywords": ["ai", "llm"],
                    "exclude_keywords": [],
                    "source_ids": [],
                    "position": 0,
                    "enabled": True,
                }
            ]
        },
    )

    id: int
    name: str
    include_keywords: list[str]
    exclude_keywords: list[str]
    source_ids: list[str] = Field(description="Only these sources; empty = all of the profile's.")
    position: int
    enabled: bool


class ProfileV1(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "slug": "personal-reader",
                    "name": "Personal reader",
                    "description": "Tech and science news",
                    "sources": [
                        {
                            "id": "the-verge",
                            "name": "The Verge",
                            "type": "rss",
                            "url": "https://www.theverge.com/rss/index.xml",
                            "enabled": True,
                        }
                    ],
                    "topics": [],
                }
            ]
        },
    )

    slug: str
    name: str
    description: str
    sources: list[SourceV1]
    topics: list[TopicV1]


class StorySourceV1(BaseModel):
    name: str = Field(description="The source (outlet) name.")
    title: str
    url: str = Field(description="The publisher's own permalink (the raw feed URL).")
    published_at: datetime
    publisher: str = Field(
        default="", description="The primary label: the article URL's domain (`ai.meta.com`)."
    )
    via: str | None = Field(
        default=None, description="The aggregator feed (`Hacker News`) when there is one."
    )


class ImageOut(BaseModel):
    """The story's chosen card image (validated; see ``source``)."""

    url: str | None = Field(description="Original image URL (null: no usable image).")
    proxy_url: str | None = Field(
        default=None, description="This app's cached, resized WebP copy (`/img/{key}`)."
    )
    source: Literal["feed", "page", "member", "logo", "none"] = Field(
        description="feed/page: the representative article's feed or page image; member: "
        "another member's; logo/none: no image (logo: a publisher icon is available)."
    )
    width: int | None = None
    height: int | None = None


def _image_out(story: views.PublishedStory, base: str) -> ImageOut | None:
    image = story.image
    if not image:
        return None
    key = image.get("key") if images.config().cache else None
    return ImageOut(
        url=image.get("url"),
        proxy_url=f"{base}/img/{key}" if key and image.get("url") else None,
        source=image.get("source") or ("feed" if image.get("url") else "none"),
        width=image.get("width"),
        height=image.get("height"),
    )


class StoryCardV1(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": 42,
                    "version": 3,
                    "title": "Chipmaker unveils agent-ready accelerator",
                    "url": "https://news.example/story/42",
                    "published_at": _NOW,
                    "updated_at": _NOW,
                    "first_seen_at": _NOW,
                    "last_updated_at": _NOW,
                    "image_url": None,
                    "snippet": "The company said...",
                    "brief": _BRIEF_EXAMPLE,
                    "source_count": 2,
                    "source_names": ["The Verge", "Ars Technica"],
                    "report": None,
                    "sources": None,
                }
            ]
        }
    )

    id: int
    version: int = Field(description="The published version; bumps on every update.")
    title: str
    url: str = Field(description="The story page in this app.")
    published_at: datetime | None = Field(description="When the story was first published.")
    updated_at: datetime | None = Field(description="When this version was published.")
    first_seen_at: datetime
    last_updated_at: datetime = Field(description="Newest member article.")
    image_url: str | None
    image: ImageOut | None = None
    snippet: str = Field(description="The feed's own summary (when `brief` is null).")
    brief: BriefOut | None
    source_count: int
    source_names: list[str]
    report: ReportOut | None = Field(default=None, description="Only with `include=full`.")
    sources: list[StorySourceV1] | None = Field(
        default=None, description="Only with `include=full`: one article per source."
    )


class FeedV1(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "profile": "personal-reader",
                    "topic": None,
                    "include": "card",
                    "items": [StoryCardV1.model_config["json_schema_extra"]["examples"][0]],
                    "next_cursor": "MjAyNi0xMC0xMlQwODozMDowMCswMDowMHw0Mg",
                }
            ]
        }
    )

    profile: str
    topic: TopicV1 | None
    include: Literal["card", "full"]
    items: list[StoryCardV1] = Field(description="Published stories, newest update first.")
    next_cursor: str | None = Field(description="Pass as `cursor` for the next page.")


class VersionsV1(BaseModel):
    story: int = Field(description="The published story version (as in events).")
    schema_version: int = Field(description="Version of the stored story document.")


class StoryV1(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": 42,
                    "version": 3,
                    "title": "Chipmaker unveils agent-ready accelerator",
                    "url": "https://news.example/story/42",
                    "published_at": _NOW,
                    "updated_at": _NOW,
                    "first_seen_at": _NOW,
                    "last_updated_at": _NOW,
                    "image_url": None,
                    "snippet": "The company said...",
                    "brief": _BRIEF_EXAMPLE,
                    "report": None,
                    "report_state": "pending",
                    "report_reason": "published before its report",
                    "model": "qwen3.5-9b",
                    "versions": {"story": 3, "schema_version": 1},
                    "sources": [_SOURCE_EXAMPLE],
                }
            ]
        }
    )

    id: int
    version: int
    title: str
    url: str
    published_at: datetime | None
    updated_at: datetime | None
    first_seen_at: datetime
    last_updated_at: datetime
    image_url: str | None
    image: ImageOut | None = None
    snippet: str
    brief: BriefOut | None
    report: ReportOut | None = Field(description="Paragraphs only when `report_state=ready`.")
    report_state: Literal["ready", "pending", "skipped", "failed"] | None = Field(
        description="null with reports off."
    )
    report_reason: str | None
    model: str
    versions: VersionsV1
    sources: list[StorySourceV1] = Field(description="Every member article, oldest first.")


class HealthV1(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"status": "ok", "database": "ok", "version": "0.1.0"}]}
    )

    status: Literal["ok", "degraded"]
    database: Literal["ok", "error"]
    version: str


class VersionV1(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"api_version": "1", "app_version": "0.1.0", "schema_version": 1}]
        }
    )

    api_version: str
    app_version: str
    schema_version: int = Field(description="Version of the stored story documents.")


def _session(request: Request) -> Session:
    return request.app.state.session_factory()


def _profile(session: Session, slug: str) -> ProfileRecord:
    profile = repo.get_profile(session, slug)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"unknown profile {slug!r}")
    return profile


def _scope(session: Session, slug: str, topic_id: int | None):
    """``(profile, topic, rule, source_ids)`` of a profile/topic filter."""
    profile = _profile(session, slug)
    topic = None
    if topic_id is not None:
        topic = repo.get_topic(profile, topic_id)
        if topic is None:
            raise HTTPException(status_code=404, detail=f"unknown topic {topic_id}")
    rule = repo.topic_rule(topic) if topic else TopicRule()
    source_ids = {s.id for s in profile.sources}
    if rule.source_ids:
        source_ids &= set(rule.source_ids)
    return profile, topic, rule, sorted(source_ids)


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _sources(members: Sequence[views.Member]) -> list[StorySourceV1]:
    return [
        StorySourceV1(
            name=m.source,
            title=m.title,
            url=m.url,
            published_at=m.published_at,
            publisher=m.publisher,
            via=m.via,
        )
        for m in members
    ]


def card(story: views.PublishedStory, base: str, full: bool = False) -> StoryCardV1:
    return StoryCardV1(
        id=story.story_id,
        version=story.version,
        title=story.title,
        url=f"{base}/story/{story.story_id}",
        published_at=story.published_at,
        updated_at=story.published_at,
        first_seen_at=story.first_seen_at,
        last_updated_at=story.last_updated_at,
        image_url=story.image_url,
        image=_image_out(story, base),
        snippet=snippet(story.snippet_source),
        brief=_brief_out(story.brief),
        source_count=story.source_count,
        source_names=[m.source for m in story.sources],
        report=_report_out(story.report) if full else None,
        sources=_sources(story.sources) if full else None,
    )


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@router.get("/profiles", response_model=list[ProfileV1], summary="All profiles")
def list_profiles(request: Request) -> list[ProfileRecord]:
    with _session(request) as session:
        return repo.list_profiles(session)


@router.get("/profiles/{slug}", response_model=ProfileV1, summary="One profile")
def get_profile(request: Request, slug: str) -> ProfileRecord:
    with _session(request) as session:
        return _profile(session, slug)


@router.get("/profiles/{slug}/topics", response_model=list[TopicV1], summary="A profile's topics")
def list_topics(request: Request, slug: str):
    with _session(request) as session:
        return list(_profile(session, slug).topics)


_TOPIC_Q = Annotated[int | None, Query(description="Topic id; omit for the whole profile.")]


@router.get(
    "/profiles/{slug}/feed",
    response_model=FeedV1,
    summary="Published stories, newest update first",
)
def profile_feed(
    request: Request,
    slug: str,
    topic: _TOPIC_Q = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 24,
    cursor: Annotated[str | None, Query(description="`next_cursor` of the previous page.")] = None,
    since: Annotated[
        datetime | None, Query(description="Only stories first published after this time.")
    ] = None,
    updated_since: Annotated[
        datetime | None,
        Query(description="Only stories whose current version was published after this time."),
    ] = None,
    include: Annotated[
        Literal["card", "full"], Query(description="`full` adds `report` and `sources`.")
    ] = "card",
) -> FeedV1:
    with _session(request) as session:
        profile, selected, rule, source_ids = _scope(session, slug, topic)
        try:
            found, more = views.published_page(
                session,
                source_ids,
                rule,
                limit=limit,
                cursor=repo.decode_cursor(cursor) if cursor else None,
                published_after=_utc(since),
                updated_after=_utc(updated_since),
            )
        except repo.InvalidCursor:
            raise HTTPException(status_code=422, detail="invalid cursor") from None
        topic_out = TopicV1.model_validate(selected) if selected else None
    last = found[-1] if found else None
    base = _base(request)
    return FeedV1(
        profile=profile.slug,
        topic=topic_out,
        include=include,
        items=[card(story, base, include == "full") for _, _, story in found],
        next_cursor=repo.encode_cursor(last[0], last[1]) if more and last else None,
    )


@router.get(
    "/stories/{story_id}",
    response_model=StoryV1,
    summary="One published story",
    responses={301: {"description": "Merged into another story (follow `Location`)."}},
)
def get_story(
    request: Request,
    story_id: int,
    profile: Annotated[
        str | None, Query(description="Only members from this profile's sources.")
    ] = None,
):
    with _session(request) as session:
        record = _profile(session, profile) if profile else None
        target = views.resolve(session, story_id)
        if target is not None and target != story_id:
            query = request.url.query
            return RedirectResponse(
                f"{PREFIX}/stories/{target}" + (f"?{query}" if query else ""), status_code=301
            )
        story = views.published_story(
            session, story_id, [s.id for s in record.sources] if record else None
        )
    if story is None:
        raise HTTPException(status_code=404, detail=f"story {story_id} is not published")
    report = story.report
    return StoryV1(
        id=story.story_id,
        version=story.version,
        title=story.title,
        url=f"{_base(request)}/story/{story.story_id}",
        published_at=story.published_at,
        updated_at=story.published_at,
        first_seen_at=story.first_seen_at,
        last_updated_at=story.last_updated_at,
        image_url=story.image_url,
        image=_image_out(story, _base(request)),
        snippet=snippet(story.snippet_source),
        brief=_brief_out(story.brief),
        report=_report_out(report),
        report_state=report.state if report else None,
        report_reason=report.reason if report else None,
        model=story.model,
        versions=VersionsV1(story=story.version, schema_version=views.SCHEMA_VERSION),
        sources=_sources(story.members),
    )


ATOM = "http://www.w3.org/2005/Atom"


def _iso(value: datetime | None) -> str:
    return (_utc(value) or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")


def atom_feed(profile: ProfileRecord, stories: Sequence[views.PublishedStory], base: str) -> bytes:
    ET.register_namespace("", ATOM)

    def sub(parent, tag, text=None, **attrs):
        el = ET.SubElement(parent, f"{{{ATOM}}}{tag}", attrs)
        if text is not None:
            el.text = text
        return el

    feed = ET.Element(f"{{{ATOM}}}feed")
    sub(feed, "id", f"{base}/p/{profile.slug}")
    sub(feed, "title", profile.name)
    times = [s.published_at or s.last_updated_at for s in stories]
    sub(feed, "updated", _iso(max(times) if times else None))
    sub(feed, "link", rel="self", href=f"{base}{PREFIX}/profiles/{profile.slug}/feed.atom")
    sub(feed, "link", rel="alternate", href=f"{base}/p/{profile.slug}")
    author = sub(feed, "author")
    sub(author, "name", "Discover Feed")
    for story in stories:
        entry = sub(feed, "entry")
        sub(entry, "id", f"{base}/story/{story.story_id}")
        sub(entry, "title", story.title)
        sub(entry, "updated", _iso(story.published_at or story.last_updated_at))
        sub(entry, "published", _iso(story.published_at or story.first_seen_at))
        sub(entry, "link", rel="alternate", href=f"{base}/story/{story.story_id}")
        for m in story.sources:
            sub(entry, "link", rel="related", href=m.url, title=m.label)
        brief = story.brief
        if brief is not None and brief.style != "snippet":
            lead = ET.Element("p")
            lead.text = brief.lead_text
            html = ET.tostring(lead, encoding="unicode")
            if brief.bullets:
                ul = ET.Element("ul")
                for b in brief.bullets:
                    ET.SubElement(ul, "li").text = b.text
                html += ET.tostring(ul, encoding="unicode")
            sub(entry, "content", html, type="html")
        else:
            sub(entry, "content", snippet(story.snippet_source), type="text")
    return b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(feed, encoding="utf-8")


@router.get(
    "/profiles/{slug}/feed.atom",
    summary="Atom feed of published stories",
    response_class=Response,
    responses={200: {"content": {"application/atom+xml": {}}, "description": "Atom 1.0"}},
)
def profile_atom(
    request: Request,
    slug: str,
    topic: _TOPIC_Q = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> Response:
    """Title = headline; content = the brief's lead and bullets; links to the story page
    (`alternate`) and to each source (`related`)."""
    with _session(request) as session:
        profile, _topic, rule, source_ids = _scope(session, slug, topic)
        found, _more = views.published_page(session, source_ids, rule, limit=limit)
    body = atom_feed(profile, [s for _, _, s in found], _base(request))
    return Response(body, media_type="application/atom+xml; charset=utf-8")


@router.get("/health", response_model=HealthV1, summary="Liveness and database check")
def health(request: Request) -> HealthV1:
    db_ok = ping(request.app.state.engine)
    return HealthV1(
        status="ok" if db_ok else "degraded",
        database="ok" if db_ok else "error",
        version=request.app.version,
    )


@router.get("/version", response_model=VersionV1, summary="API and app versions")
def get_version(request: Request) -> VersionV1:
    return VersionV1(
        api_version=API_VERSION,
        app_version=request.app.version,
        schema_version=views.SCHEMA_VERSION,
    )


# --- stream -------------------------------------------------------------------------------


@dataclass
class StreamFilter:
    profile: str | None = None
    source_ids: list[str] | None = None
    rule: TopicRule | None = None
    include: str = "id"
    base: str = ""


def _event_data(session_factory: sessionmaker, f: StreamFilter, item: StoryEvent) -> dict | None:
    """The ``data`` of one event, or None when the filter drops it."""
    if f.profile is not None and f.profile not in item.profiles:
        return None
    data = {
        "id": item.id,
        "type": item.type,
        "story_id": item.story_id,
        "version": item.version,
        "profiles": list(item.profiles),
    }
    if item.type == "alert.fired":  # profile-scoped, no story content (may be unpublished)
        return data | (item.data or {})
    needs_story = f.include != "id" or (f.rule is not None and item.type != "story.retracted")
    if not needs_story or item.type == "story.retracted":
        return data
    with session_factory() as session:
        story = views.published_story(session, item.story_id, f.source_ids)
    if not _in_topic(f.rule, story):
        return None
    if f.include != "id":
        data["story"] = (
            card(story, f.base, f.include == "full").model_dump(mode="json")
            if (story is not None)
            else None
        )
    return data


def _in_topic(rule: TopicRule | None, story: views.PublishedStory | None) -> bool:
    if rule is None or not (rule.include or rule.exclude or rule.source_ids):
        return True
    return story is not None and any(
        matches(rule, title=m.title, summary=m.summary_raw, source_id=m.source_id)
        for m in story.members
    )


def sse(event: str, data: dict, id_: int | None = None) -> str:
    head = f"id: {id_}\n" if id_ is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def stream_events(
    session_factory: sessionmaker,
    f: StreamFilter,
    queue: Subscription,
    *,
    after_id: int | None,
    heartbeat: float = 15,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[str]:
    """``retry:``, a ``hello`` event (latest id), stored events after ``after_id``, then live
    events; ids only increase (live duplicates of replayed events are skipped). A queue
    that overflowed while this client was slow is caught up from the table."""

    def latest() -> int:
        with session_factory() as session:
            return latest_event_id(session)

    def replay(after: int) -> tuple[list[StoryEvent], bool]:
        with session_factory() as session:
            raw = events_after(session, after, limit=REPLAY_BATCH)
        return raw, len(raw) == REPLAY_BATCH

    newest = await run_in_threadpool(latest)
    last = newest if after_id is None else after_id
    yield f"retry: {RETRY_MS}\n\n"
    yield sse("hello", {"latest_id": newest, "resume_from": last})
    catch_up = last < newest
    while True:
        if catch_up or queue.overflowed:
            queue.overflowed = False
            batch, more = await run_in_threadpool(replay, last)
            for item in batch:
                data = await run_in_threadpool(_event_data, session_factory, f, item)
                last = item.id
                if data is not None:
                    yield sse(item.type, data, item.id)
            catch_up = more
            continue
        try:
            item = await asyncio.wait_for(queue.get(), timeout=heartbeat)
        except TimeoutError:
            if is_disconnected is not None and await is_disconnected():
                return
            yield sse("heartbeat", {"latest_id": last})
            continue
        if item.id <= last:
            continue
        data = await run_in_threadpool(_event_data, session_factory, f, item)
        last = item.id
        if data is not None:
            yield sse(item.type, data, item.id)


@router.get(
    "/stream",
    summary="Story events (Server-Sent Events), resumable",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "SSE"}},
)
async def stream(
    request: Request,
    profile: Annotated[str | None, Query(description="Only this profile's stories.")] = None,
    topic: Annotated[int | None, Query(description="Only this topic (needs `profile`).")] = None,
    include: Annotated[
        Literal["id", "card", "full"],
        Query(description="`card`/`full` add the story (as in the feed) to each event."),
    ] = "id",
    since_id: Annotated[
        int | None, Query(ge=0, description="Replay stored events after this id.")
    ] = None,
    heartbeat: Annotated[float, Query(gt=0, le=300, description="Seconds between pings.")] = 15,
    last_event_id: Annotated[
        str | None, Header(description="Sent by EventSource on reconnect; wins over since_id.")
    ] = None,
) -> StreamingResponse:
    """Events `story.published`, `story.updated`, `story.retracted` with
    `id: <n>` and `data: {"id", "type", "story_id", "version", "profiles"[, "story"]}`, and
    `alert.fired` (`data` adds `"rule", "article_id", "matched"`; sent for unpublished
    stories too, never with story content; not filtered by `topic`);
    first a `hello` event (`latest_id`), a `heartbeat` event every `heartbeat` seconds and a
    `retry:` hint. Reconnect with `Last-Event-ID` (or `since_id`) to get every event you
    missed (kept `summaries.event_retention_days`). Listening never starts generation."""
    f = StreamFilter(include=include, base=_base(request))
    if topic is not None and profile is None:
        raise HTTPException(status_code=422, detail="topic needs profile")
    if profile is not None:
        with _session(request) as session:
            _p, _t, rule, source_ids = _scope(session, profile, topic)
        f.profile, f.source_ids, f.rule = profile, source_ids, rule
    after = since_id
    if last_event_id is not None:
        try:
            after = int(last_event_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid Last-Event-ID") from None
    queue = bus.subscribe()

    async def body():
        try:
            async for chunk in stream_events(
                request.app.state.session_factory,
                f,
                queue,
                after_id=after,
                heartbeat=heartbeat,
                is_disconnected=request.is_disconnected,
            ):
                yield chunk
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
