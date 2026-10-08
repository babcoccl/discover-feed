from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import profiles as repo
from app.ingest import SourceRunResult
from app.models import Article, ProfileRecord

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
    items: list[FeedItem]
    next_cursor: str | None = Field(description="Pass as `cursor` to get the next page.")


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
    response_model=FeedOut,
    tags=["profiles"],
    summary="A profile's feed, optionally filtered by topic (cursor-paginated, newest first)",
)
def profile_feed(
    request: Request,
    slug: str,
    topic: Annotated[int | None, Query(description="Topic id; omit for all articles.")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 24,
    cursor: Annotated[str | None, Query(description="`next_cursor` of the previous page.")] = None,
    time_field: Annotated[
        Literal["published", "fetched"],
        Query(description="Timestamp to order and paginate by."),
    ] = "published",
) -> FeedOut:
    with _session(request) as session:
        profile = _profile_or_404(session, slug)
        selected = None
        if topic is not None:
            selected = repo.get_topic(profile, topic)
            if selected is None:
                raise HTTPException(status_code=404, detail=f"unknown topic {topic}")
        try:
            page = repo.feed_page(
                session, profile, selected, limit=limit, cursor=cursor, time_field=time_field
            )
        except repo.InvalidCursor:
            raise HTTPException(status_code=422, detail="invalid cursor") from None
        names = repo.source_names(session)
    return FeedOut(
        profile=profile.slug,
        topic=TopicOut.model_validate(selected) if selected else None,
        items=[
            FeedItem(
                **ArticleOut.model_validate(a).model_dump(),
                source_name=names.get(a.source_id, a.source_id),
            )
            for a in page.items
        ],
        next_cursor=page.next_cursor,
    )


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
