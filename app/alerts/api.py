"""``/api/v1`` alert endpoints: reads need an API key (like the rest of v1); creating,
changing, deleting and previewing rules need the admin key."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import profiles as profiles_repo
from app.alerts import repo
from app.api_v1 import PREFIX
from app.auth import require_admin, require_api_key
from app.models import Notification, ProfileRecord

router = APIRouter(prefix=PREFIX, tags=["v1"], dependencies=[Depends(require_api_key)])
_ADMIN = [Depends(require_admin)]
_NOW = "2026-10-12T08:30:00Z"


class AlertKeywordV1(BaseModel):
    keyword: str = Field(min_length=1, description="Whole word or phrase; `$AAPL` -> `AAPL`.")
    case_sensitive: bool = False


class QuietHoursV1(BaseModel):
    start: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$", examples=["22:00"])
    end: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$", examples=["07:00"])


_RULE_EXAMPLE = {
    "name": "Big tech tickers",
    "enabled": True,
    "include": [{"keyword": "AAPL", "case_sensitive": True}],
    "exclude": [],
    "match": "any",
    "match_in": ["title", "summary"],
    "source_ids": [],
    "priority": "high",
    "channels": ["phone"],
    "quiet_hours": {"start": "22:00", "end": "07:00"},
    "max_per_hour": 10,
    "notify_on_update": False,
}


class AlertRuleIn(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [_RULE_EXAMPLE]})

    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    include: list[AlertKeywordV1] = Field(min_length=1)
    exclude: list[AlertKeywordV1] = []
    match: Literal["any", "all"] = "any"
    match_in: list[Literal["title", "summary", "text"]] = Field(
        default=["title", "summary"], min_length=1
    )
    source_ids: list[str] = Field(default=[], description="Empty: all the profile's sources.")
    priority: Literal["low", "default", "high"] = "default"
    channels: list[str] = Field(default=[], description="Channel keys of the profile.")
    quiet_hours: QuietHoursV1 | None = None
    max_per_hour: int = Field(default=10, ge=1, le=1000)
    notify_on_update: bool = False


class AlertRuleV1(AlertRuleIn):
    model_config = ConfigDict(json_schema_extra={"examples": [{"id": 3, **_RULE_EXAMPLE}]})

    id: int


class NotificationV1(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "id": 41,
                    "rule_id": 3,
                    "rule": "Big tech tickers",
                    "kind": "alert",
                    "status": "sent",
                    "hold_reason": None,
                    "channel": "phone",
                    "story_id": 1201,
                    "article_id": 5120,
                    "title": "Apple shares jump after earnings beat",
                    "url": "https://www.cnbc.com/2026/10/12/apple-earnings.html",
                    "matched": ["AAPL"],
                    "attempts": 1,
                    "last_error": None,
                    "created_at": _NOW,
                    "sent_at": _NOW,
                }
            ]
        }
    )

    id: int
    rule_id: int | None
    rule: str
    kind: Literal["alert", "update", "digest", "test"]
    status: Literal["queued", "sent", "failed", "held", "digested"]
    hold_reason: str | None = None
    channel: str
    story_id: int | None = Field(description="The story lineage the alert belongs to.")
    article_id: int | None
    title: str | None
    url: str | None = Field(description="Story page if published, else the article.")
    matched: list[str] = []
    attempts: int
    last_error: str | None
    created_at: datetime
    sent_at: datetime | None


class PreviewMatchV1(BaseModel):
    article_id: int
    story_id: int | None
    title: str
    url: str
    source: str
    published_at: datetime
    matched: list[str]
    fired: bool = Field(description="This article already fired the rule.")


class PreviewV1(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "rule_id": 3,
                    "days": 7,
                    "sent": False,
                    "matches": [
                        {
                            "article_id": 5120,
                            "story_id": 1201,
                            "title": "Apple shares jump after earnings beat",
                            "url": "https://www.cnbc.com/2026/10/12/apple-earnings.html",
                            "source": "CNBC Markets",
                            "published_at": _NOW,
                            "matched": ["AAPL"],
                            "fired": False,
                        }
                    ],
                }
            ]
        }
    )

    rule_id: int
    days: int
    sent: bool = Field(default=False, description="Always false: previews never notify.")
    matches: list[PreviewMatchV1]


def _session(request: Request) -> Session:
    return request.app.state.session_factory()


def _profile(session: Session, slug: str) -> ProfileRecord:
    profile = profiles_repo.get_profile(session, slug)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"unknown profile {slug!r}")
    return profile


def _out(rule) -> AlertRuleV1:
    return AlertRuleV1.model_validate(repo.rule_dict(rule))


def _save(session: Session, profile: ProfileRecord, body: AlertRuleIn, rule=None):
    try:
        rule = repo.save_rule(session, profile, body.model_dump(), rule)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    session.commit()
    return _out(rule)


@router.get("/profiles/{slug}/alert-rules", summary="A profile's alert rules")
def list_alert_rules(request: Request, slug: str) -> list[AlertRuleV1]:
    with _session(request) as session:
        return [_out(r) for r in repo.list_rules(session, _profile(session, slug))]


@router.post(
    "/profiles/{slug}/alert-rules",
    status_code=201,
    dependencies=_ADMIN,
    summary="Create an alert rule (admin)",
)
def create_alert_rule(request: Request, slug: str, body: AlertRuleIn) -> AlertRuleV1:
    with _session(request) as session:
        return _save(session, _profile(session, slug), body)


@router.put(
    "/profiles/{slug}/alert-rules/{rule_id}",
    dependencies=_ADMIN,
    summary="Replace an alert rule (admin)",
)
def update_alert_rule(request: Request, slug: str, rule_id: int, body: AlertRuleIn) -> AlertRuleV1:
    with _session(request) as session:
        profile = _profile(session, slug)
        rule = repo.get_rule(session, rule_id, profile)
        if rule is None:
            raise HTTPException(status_code=404, detail=f"unknown rule {rule_id}")
        return _save(session, profile, body, rule)


@router.delete(
    "/profiles/{slug}/alert-rules/{rule_id}",
    status_code=204,
    dependencies=_ADMIN,
    summary="Delete an alert rule (admin)",
)
def delete_alert_rule(request: Request, slug: str, rule_id: int) -> Response:
    with _session(request) as session:
        rule = repo.get_rule(session, rule_id, _profile(session, slug))
        if rule is None:
            raise HTTPException(status_code=404, detail=f"unknown rule {rule_id}")
        session.delete(rule)
        session.commit()
    return Response(status_code=204)


def notification_out(n: Notification) -> NotificationV1:
    payload = n.payload or {}
    return NotificationV1(
        id=n.id,
        rule_id=n.rule_id,
        rule=n.rule_name,
        kind=n.kind,
        status=n.status,
        hold_reason=n.hold_reason,
        channel=n.channel_key,
        story_id=n.lineage_id,
        article_id=n.article_id,
        title=payload.get("title"),
        url=payload.get("url"),
        matched=payload.get("matched") or [],
        attempts=n.attempts,
        last_error=n.last_error,
        created_at=n.created_at,
        sent_at=n.sent_at,
    )


def query_notifications(
    session: Session, profile: ProfileRecord, rule_id=None, status=None, limit: int = 100
) -> list[Notification]:
    stmt = select(Notification).where(Notification.profile_id == profile.id)
    if rule_id is not None:
        stmt = stmt.where(Notification.rule_id == rule_id)
    if status:
        stmt = stmt.where(Notification.status == status)
    return list(session.scalars(stmt.order_by(Notification.id.desc()).limit(limit)))


@router.get("/profiles/{slug}/notifications", summary="The notification log, newest first")
def list_notifications(
    request: Request,
    slug: str,
    rule_id: Annotated[int | None, Query(description="Only this rule.")] = None,
    status: Annotated[
        Literal["queued", "sent", "failed", "held", "digested"] | None,
        Query(description="Only this status."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[NotificationV1]:
    with _session(request) as session:
        profile = _profile(session, slug)
        return [
            notification_out(n)
            for n in query_notifications(session, profile, rule_id, status, limit)
        ]


@router.post(
    "/alert-rules/{rule_id}/preview",
    dependencies=_ADMIN,
    summary="Articles of the last 7 days a rule matches (sends nothing; admin)",
)
def preview_alert_rule(request: Request, rule_id: int) -> PreviewV1:
    with _session(request) as session:
        rule = repo.get_rule(session, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail=f"unknown rule {rule_id}")
        matches = request.app.state.alerts.preview(session, rule)
    return PreviewV1(rule_id=rule_id, days=7, matches=matches)
