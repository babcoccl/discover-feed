from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import profiles as repo
from app.cluster.service import ClusterRunResult
from app.extract import ExtractRunResult
from app.ingest import SourceRunResult
from app.models import Article, ProfileRecord, Story
from app.summarize.views import SummaryView, summary_views
from app.summarize.worker import SummarizeRunResult, WorkerStatus
from app.web import snippet

router = APIRouter(prefix="/api")


class ArticleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: str
    url: str = Field(description="Link to show users: the feed's own permalink for the article.")
    canonical_url: str = Field(
        description="Normalized URL used only for deduplication; don't link to it."
    )
    title: str
    summary_raw: str
    author: str | None
    published_at: datetime
    fetched_at: datetime
    image_url: str | None
    content_hash: str


class RefreshResponse(BaseModel):
    results: list[SourceRunResult]


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    type: str
    url: str
    enabled: bool
    refresh_minutes: int


class TopicIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    include_keywords: list[str] = Field(
        default_factory=list,
        description="Whole-word, case-insensitive; any match includes. Empty = all articles.",
    )
    exclude_keywords: list[str] = Field(default_factory=list, description="Any match excludes.")
    source_ids: list[str] = Field(
        default_factory=list, description="Restrict to these profile sources; empty = all."
    )
    enabled: bool = True
    position: int | None = Field(
        default=None, ge=0, description="0-based tab position; omit to append / keep."
    )


class TopicOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    include_keywords: list[str]
    exclude_keywords: list[str]
    source_ids: list[str]
    position: int
    enabled: bool


class ProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    description: str
    sources: list[SourceOut]
    topics: list[TopicOut]


class FeedItem(ArticleOut):
    source_name: str


class FeedOut(BaseModel):
    profile: str
    topic: TopicOut | None
    group: Literal["articles"] = "articles"
    items: list[FeedItem]
    next_cursor: str | None = Field(description="Pass as `cursor` to get the next page.")


class StorySourceOut(BaseModel):
    name: str
    url: str = Field(description="The publisher's own permalink for this source's article.")
    published_at: datetime


class CitationOut(BaseModel):
    index: int = Field(description="The [n] marker used in the summary.")
    source: str
    headline: str
    url: str = Field(description="The cited article's own permalink (never the canonical URL).")


class SummarySentenceOut(BaseModel):
    text: str
    citations: list[int] = Field(description="Indexes [n] of the sources this sentence cites.")


class SummaryOut(BaseModel):
    text: str = Field(description="The summary with [n] markers after each sentence.")
    citations: list[CitationOut] = Field(description="Numbered sources, [1] first.")
    sentences: list[SummarySentenceOut]
    basis: Literal["full_text", "feed_summary", "mixed"] = Field(
        description="What the model read: extracted article text, feed snippets, or both."
    )
    model: str
    generated_at: datetime


def _summary_out(view: SummaryView | None) -> SummaryOut | None:
    if view is None:
        return None
    return SummaryOut(
        text=view.marked_text,
        citations=[
            CitationOut(index=c.index, source=c.source, headline=c.headline, url=c.url)
            for c in view.citations
        ],
        sentences=[
            SummarySentenceOut(text=s.text, citations=[c.index for c in s.citations])
            for s in view.sentences
        ],
        basis=view.basis,
        model=view.model,
        generated_at=view.generated_at,
    )


_SUMMARY_FIELD = Field(description="Latest generated summary; null until one is generated.")


class StoryItemOut(BaseModel):
    id: int | None = Field(description="Story id; null for an article not clustered yet.")
    title: str
    image_url: str | None
    snippet: str = Field(description="The feed's own summary (fallback when `summary` is null).")
    summary: SummaryOut | None = _SUMMARY_FIELD
    sources: list[StorySourceOut]
    source_count: int
    first_seen_at: datetime
    last_updated_at: datetime


class StoryFeedOut(BaseModel):
    profile: str
    topic: TopicOut | None
    group: Literal["stories"] = "stories"
    items: list[StoryItemOut]
    next_cursor: str | None = Field(description="Pass as `cursor` to get the next page.")


class StoryMemberOut(BaseModel):
    id: int
    title: str
    source_id: str
    source: str
    url: str
    published_at: datetime
    summary: str


class StoryOut(BaseModel):
    id: int
    title: str
    representative_article_id: int
    source_count: int
    first_seen_at: datetime
    last_updated_at: datetime
    summary: SummaryOut | None = _SUMMARY_FIELD
    members: list[StoryMemberOut] = Field(description="Oldest first.")


class AdminArticleOut(ArticleOut):
    text_status: str
    text_fetched_at: datetime | None
    word_count: int | None
    story_id: int | None
    text: str | None = Field(
        default=None, description="Extracted body; only with `include_text=true`."
    )


def _session(request: Request) -> Session:
    return request.app.state.session_factory()


def _profile_or_404(session: Session, slug: str) -> ProfileRecord:
    profile = repo.get_profile(session, slug)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"unknown profile {slug!r}")
    return profile


@router.get(
    "/articles",
    response_model=list[ArticleOut],
    tags=["articles"],
    summary="List normalized articles, newest first",
)
def list_articles(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    source_id: Annotated[str | None, Query(description="Only articles from this source.")] = None,
    since: Annotated[
        datetime | None,
        Query(description="Only articles at or after this ISO 8601 time (UTC if naive)."),
    ] = None,
    time_field: Annotated[
        Literal["published", "fetched"],
        Query(
            description="Timestamp `since` filters on: `published` (feed date) or `fetched` "
            "(when this app first stored the article, i.e. new since you last looked). "
            "Results are always ordered by published time, newest first."
        ),
    ] = "published",
) -> list[Article]:
    stmt = select(Article).order_by(Article.published_at.desc(), Article.id.desc()).limit(limit)
    if source_id:
        stmt = stmt.where(Article.source_id == source_id)
    if since:
        since = since.replace(tzinfo=UTC) if since.tzinfo is None else since.astimezone(UTC)
        column = Article.fetched_at if time_field == "fetched" else Article.published_at
        stmt = stmt.where(column >= since)
    with request.app.state.session_factory() as session:
        return list(session.scalars(stmt))


@router.post(
    "/admin/refresh",
    response_model=RefreshResponse,
    tags=["admin"],
    summary="Fetch sources now (all enabled sources, or one by id)",
)
async def refresh(
    request: Request,
    source_id: Annotated[str | None, Query(description="Refresh only this source id.")] = None,
) -> RefreshResponse:
    with _session(request) as session:
        if source_id:
            source = repo.get_source(session, source_id)
            if source is None:
                raise HTTPException(status_code=404, detail=f"unknown source {source_id!r}")
            sources = [source]
        else:
            sources = repo.active_sources(session)
    results = await request.app.state.ingestor.run(sources, force=True)
    return RefreshResponse(results=results)


@router.get("/profiles", response_model=list[ProfileOut], tags=["profiles"])
def list_profiles(request: Request) -> list[ProfileRecord]:
    with _session(request) as session:
        return repo.list_profiles(session)


@router.get("/profiles/{slug}", response_model=ProfileOut, tags=["profiles"])
def get_profile(request: Request, slug: str) -> ProfileRecord:
    with _session(request) as session:
        return _profile_or_404(session, slug)


@router.get(
    "/profiles/{slug}/feed",
    response_model=StoryFeedOut | FeedOut,
    tags=["profiles"],
    summary="A profile's feed of stories or articles, optionally filtered by topic",
)
def profile_feed(
    request: Request,
    slug: str,
    topic: Annotated[int | None, Query(description="Topic id; omit for all articles.")] = None,
    group: Annotated[
        Literal["stories", "articles"],
        Query(
            description="`stories` (default): related articles from the profile's sources "
            "grouped into one item, newest update first. `articles`: one item per article."
        ),
    ] = "stories",
    limit: Annotated[int, Query(ge=1, le=100)] = 24,
    cursor: Annotated[str | None, Query(description="`next_cursor` of the previous page.")] = None,
    time_field: Annotated[
        Literal["published", "fetched"],
        Query(description="Timestamp to order and paginate articles by (`group=articles`)."),
    ] = "published",
) -> StoryFeedOut | FeedOut:
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        selected = None
        if topic is not None:
            selected = repo.get_topic(profile, topic)
            if selected is None:
                raise HTTPException(status_code=404, detail=f"unknown topic {topic}")
        names = repo.source_names(session)
        topic_out = TopicOut.model_validate(selected) if selected else None
        try:
            if group == "stories":
                stories = repo.story_feed_page(
                    session, profile, selected, limit=limit, cursor=cursor
                )
                summaries = summary_views(session, [i.story_id for i in stories.items], names)
                return StoryFeedOut(
                    profile=profile.slug,
                    topic=topic_out,
                    items=[_story_item_out(item, names, summaries) for item in stories.items],
                    next_cursor=stories.next_cursor,
                )
            page = repo.feed_page(
                session, profile, selected, limit=limit, cursor=cursor, time_field=time_field
            )
        except repo.InvalidCursor:
            raise HTTPException(status_code=422, detail="invalid cursor") from None
    return FeedOut(
        profile=profile.slug,
        topic=topic_out,
        items=[
            FeedItem(
                **ArticleOut.model_validate(a).model_dump(),
                source_name=names.get(a.source_id, a.source_id),
            )
            for a in page.items
        ],
        next_cursor=page.next_cursor,
    )


def _story_item_out(
    item: repo.StoryItem, names: dict[str, str], summaries: dict[int, SummaryView]
) -> StoryItemOut:
    return StoryItemOut(
        id=item.story_id,
        title=item.title,
        image_url=item.image_url,
        snippet=snippet(item.snippet_source),
        summary=_summary_out(summaries.get(item.story_id)),
        sources=[
            StorySourceOut(
                name=names.get(a.source_id, a.source_id), url=a.url, published_at=a.published_at
            )
            for a in item.sources
        ],
        source_count=item.source_count,
        first_seen_at=item.first_seen_at,
        last_updated_at=item.last_updated_at,
    )


@router.get("/stories/{story_id}", response_model=StoryOut, tags=["stories"])
def get_story(
    request: Request,
    story_id: int,
    profile: Annotated[
        str | None, Query(description="Only members from this profile's sources.")
    ] = None,
) -> StoryOut:
    with _session(request) as session:
        record = _profile_or_404(session, profile) if profile else None
        item = repo.get_story(session, story_id, record)
        if item is None:
            raise HTTPException(status_code=404, detail=f"unknown story {story_id}")
        names = repo.source_names(session)
        summary = summary_views(session, [story_id], names).get(story_id)
    return StoryOut(
        id=story_id,
        title=item.title,
        representative_article_id=item.representative.id,
        source_count=item.source_count,
        first_seen_at=item.first_seen_at,
        last_updated_at=item.last_updated_at,
        summary=_summary_out(summary),
        members=[
            StoryMemberOut(
                id=m.id,
                title=m.title,
                source_id=m.source_id,
                source=names.get(m.source_id, m.source_id),
                url=m.url,
                published_at=m.published_at,
                summary=m.summary_raw,
            )
            for m in item.members
        ],
    )


@router.post(
    "/admin/extract",
    response_model=ExtractRunResult,
    tags=["admin"],
    summary="Extract article text now (pending articles, newest first)",
)
async def extract(
    request: Request,
    limit: Annotated[
        int | None, Query(ge=1, le=1000, description="Default: DISCOVER_EXTRACT_MAX_ARTICLES.")
    ] = None,
) -> ExtractRunResult:
    return await request.app.state.pipeline.extract(limit)


@router.post(
    "/admin/cluster",
    response_model=ClusterRunResult,
    tags=["admin"],
    summary="Cluster unassigned articles into stories now",
)
async def cluster(
    request: Request,
    rebuild: Annotated[
        bool, Query(description="Drop all stories and recluster every article.")
    ] = False,
) -> ClusterRunResult:
    return await run_in_threadpool(request.app.state.pipeline.cluster, rebuild=rebuild)


class SummarizeOut(BaseModel):
    started: bool = Field(description="A background run was started (no `story_id`/`wait`).")
    result: SummarizeRunResult | None = Field(description="Set when the run was awaited.")
    status: WorkerStatus


@router.post(
    "/admin/summarize",
    response_model=SummarizeOut,
    tags=["admin"],
    summary="Summarize queued stories now, or one story",
)
async def summarize(
    request: Request,
    story_id: Annotated[
        int | None, Query(description="Summarize only this story (always awaited).")
    ] = None,
    force: Annotated[
        bool, Query(description="Regenerate even if the input is unchanged (all stories if no id).")
    ] = False,
    wait: Annotated[bool, Query(description="Wait for the run instead of starting it.")] = False,
) -> SummarizeOut:
    worker = request.app.state.summaries
    if not worker.enabled:
        raise HTTPException(status_code=409, detail="no summarizer configured")
    if story_id is not None:
        with _session(request) as session:
            if session.get(Story, story_id) is None:
                raise HTTPException(status_code=404, detail=f"unknown story {story_id}")
        worker.enqueue([story_id], force=force)
        result = await worker.run(story_id=story_id, manual=True)
        return SummarizeOut(started=False, result=result, status=worker.status())
    if force:
        worker.enqueue_all(force=True)
    else:
        worker.enqueue_missing()
    if wait:
        result = await worker.run(manual=True)
        return SummarizeOut(started=False, result=result, status=worker.status())
    started = worker.start(manual=True)
    return SummarizeOut(started=started, result=None, status=worker.status())


@router.get(
    "/admin/articles/{article_id}",
    response_model=AdminArticleOut,
    response_model_exclude_none=False,
    tags=["admin"],
    summary="One article with its pipeline state (internal; text only on request)",
)
def admin_article(
    request: Request,
    article_id: int,
    include_text: Annotated[bool, Query(description="Include the extracted text.")] = False,
) -> AdminArticleOut:
    with _session(request) as session:
        article = session.get(Article, article_id)
        if article is None:
            raise HTTPException(status_code=404, detail=f"unknown article {article_id}")
        out = AdminArticleOut.model_validate(article)
    if not include_text:
        out.text = None
    return out


def _topic_kwargs(body: TopicIn) -> dict:
    return {
        "name": body.name,
        "include": body.include_keywords,
        "exclude": body.exclude_keywords,
        "source_ids": body.source_ids,
        "enabled": body.enabled,
        "position": body.position,
    }


@router.post("/profiles/{slug}/topics", response_model=TopicOut, status_code=201, tags=["topics"])
def create_topic(request: Request, slug: str, body: TopicIn):
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        try:
            return repo.create_topic(session, profile, **_topic_kwargs(body))
        except repo.TopicError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None


@router.put("/profiles/{slug}/topics/{topic_id}", response_model=TopicOut, tags=["topics"])
def update_topic(request: Request, slug: str, topic_id: int, body: TopicIn):
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        topic = repo.get_topic(profile, topic_id)
        if topic is None:
            raise HTTPException(status_code=404, detail=f"unknown topic {topic_id}")
        try:
            return repo.update_topic(session, profile, topic, **_topic_kwargs(body))
        except repo.TopicError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None


@router.delete("/profiles/{slug}/topics/{topic_id}", status_code=204, tags=["topics"])
def delete_topic(request: Request, slug: str, topic_id: int) -> Response:
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        topic = repo.get_topic(profile, topic_id)
        if topic is None:
            raise HTTPException(status_code=404, detail=f"unknown topic {topic_id}")
        repo.delete_topic(session, profile, topic)
    return Response(status_code=204)
