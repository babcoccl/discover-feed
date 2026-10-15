"""Rule evaluation and the exactly-once decision.

* ``on_ingested``: right after new articles are stored (title + feed summary), before
  extraction, clustering or any LLM work. ``on_extracted``: again for rules that search
  ``text``. ``on_clustered``: ``notify_on_update`` rules when a new source joins.
* Only articles published within ``alerts.alert_max_age_hours`` fire.
* Once per (rule, story lineage): a rule never fires twice for articles that are (now) in
  the same story, whatever merges happened; an article that is about to join a story that
  already alerted (predicted with the clusterer) is suppressed too. ``alert_firings`` has a
  UNIQUE (rule_id, lineage_id, kind, slot) so concurrent evaluations cannot double-fire.
"""

import copy
import html
import logging
import re
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app import story_views as views
from app.alerts import repo
from app.alerts.match import matched_keywords
from app.cluster.base import Clusterer, StoryDoc
from app.cluster.service import doc
from app.config import AlertsConfig, AppConfig
from app.events import record_alert
from app.labels import label, publisher_of, via_of
from app.models import (
    ENRICHMENT,
    AlertChannelRecord,
    AlertFiring,
    AlertRuleRecord,
    Article,
    Notification,
    NotificationKind,
    NotificationStatus,
    ProfileRecord,
    SourceRecord,
    Story,
    profile_sources,
    utcnow,
)

logger = logging.getLogger(__name__)
UPDATE_INTERVAL = timedelta(hours=1)
PREDICT_MARGIN = 0.8
"""Suppression predicts with a looser clustering threshold: at ingest only title + summary
are known, while the clusterer merges later with the extracted text as well."""
PREVIEW_DAYS = 7
_ACTIVE = (NotificationStatus.QUEUED, NotificationStatus.SENT, NotificationStatus.FAILED)
_TAG = re.compile(r"<[^>]+>")


def plain(text: str | None, length: int = 280) -> str:
    text = " ".join(html.unescape(_TAG.sub(" ", text or "")).split())
    if len(text) <= length:
        return text
    return text[:length].rsplit(" ", 1)[0].rstrip(",.;:") + "…"


def _minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def quiet_now(rule: AlertRuleRecord, timezone: str, now: datetime) -> bool:
    if not (rule.quiet_start and rule.quiet_end):
        return False
    local = now.astimezone(ZoneInfo(timezone))
    at, start, end = (
        local.hour * 60 + local.minute,
        _minutes(rule.quiet_start),
        _minutes(rule.quiet_end),
    )
    if start == end:
        return False
    return start <= at < end if start < end else at >= start or at < end


def local_time(now: datetime, timezone: str) -> str:
    return now.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M %Z")


class AlertEngine:
    def __init__(
        self,
        session_factory: sessionmaker,
        config: AppConfig,
        *,
        clusterer: Clusterer | None = None,
        clock: Callable[[], datetime] = utcnow,
        on_queued: Callable[[], None] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.config = config
        self.clusterer = clusterer
        self.clock = clock
        self.on_queued = on_queued
        self._lock = threading.Lock()

    def settings(self, slug: str) -> AlertsConfig:
        profile = self.config.get_profile(slug)
        return profile.alerts if profile is not None else AlertsConfig()

    # --- hooks ----------------------------------------------------------------------------

    def on_ingested(self, article_ids: Sequence[int]) -> list[int]:
        """Title + feed summary of just-stored articles. Returns the new firing ids."""
        return self._evaluate(article_ids, "ingest")

    def on_extracted(self, article_ids: Sequence[int]) -> list[int]:
        return self._evaluate(article_ids, "text")

    def on_clustered(self, story_ids: Iterable[int]) -> list[int]:
        """``notify_on_update`` rules: a new source joined a story that alerted."""
        story_ids = list(story_ids)
        if not story_ids:
            return []
        fired: list[int] = []
        with self._lock, self.session_factory() as session:
            now = self.clock()
            roots = {r for r in (views.resolve(session, s) for s in story_ids) if r is not None}
            for root in sorted(roots):
                fired += self._updates(session, root, now)
        self._kick(fired)
        return fired

    def _kick(self, fired: list[int]) -> None:
        if fired and self.on_queued is not None:
            self.on_queued()

    # --- evaluation -----------------------------------------------------------------------

    def _evaluate(self, article_ids: Sequence[int], stage: str) -> list[int]:
        if not article_ids:
            return []
        fired: list[int] = []
        with self._lock, self.session_factory() as session:
            now = self.clock()
            ids = [
                a.id
                for a in session.scalars(
                    select(Article)
                    .where(Article.id.in_(list(article_ids)))
                    .order_by(Article.published_at, Article.id)
                )
            ]
            for article_id in ids:
                article = session.get(Article, article_id)
                for rule_id, slug in self._rules_for(session, article.source_id):
                    rule = session.get(AlertRuleRecord, rule_id)
                    firing_id = self._consider(session, rule, slug, article, stage, now)
                    if firing_id is not None:
                        fired.append(firing_id)
                        article = session.get(Article, article_id)
        self._kick(fired)
        return fired

    def _rules_for(self, session: Session, source_id: str) -> list[tuple[int, str]]:
        stmt = (
            select(AlertRuleRecord.id, ProfileRecord.slug)
            .join(ProfileRecord, ProfileRecord.id == AlertRuleRecord.profile_id)
            .join(profile_sources, profile_sources.c.profile_id == ProfileRecord.id)
            .where(profile_sources.c.source_id == source_id, AlertRuleRecord.enabled.is_(True))
            .order_by(AlertRuleRecord.profile_id, AlertRuleRecord.position, AlertRuleRecord.id)
        )
        return [(row[0], row[1]) for row in session.execute(stmt)]

    def _consider(
        self, session: Session, rule: AlertRuleRecord, slug: str, article: Article, stage: str, now
    ) -> int | None:
        spec = repo.spec(rule)
        if stage == "ingest":
            fields = tuple(f for f in spec.match_in if f != "text")
        elif "text" in spec.match_in and article.text_status == "ok":
            fields = spec.match_in
        else:
            return None
        if not fields or article.story_id is None:
            return None
        cfg = self.settings(slug)
        if article.published_at < now - timedelta(hours=cfg.alert_max_age_hours):
            return None
        matched = matched_keywords(
            replace(spec, match_in=fields),
            source_id=article.source_id,
            fields={
                "title": article.title,
                "summary": plain(article.summary_raw, 10_000),
                "text": article.text,
            },
        )
        if matched is None:
            return None
        root = views.resolve(session, article.story_id)
        if root is None:
            return None
        members = _member_ids(session, root)
        if _fired(session, rule.id, members, (NotificationKind.ALERT,)):
            return None
        if self._joins_alerted_story(session, rule, article, root, members, now):
            logger.info(
                "alert %r: article %d joins an alerted story; not firing", rule.name, article.id
            )
            return None
        return self._fire(
            session, rule, slug, cfg, article, root, NotificationKind.ALERT, 0, matched, now
        )

    def _joins_alerted_story(self, session, rule, article, root, members, now) -> bool:
        """An article not clustered yet that the clusterer would put into a story this rule
        already alerted for (so a merge later cannot cause a second alert)."""
        if self.clusterer is None or members != [article.id]:
            return False
        since = now - self.clusterer.window
        fired_articles = session.scalars(
            select(AlertFiring.article_id).where(
                AlertFiring.rule_id == rule.id, AlertFiring.created_at >= since
            )
        )
        roots = set()
        for article_id in fired_articles:
            other = session.get(Article, article_id)
            if other is not None and other.story_id is not None:
                r = views.resolve(session, other.story_id)
                if r is not None and r != root:
                    roots.add(r)
        if not roots:
            return False
        stories = []
        for r in sorted(roots):
            rows = session.scalars(
                select(Article)
                .where(Article.story_id == r)
                .order_by(Article.published_at, Article.id)
            )
            stories.append(StoryDoc(id=r, members=[doc(a) for a in rows]))
        predictor = copy.copy(self.clusterer)
        if isinstance(getattr(predictor, "threshold", None), float):
            predictor.threshold *= PREDICT_MARGIN
        assignment = predictor.assign([doc(article)], stories)
        return bool(assignment) and assignment[0].story_id is not None

    def _updates(self, session: Session, root: int, now: datetime) -> list[int]:
        members = list(
            session.scalars(
                select(Article).where(Article.story_id == root, Article.origin != ENRICHMENT)
            )
        )
        if len(members) < 2:
            return []
        member_ids = [a.id for a in members]
        sources = {a.source_id for a in members}
        fired: list[int] = []
        rule_rows = {r for s in sources for r in self._rules_for(session, s)}
        for rule_id, slug in sorted(rule_rows):
            rule = session.get(AlertRuleRecord, rule_id)
            if rule is None or not rule.notify_on_update:
                continue
            firings = list(
                session.scalars(
                    select(AlertFiring).where(
                        AlertFiring.rule_id == rule.id, AlertFiring.article_id.in_(member_ids)
                    )
                )
            )
            if not any(f.kind == NotificationKind.ALERT for f in firings):
                continue
            cfg = self.settings(slug)
            notified = {f.source_id for f in firings}
            fresh = now - timedelta(hours=cfg.alert_max_age_hours)
            new = sorted(
                (
                    a
                    for a in members
                    if a.source_id not in notified
                    and (not rule.source_ids or a.source_id in rule.source_ids)
                    and a.published_at >= fresh
                ),
                key=lambda a: (a.published_at, a.id),
            )
            if not new:
                continue
            last = max(
                (f.created_at for f in firings if f.kind == NotificationKind.UPDATE), default=None
            )
            if last is not None and now - last < UPDATE_INTERVAL:
                continue
            slot = int(now.timestamp() // 3600)
            firing_id = self._fire(
                session, rule, slug, cfg, new[0], root, NotificationKind.UPDATE, slot, [], now
            )
            if firing_id is not None:
                fired.append(firing_id)
        return fired

    # --- firing ---------------------------------------------------------------------------

    def _fire(
        self, session, rule, slug, cfg, article, root, kind, slot, matched, now
    ) -> int | None:
        firing = AlertFiring(
            rule_id=rule.id,
            lineage_id=root,
            kind=kind.value,
            slot=slot,
            article_id=article.id,
            source_id=article.source_id,
            matched=matched,
            created_at=now,
        )
        session.add(firing)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            return None
        payload = build_payload(session, rule, slug, cfg, article, root, kind.value, matched, now)
        self._enqueue(session, rule, cfg, firing, payload, now)
        if kind is NotificationKind.ALERT:
            record_alert(
                session,
                slug,
                root,
                {
                    "rule": rule.name,
                    "rule_id": rule.id,
                    "article_id": article.id,
                    "matched": matched,
                },
            )
        try:
            session.commit()
        except IntegrityError:  # another process fired first
            session.rollback()
            return None
        logger.info(
            "alert %r fired (%s) for article %d, story %d", rule.name, kind.value, article.id, root
        )
        return firing.id

    def _enqueue(self, session, rule, cfg, firing, payload, now) -> None:
        channels = session.scalars(
            select(AlertChannelRecord).where(
                AlertChannelRecord.profile_id == rule.profile_id,
                AlertChannelRecord.key.in_(rule.channels or []),
                AlertChannelRecord.enabled.is_(True),
            )
        )
        for channel in channels:
            reason = None
            if quiet_now(rule, cfg.timezone, now):
                reason = "quiet_hours"
            elif _sent_last_hour(session, rule.id, channel.id, now) >= rule.max_per_hour:
                reason = "rate_limit"
            session.add(
                Notification(
                    profile_id=rule.profile_id,
                    rule_id=rule.id,
                    rule_name=rule.name,
                    firing_id=firing.id,
                    lineage_id=firing.lineage_id,
                    article_id=firing.article_id,
                    channel_id=channel.id,
                    channel_key=channel.key,
                    kind=firing.kind,
                    status=NotificationStatus.HELD if reason else NotificationStatus.QUEUED,
                    hold_reason=reason,
                    next_attempt_at=None if reason else now,
                    payload=payload,
                    created_at=now,
                    updated_at=now,
                )
            )
        session.flush()

    # --- digests and retention ------------------------------------------------------------

    def maintain(self) -> dict:
        """Send held notifications as one digest per rule+channel once quiet hours end / the
        hourly limit has room again; drop notifications older than the retention."""
        digests = pruned = 0
        with self._lock, self.session_factory() as session:
            now = self.clock()
            held = session.scalars(
                select(Notification)
                .where(Notification.status == NotificationStatus.HELD)
                .order_by(Notification.id)
            )
            groups: dict[tuple, list[Notification]] = {}
            for n in held:
                groups.setdefault((n.profile_id, n.rule_id, n.channel_id), []).append(n)
            for (profile_id, rule_id, channel_id), items in groups.items():
                profile = session.get(ProfileRecord, profile_id)
                rule = session.get(AlertRuleRecord, rule_id) if rule_id else None
                cfg = self.settings(profile.slug)
                if rule is not None and quiet_now(rule, cfg.timezone, now):
                    continue
                limit = rule.max_per_hour if rule is not None else 10
                if _sent_last_hour(session, rule_id, channel_id, now) >= limit:
                    continue
                digest = Notification(
                    profile_id=profile_id,
                    rule_id=rule_id,
                    rule_name=items[0].rule_name,
                    channel_id=channel_id,
                    channel_key=items[0].channel_key,
                    kind=NotificationKind.DIGEST,
                    status=NotificationStatus.QUEUED,
                    next_attempt_at=now,
                    payload=digest_payload(items, cfg, now),
                    created_at=now,
                    updated_at=now,
                )
                session.add(digest)
                session.flush()
                for n in items:
                    n.status, n.digest_id = NotificationStatus.DIGESTED, digest.id
                digests += 1
            for profile in session.scalars(select(ProfileRecord)):
                cutoff = now - timedelta(
                    days=self.settings(profile.slug).notification_retention_days
                )
                pruned += session.execute(
                    delete(Notification).where(
                        Notification.profile_id == profile.id, Notification.created_at < cutoff
                    )
                ).rowcount
                rule_ids = select(AlertRuleRecord.id).where(
                    AlertRuleRecord.profile_id == profile.id
                )
                session.execute(
                    delete(AlertFiring).where(
                        AlertFiring.rule_id.in_(rule_ids), AlertFiring.created_at < cutoff
                    )
                )
            session.commit()
        if digests and self.on_queued is not None:
            self.on_queued()
        return {"digests": digests, "pruned": pruned}

    # --- preview --------------------------------------------------------------------------

    def preview(
        self, session: Session, rule: AlertRuleRecord, days: int = PREVIEW_DAYS
    ) -> list[dict]:
        """Articles of the last ``days`` the rule matches (all its fields). Sends nothing."""
        now = self.clock()
        profile = session.get(ProfileRecord, rule.profile_id)
        source_ids = [s.id for s in profile.sources]
        spec = repo.spec(rule)
        rows = session.scalars(
            select(Article)
            .where(
                Article.source_id.in_(source_ids),
                Article.published_at >= now - timedelta(days=days),
            )
            .order_by(Article.published_at.desc(), Article.id.desc())
            .limit(2000)
        )
        fired_articles = set(
            session.scalars(select(AlertFiring.article_id).where(AlertFiring.rule_id == rule.id))
        )
        names = {s.id: s.name for s in profile.sources}
        out = []
        for article in rows:
            matched = matched_keywords(
                spec,
                source_id=article.source_id,
                fields={
                    "title": article.title,
                    "summary": plain(article.summary_raw, 10_000),
                    "text": article.text if article.text_status == "ok" else None,
                },
            )
            if matched is None:
                continue
            out.append(
                {
                    "article_id": article.id,
                    "story_id": views.resolve(session, article.story_id)
                    if article.story_id
                    else None,
                    "title": article.title,
                    "url": article.url,
                    "source": names.get(article.source_id, article.source_id),
                    "published_at": article.published_at,
                    "matched": matched,
                    "fired": article.id in fired_articles,
                }
            )
        return out


def _member_ids(session: Session, root: int) -> list[int]:
    return list(
        session.scalars(select(Article.id).where(Article.story_id == root).order_by(Article.id))
    )


def _fired(session: Session, rule_id: int, article_ids: Sequence[int], kinds) -> bool:
    return (
        session.scalar(
            select(AlertFiring.id)
            .where(
                AlertFiring.rule_id == rule_id,
                AlertFiring.article_id.in_(list(article_ids)),
                AlertFiring.kind.in_([k.value for k in kinds]),
            )
            .limit(1)
        )
        is not None
    )


def _sent_last_hour(session: Session, rule_id, channel_id, now: datetime) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.rule_id == rule_id,
                Notification.channel_id == channel_id,
                Notification.kind.in_([NotificationKind.ALERT, NotificationKind.UPDATE]),
                Notification.status.in_([s.value for s in _ACTIVE]),
                Notification.created_at >= now - timedelta(hours=1),
            )
        )
        or 0
    )


def build_payload(session, rule, slug, cfg, article, root, kind, matched, now) -> dict:
    """Channel-neutral message content; never contains secrets or unpublished story links."""
    source = session.get(SourceRecord, article.source_id)
    name = source.name if source is not None else article.source_id
    publisher = publisher_of(article.url, name)
    via = via_of(
        article.source_id, name, publisher, [source.id] if source and source.aggregator else []
    )
    story = session.get(Story, root)
    story_url, lead = None, None
    if story is not None and story.published_version is not None:
        story_url = f"{cfg.public_url}/story/{story.id}"
        published = views.published_story(session, story.id)
        if published is not None and published.brief is not None:
            lead = published.brief.lead or None
    return {
        "kind": kind,
        "test": False,
        "profile": slug,
        "rule": rule.name,
        "rule_id": rule.id,
        "priority": rule.priority,
        "title": article.title,
        "source": label(publisher, via),
        "publisher": publisher,
        "via": via,
        "new_source": label(publisher, via) if kind == NotificationKind.UPDATE else None,
        "snippet": plain(article.summary_raw),
        "matched": list(matched),
        "lead": lead,
        "url": story_url or article.url,
        "article_url": article.url,
        "story_url": story_url,
        "story_id": root,
        "article_id": article.id,
        "published_at": article.published_at.isoformat(),
        "time": now.isoformat(timespec="seconds"),
        "time_local": local_time(now, cfg.timezone),
    }


def digest_payload(items: Sequence[Notification], cfg: AlertsConfig, now: datetime) -> dict:
    first = items[0].payload or {}
    return {
        "kind": NotificationKind.DIGEST.value,
        "test": False,
        "profile": first.get("profile"),
        "rule": items[0].rule_name,
        "priority": first.get("priority", "default"),
        "title": f"{len(items)} alerts: {items[0].rule_name}",
        "items": [
            {
                "title": (n.payload or {}).get("title"),
                "source": (n.payload or {}).get("source"),
                "url": (n.payload or {}).get("url"),
                "time_local": (n.payload or {}).get("time_local"),
                "reason": n.hold_reason,
            }
            for n in items
        ],
        "url": first.get("url"),
        "time": now.isoformat(timespec="seconds"),
        "time_local": local_time(now, cfg.timezone),
    }
