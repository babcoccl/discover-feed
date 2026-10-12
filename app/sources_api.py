"""``/api/v1`` source management (admin key): CRUD, sharing, health, feed discovery,
read-only test fetches, OPML, the starter catalog and "sync from YAML"."""

from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app import source_admin as admin
from app.api_v1 import PREFIX
from app.auth import require_admin
from app.config import SourceType, load_config
from app.models import SourceRecord
from app.profiles import get_profile

router = APIRouter(prefix=PREFIX, tags=["v1"], dependencies=[Depends(require_admin)])
_NOW = "2026-10-12T08:30:00Z"

_HEALTH_EXAMPLE = {
    "id": "guardian-world",
    "name": "Guardian world",
    "type": "guardian",
    "state": "ok",
    "enabled": True,
    "auto_disabled_at": None,
    "disabled_reason": None,
    "last_success_at": _NOW,
    "last_attempt_at": _NOW,
    "consecutive_failures": 0,
    "last_error": None,
    "next_fetch_at": "2026-10-12T09:00:00Z",
    "items_per_day": 42.3,
    "items_last_run": 20,
    "requests_today": 12,
    "daily_request_budget": 200,
    "reads_this_month": 0,
    "spend_this_month_usd": 0.0,
    "monthly_budget_usd": None,
    "profiles": ["market-monitor"],
}


class SourceHealthV1(BaseModel):
    model_config = ConfigDict(
        from_attributes=True, json_schema_extra={"examples": [_HEALTH_EXAMPLE]}
    )

    id: str
    name: str
    type: str
    state: str = Field(description="ok, pending, failing, disabled, auto_disabled or removed.")
    enabled: bool
    auto_disabled_at: datetime | None
    disabled_reason: str | None
    last_success_at: datetime | None
    last_attempt_at: datetime | None
    consecutive_failures: int
    last_error: str | None
    next_fetch_at: datetime | None
    items_per_day: float = Field(description="Articles stored per day, last 7 days.")
    items_last_run: int | None
    requests_today: int = Field(description="API requests today (UTC).")
    daily_request_budget: int | None
    reads_this_month: int = Field(description="Paid reads (X posts) this month (UTC).")
    spend_this_month_usd: float
    monthly_budget_usd: float | None
    profiles: list[str]


class SourceAdminV1(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": "guardian-world",
                    "name": "Guardian world",
                    "type": "guardian",
                    "url": "https://content.guardianapis.com/",
                    "enabled": True,
                    "refresh_minutes": 30,
                    "tags": ["world"],
                    "aggregator": False,
                    "first_party": False,
                    "params": {"section": "world", "page_size": 20},
                    "api_key_env": "GUARDIAN_API_KEY",
                    "api_key_set": True,
                    "limits": {"daily_request_budget": 200},
                    "removed_at": None,
                    "profiles": ["market-monitor"],
                    "health": _HEALTH_EXAMPLE,
                }
            ]
        }
    )

    id: str
    name: str
    type: str
    url: str
    enabled: bool
    refresh_minutes: int
    tags: list[str]
    aggregator: bool
    first_party: bool
    params: dict[str, Any]
    api_key_env: str | None = Field(description="Variable name only; the key is never returned.")
    api_key_set: bool = Field(description="Whether that variable is set in the environment.")
    limits: dict[str, Any]
    removed_at: datetime | None
    profiles: list[str]
    health: SourceHealthV1


class SourceIn(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "name": "Ars Technica",
                    "type": "rss",
                    "url": "https://feeds.arstechnica.com/arstechnica/index",
                    "tags": ["tech"],
                    "profiles": ["personal-reader"],
                },
                {
                    "name": "Guardian world",
                    "type": "guardian",
                    "params": {"section": "world", "page_size": 20},
                    "api_key": "GUARDIAN_API_KEY",
                    "daily_request_budget": 200,
                    "profiles": ["market-monitor"],
                },
            ]
        },
    )

    id: str = Field(default="", description="Defaults to a slug of the name.")
    name: str
    type: SourceType = SourceType.RSS
    url: str | None = None
    enabled: bool = True
    refresh_minutes: int = 30
    tags: list[str] = Field(default_factory=list)
    aggregator: bool = False
    first_party: bool = False
    params: dict[str, Any] = Field(default_factory=dict)
    api_key: str | None = Field(
        default=None, description="Environment variable name (`GUARDIAN_API_KEY` or `${...}`)."
    )
    daily_request_budget: int | None = None
    max_posts_per_run: int | None = None
    monthly_budget_usd: float | None = None
    cost_per_read_usd: float | None = None
    profiles: list[str] | None = Field(
        default=None, description="Profile slugs; on update, omit to keep the current ones."
    )

    @field_validator("api_key")
    @classmethod
    def _env_name(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if value.startswith("${") and value.endswith("}"):
            value = value[2:-1]
        return value

    def to_source(self):
        data = self.model_dump(exclude={"profiles"}, exclude_none=True)
        return admin.build_source(data)


class DiscoverIn(BaseModel):
    url: str = Field(description="A site or feed URL.", examples=["https://www.theverge.com"])


class OpmlImportV1(admin.OpmlResult):
    pass


class CatalogEntryV1(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": "federal-reserve-press-releases",
                    "category": "Regulators",
                    "name": "Federal Reserve press releases",
                    "url": "https://www.federalreserve.gov/feeds/press_all.xml",
                    "type": "rss",
                    "tags": ["fed", "rates"],
                    "first_party": True,
                    "aggregator": False,
                    "note": None,
                }
            ]
        },
    )

    id: str
    category: str
    name: str
    url: str
    type: SourceType
    tags: list[str]
    first_party: bool
    aggregator: bool
    note: str | None


def _session(request: Request) -> Session:
    return request.app.state.session_factory()


def _raise(exc: admin.SourceError):
    raise HTTPException(status_code=exc.status_code, detail=str(exc))


def source_out(session: Session, record: SourceRecord) -> SourceAdminV1:
    import os

    health = admin.source_health(session, [record])[0]
    return SourceAdminV1(
        id=record.id,
        name=record.name,
        type=record.type,
        url=record.url,
        enabled=bool(record.enabled),
        refresh_minutes=record.refresh_minutes,
        tags=list(record.tags or []),
        aggregator=bool(record.aggregator),
        first_party=bool(record.first_party),
        params=dict(record.params or {}),
        api_key_env=record.api_key_env,
        api_key_set=bool(record.api_key_env and os.environ.get(record.api_key_env, "").strip()),
        limits=dict(record.limits or {}),
        removed_at=record.removed_at,
        profiles=health.profiles,
        health=_health_out(health),
    )


def _health_out(health: admin.SourceHealth) -> SourceHealthV1:
    return SourceHealthV1.model_validate({**health.__dict__, "state": health.state})


@router.get("/sources", response_model=list[SourceAdminV1], summary="List sources")
def list_sources(
    request: Request,
    include_removed: Annotated[bool, Query(description="Also list removed sources.")] = False,
) -> list[SourceAdminV1]:
    with _session(request) as session:
        records = admin.list_sources(session, include_removed=include_removed)
        return [source_out(session, r) for r in records]


@router.get(
    "/sources/health",
    response_model=list[SourceHealthV1],
    summary="Fetch health, budgets and spend of every source",
)
def sources_health(request: Request) -> list[SourceHealthV1]:
    with _session(request) as session:
        return [_health_out(h) for h in admin.source_health(session, admin.list_sources(session))]


@router.get("/source-catalog", response_model=list[CatalogEntryV1], summary="Starter feeds")
def source_catalog() -> list[CatalogEntryV1]:
    return [
        CatalogEntryV1.model_validate({**e.model_dump(), "id": e.id}) for e in admin.load_catalog()
    ]


@router.post(
    "/sources",
    response_model=SourceAdminV1,
    status_code=201,
    summary="Add a source (an identical existing source is shared instead)",
)
def create_source(request: Request, body: SourceIn, response: Response) -> SourceAdminV1:
    with _session(request) as session:
        try:
            source = body.to_source()
            record, created = admin.create_source(session, source, body.profiles or [])
        except admin.SourceError as exc:
            _raise(exc)
        if not created:
            response.status_code = 200
        out = source_out(session, record)
    admin.reschedule(request.app.state)
    return out


@router.post(
    "/sources/discover",
    response_model=list[admin.FeedCandidate],
    summary="Find the feeds of a site (HTML <link rel=alternate>) or confirm a feed URL",
)
async def discover(request: Request, body: DiscoverIn) -> list[admin.FeedCandidate]:
    async with request.app.state.ingestor.http_client() as client:
        try:
            return await admin.discover_feeds(client, body.url)
        except admin.SourceError as exc:
            _raise(exc)


@router.post(
    "/sources/test",
    response_model=admin.TestFetchResult,
    summary="Fetch a source once without saving anything",
)
async def test_fetch(request: Request, body: SourceIn) -> admin.TestFetchResult:
    try:
        source = body.to_source()
    except admin.SourceError as exc:
        _raise(exc)
    with _session(request) as session:
        async with request.app.state.ingestor.http_client() as client:
            return await admin.test_fetch(client, source, session)


@router.post(
    "/sources/sync-yaml",
    response_model=admin.SyncResult,
    summary="Add profiles, sources and topics from the YAML that the DB lacks (never deletes)",
)
def sync_yaml(request: Request) -> admin.SyncResult:
    path = Path(request.app.state.settings.config_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"config file {path} not found")
    try:
        config = load_config(path)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid config: {exc}") from None
    with _session(request) as session:
        result = admin.sync_from_yaml(session, config)
    admin.reschedule(request.app.state)
    return result


def _record(session: Session, source_id: str) -> SourceRecord:
    try:
        return admin.get_record(session, source_id)
    except admin.SourceError as exc:
        _raise(exc)


@router.get("/sources/{source_id}", response_model=SourceAdminV1, summary="One source")
def get_source(request: Request, source_id: str) -> SourceAdminV1:
    with _session(request) as session:
        return source_out(session, _record(session, source_id))


@router.put("/sources/{source_id}", response_model=SourceAdminV1, summary="Edit a source")
def update_source(request: Request, source_id: str, body: SourceIn) -> SourceAdminV1:
    with _session(request) as session:
        record = _record(session, source_id)
        try:
            source = body.to_source()
            admin.update_source(session, record, source, body.profiles)
        except admin.SourceError as exc:
            _raise(exc)
        out = source_out(session, record)
    admin.reschedule(request.app.state)
    return out


def _action(request: Request, source_id: str, action) -> SourceAdminV1:
    with _session(request) as session:
        record = _record(session, source_id)
        try:
            action(session, record)
        except admin.SourceError as exc:
            _raise(exc)
        out = source_out(session, record)
    admin.reschedule(request.app.state)
    return out


@router.delete(
    "/sources/{source_id}",
    response_model=SourceAdminV1,
    summary="Remove a source (stops fetching; its articles and stories are kept)",
)
def delete_source(request: Request, source_id: str) -> SourceAdminV1:
    return _action(request, source_id, admin.remove_source)


@router.post("/sources/{source_id}/enable", response_model=SourceAdminV1, summary="Enable")
def enable_source(request: Request, source_id: str) -> SourceAdminV1:
    return _action(request, source_id, lambda s, r: admin.set_enabled(s, r, True))


@router.post("/sources/{source_id}/disable", response_model=SourceAdminV1, summary="Disable")
def disable_source(request: Request, source_id: str) -> SourceAdminV1:
    return _action(request, source_id, lambda s, r: admin.set_enabled(s, r, False))


@router.post("/sources/{source_id}/restore", response_model=SourceAdminV1, summary="Undelete")
def restore_source(request: Request, source_id: str) -> SourceAdminV1:
    return _action(request, source_id, admin.restore_source)


@router.get(
    "/profiles/{slug}/sources.opml",
    summary="Export a profile's feeds as OPML",
    response_class=Response,
    responses={200: {"content": {"text/x-opml": {}}}},
)
def export_opml(request: Request, slug: str) -> Response:
    with _session(request) as session:
        profile = get_profile(session, slug)
        if profile is None:
            raise HTTPException(status_code=404, detail=f"unknown profile {slug!r}")
        text = admin.export_opml(profile)
    return Response(
        text,
        media_type="text/x-opml",
        headers={"Content-Disposition": f'attachment; filename="{slug}.opml"'},
    )


@router.post(
    "/profiles/{slug}/sources.opml",
    response_model=OpmlImportV1,
    summary="Import feeds from OPML (request body: the OPML document)",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"text/x-opml": {"schema": {"type": "string"}}},
        }
    },
)
async def import_opml(request: Request, slug: str) -> OpmlImportV1:
    text = (await request.body()).decode("utf-8", errors="replace")
    with _session(request) as session:
        profile = get_profile(session, slug)
        if profile is None:
            raise HTTPException(status_code=404, detail=f"unknown profile {slug!r}")
        try:
            result = admin.import_opml(session, profile, text)
        except admin.SourceError as exc:
            _raise(exc)
    admin.reschedule(request.app.state)
    return OpmlImportV1.model_validate(result.model_dump())
