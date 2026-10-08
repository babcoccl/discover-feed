from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.ingest import SourceRunResult
from app.models import Article

router = APIRouter(prefix="/api")


class ArticleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: str
    canonical_url: str
    title: str
    summary_raw: str
    author: str | None
    published_at: datetime
    fetched_at: datetime
    image_url: str | None
    content_hash: str


class RefreshResponse(BaseModel):
    results: list[SourceRunResult]


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
        Query(description="Only articles published at or after this ISO 8601 time (UTC if naive)."),
    ] = None,
) -> list[Article]:
    stmt = select(Article).order_by(Article.published_at.desc(), Article.id.desc()).limit(limit)
    if source_id:
        stmt = stmt.where(Article.source_id == source_id)
    if since:
        since = since.replace(tzinfo=UTC) if since.tzinfo is None else since.astimezone(UTC)
        stmt = stmt.where(Article.published_at >= since)
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
    config = request.app.state.config
    if source_id:
        source = config.get_source(source_id)
        if source is None:
            raise HTTPException(status_code=404, detail=f"unknown source {source_id!r}")
        sources = [source]
    else:
        sources = config.all_sources()
    results = await request.app.state.ingestor.run(sources, force=True)
    return RefreshResponse(results=results)
