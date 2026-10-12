"""Alert rules and channels in the DB: seeded once from YAML, then edited in the UI/API."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.alerts.match import RuleSpec, keywords, normalize
from app.config import SECRET_FIELDS, AlertChannel, AlertRule, AppConfig
from app.models import AlertChannelRecord, AlertRuleRecord, ProfileRecord, utcnow

_CONFIG_FIELDS = ("url", "server", "topic", "host", "port", "tls", "sender", "to")


def seed_alerts(session: Session, config: AppConfig, now: datetime | None = None) -> int:
    """Copy each profile's YAML ``alert_rules`` and ``alerts.channels`` into the DB once (also
    for DBs created before alerts existed). Returns the number of profiles seeded."""
    seeded = 0
    for record in session.scalars(
        select(ProfileRecord).where(ProfileRecord.alerts_seeded_at.is_(None))
    ):
        profile = config.get_profile(record.slug)
        if profile is not None:
            for channel in profile.alerts.channels:
                session.add(channel_record(record.id, channel))
            ids_by_name = {s.name: s.id for s in profile.sources}
            for position, rule in enumerate(profile.alert_rules):
                session.add(rule_record(record.id, rule, ids_by_name, position))
            seeded += 1
        record.alerts_seeded_at = now or utcnow()
    session.commit()
    return seeded


def channel_record(profile_id: int, channel: AlertChannel) -> AlertChannelRecord:
    record = AlertChannelRecord(profile_id=profile_id, key=channel.id)
    apply_channel(record, channel)
    return record


def apply_channel(record: AlertChannelRecord, channel: AlertChannel) -> None:
    data = channel.model_dump(mode="json")
    record.key = channel.id
    record.name = channel.name
    record.type = channel.type.value
    record.enabled = channel.enabled
    record.config = {k: data[k] for k in _CONFIG_FIELDS} | {
        "timeout_seconds": channel.timeout_seconds
    }
    record.secret_env = {k: data[k] for k in SECRET_FIELDS if data.get(k)}


def channel_settings(record: AlertChannelRecord) -> AlertChannel:
    """The validated channel (secret fields hold env var names, not values)."""
    return AlertChannel.model_validate(
        {
            "id": record.key,
            "type": record.type,
            "name": record.name,
            "enabled": record.enabled,
            **{k: v for k, v in (record.config or {}).items() if v is not None},
            **(record.secret_env or {}),
        }
    )


def rule_record(
    profile_id: int, rule: AlertRule, ids_by_name: dict[str, str], position: int = 0
) -> AlertRuleRecord:
    return AlertRuleRecord(
        profile_id=profile_id,
        name=rule.name.strip(),
        enabled=rule.enabled,
        include=normalize(rule.include),
        exclude=normalize(rule.exclude),
        match=rule.match.value,
        match_in=[f.value for f in rule.match_in],
        source_ids=[ids_by_name[n] for n in rule.sources],
        priority=rule.priority.value,
        channels=list(rule.channels),
        quiet_start=rule.quiet_hours.start if rule.quiet_hours else None,
        quiet_end=rule.quiet_hours.end if rule.quiet_hours else None,
        max_per_hour=rule.max_per_hour,
        notify_on_update=rule.notify_on_update,
        position=position,
    )


def spec(rule: AlertRuleRecord) -> RuleSpec:
    return RuleSpec(
        include=tuple(keywords(rule.include)),
        exclude=tuple(keywords(rule.exclude)),
        match_all=rule.match == "all",
        match_in=tuple(rule.match_in or ("title", "summary")),
        source_ids=tuple(rule.source_ids or ()),
    )


def list_rules(session: Session, profile: ProfileRecord) -> list[AlertRuleRecord]:
    return list(
        session.scalars(
            select(AlertRuleRecord)
            .where(AlertRuleRecord.profile_id == profile.id)
            .order_by(AlertRuleRecord.position, AlertRuleRecord.id)
        )
    )


def list_channels(session: Session, profile: ProfileRecord) -> list[AlertChannelRecord]:
    return list(
        session.scalars(
            select(AlertChannelRecord)
            .where(AlertChannelRecord.profile_id == profile.id)
            .order_by(AlertChannelRecord.key)
        )
    )


def get_rule(session: Session, rule_id: int, profile: ProfileRecord | None = None):
    rule = session.get(AlertRuleRecord, rule_id)
    if rule is None or (profile is not None and rule.profile_id != profile.id):
        return None
    return rule


def get_channel(session: Session, profile: ProfileRecord, channel_id: int):
    channel = session.get(AlertChannelRecord, channel_id)
    return channel if channel is not None and channel.profile_id == profile.id else None


def save_rule(
    session: Session, profile: ProfileRecord, data: dict, rule: AlertRuleRecord | None = None
) -> AlertRuleRecord:
    """Create or update a rule from validated fields (``source_ids`` / ``channels`` are ids
    and keys); raises ValueError for unknown references or a duplicate name."""
    name = " ".join(str(data.get("name", "")).split())
    if not name:
        raise ValueError("name is required")
    include = normalize(data.get("include") or [])
    if not include:
        raise ValueError("at least one keyword is required")
    known_sources = {s.id for s in profile.sources}
    unknown = set(data.get("source_ids") or []) - known_sources
    if unknown:
        raise ValueError(f"unknown sources: {sorted(unknown)}")
    known_channels = {c.key for c in list_channels(session, profile)}
    unknown = set(data.get("channels") or []) - known_channels
    if unknown:
        raise ValueError(f"unknown channels: {sorted(unknown)}")
    clash = session.scalar(
        select(AlertRuleRecord.id).where(
            AlertRuleRecord.profile_id == profile.id,
            func.lower(AlertRuleRecord.name) == name.lower(),
        )
    )
    if clash is not None and (rule is None or clash != rule.id):
        raise ValueError(f"a rule named {name!r} already exists")
    if rule is None:
        position = session.scalar(
            select(func.coalesce(func.max(AlertRuleRecord.position), -1)).where(
                AlertRuleRecord.profile_id == profile.id
            )
        )
        rule = AlertRuleRecord(profile_id=profile.id, position=position + 1)
        session.add(rule)
    quiet = data.get("quiet_hours") or None
    rule.name = name
    rule.enabled = bool(data.get("enabled", True))
    rule.include = include
    rule.exclude = normalize(data.get("exclude") or [])
    rule.match = str(data.get("match") or "any")
    rule.match_in = list(data.get("match_in") or ["title", "summary"])
    rule.source_ids = sorted(set(data.get("source_ids") or []))
    rule.priority = str(data.get("priority") or "default")
    rule.channels = list(dict.fromkeys(data.get("channels") or []))
    rule.quiet_start = quiet["start"] if quiet else None
    rule.quiet_end = quiet["end"] if quiet else None
    rule.max_per_hour = int(data.get("max_per_hour") or 10)
    rule.notify_on_update = bool(data.get("notify_on_update", False))
    rule.updated_at = utcnow()
    session.flush()
    return rule


def rule_dict(rule: AlertRuleRecord) -> dict:
    return {
        "id": rule.id,
        "name": rule.name,
        "enabled": rule.enabled,
        "include": rule.include,
        "exclude": rule.exclude,
        "match": rule.match,
        "match_in": rule.match_in,
        "source_ids": rule.source_ids,
        "priority": rule.priority,
        "channels": rule.channels,
        "quiet_hours": (
            {"start": rule.quiet_start, "end": rule.quiet_end} if rule.quiet_start else None
        ),
        "max_per_hour": rule.max_per_hour,
        "notify_on_update": rule.notify_on_update,
    }


def stats(session: Session, profile_id: int | None = None, now: datetime | None = None) -> dict:
    """Rule count, fired in 24 h, held, queue depth and failures / last error per channel."""
    from datetime import timedelta

    from app.models import AlertFiring, Notification

    now = now or utcnow()

    def scoped(stmt, model):
        return stmt.where(model.profile_id == profile_id) if profile_id is not None else stmt

    rules = session.scalar(
        scoped(
            select(func.count())
            .select_from(AlertRuleRecord)
            .where(AlertRuleRecord.enabled.is_(True)),
            AlertRuleRecord,
        )
    )
    fired = (
        select(func.count())
        .select_from(AlertFiring)
        .where(AlertFiring.created_at >= now - timedelta(hours=24))
    )
    if profile_id is not None:
        fired = fired.join(AlertRuleRecord, AlertRuleRecord.id == AlertFiring.rule_id).where(
            AlertRuleRecord.profile_id == profile_id
        )

    def count(status: str) -> int:
        stmt = select(func.count()).select_from(Notification).where(Notification.status == status)
        return session.scalar(scoped(stmt, Notification)) or 0

    failed = dict(
        session.execute(
            scoped(
                select(Notification.channel_id, func.count())
                .where(Notification.status == "failed")
                .group_by(Notification.channel_id),
                Notification,
            )
        ).all()
    )
    channels = [
        {
            "id": c.id,
            "key": c.key,
            "name": c.name,
            "type": c.type,
            "enabled": c.enabled,
            "failed": failed.get(c.id, 0),
            "last_error": c.last_error,
            "last_error_at": c.last_error_at,
            "last_sent_at": c.last_sent_at,
        }
        for c in session.scalars(
            scoped(select(AlertChannelRecord), AlertChannelRecord).order_by(
                AlertChannelRecord.profile_id, AlertChannelRecord.key
            )
        )
    ]
    return {
        "rules": rules or 0,
        "fired_24h": session.scalar(fired) or 0,
        "held": count("held"),
        "queued": count("queued"),
        "failed": count("failed"),
        "channels": channels,
    }


def badges(session: Session, profile: ProfileRecord, story_ids) -> dict[int, list[str]]:
    """Rule names that alerted for each (published) story, via its member articles."""
    from app.models import AlertFiring, Article

    story_ids = list(story_ids)
    if not story_ids:
        return {}
    rows = session.execute(
        select(Article.story_id, AlertRuleRecord.name)
        .join(AlertFiring, AlertFiring.article_id == Article.id)
        .join(AlertRuleRecord, AlertRuleRecord.id == AlertFiring.rule_id)
        .where(
            Article.story_id.in_(story_ids),
            AlertRuleRecord.profile_id == profile.id,
            AlertFiring.kind == "alert",
        )
        .distinct()
    )
    out: dict[int, list[str]] = {}
    for story_id, name in rows:
        out.setdefault(story_id, []).append(name)
    return {k: sorted(v) for k, v in out.items()}
