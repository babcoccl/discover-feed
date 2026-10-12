"""The persistent send queue: ``notifications`` rows with status ``queued`` are delivered
with exponential backoff. A failing channel never blocks the others; rows already ``sent``
are never re-sent after a restart (receivers can also dedupe on ``X-Discover-Delivery`` /
``Message-ID``, both the notification id)."""

import logging
import smtplib
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.orm import sessionmaker

from app.alerts import repo
from app.alerts.channels import ChannelError, deliver
from app.alerts.engine import local_time
from app.config import AlertsConfig, AppConfig
from app.models import (
    AlertChannelRecord,
    Notification,
    NotificationKind,
    NotificationStatus,
    ProfileRecord,
    utcnow,
)

logger = logging.getLogger(__name__)


class AlertSender:
    def __init__(
        self,
        session_factory: sessionmaker,
        config: AppConfig,
        *,
        clock: Callable[[], datetime] = utcnow,
        transport: httpx.BaseTransport | None = None,
        smtp_factory: Callable[..., smtplib.SMTP] | None = None,
        backoff_seconds: float = 30.0,
        max_backoff_seconds: float = 3600.0,
    ) -> None:
        self.session_factory = session_factory
        self.config = config
        self.clock = clock
        self.transport = transport
        self.smtp_factory = smtp_factory
        self.backoff_seconds = backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._again = threading.Event()

    def settings(self, slug: str) -> AlertsConfig:
        profile = self.config.get_profile(slug)
        return profile.alerts if profile is not None else AlertsConfig()

    def kick(self) -> None:
        """Send what is due on a background thread (returns at once)."""
        self._again.set()
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._drain, name="alert-sender", daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        while self._again.is_set():
            self._again.clear()
            try:
                self.send_due()
            except Exception:
                logger.exception("alert sender failed")

    def wait(self, timeout: float = 10.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def send_due(self, limit: int = 200) -> dict[str, int]:
        counts = {"sent": 0, "retry": 0, "failed": 0}
        with self._lock:
            now = self.clock()
            with self.session_factory() as session:
                ids = list(
                    session.scalars(
                        select(Notification.id)
                        .where(
                            Notification.status == NotificationStatus.QUEUED,
                            or_(
                                Notification.next_attempt_at.is_(None),
                                Notification.next_attempt_at <= now,
                            ),
                        )
                        .order_by(Notification.id)
                        .limit(limit)
                    )
                )
            failing: set[int | None] = set()
            for notification_id in ids:
                with self.session_factory() as session:
                    n = session.get(Notification, notification_id)
                    if (
                        n is None
                        or n.status != NotificationStatus.QUEUED
                        or n.channel_id in failing
                    ):
                        continue
                    outcome = self._send_one(session, n, now)
                    counts[outcome] += 1
                    if outcome != "sent":
                        failing.add(n.channel_id)
        return counts

    def _send_one(self, session, n: Notification, now: datetime, *, final: bool = False) -> str:
        channel = session.get(AlertChannelRecord, n.channel_id) if n.channel_id else None
        profile = session.get(ProfileRecord, n.profile_id)
        max_attempts = 1 if final else self.settings(profile.slug).max_attempts
        try:
            if channel is None:
                raise ChannelError("channel was deleted")
            if not channel.enabled and n.kind != NotificationKind.TEST:
                raise ChannelError("channel is disabled")
            deliver(
                repo.channel_settings(channel),
                n.payload or {},
                n.id,
                transport=self.transport,
                smtp_factory=self.smtp_factory,
            )
        except (ChannelError, ValueError) as exc:
            error = str(exc).splitlines()[0][:500]
            n.attempts += 1
            n.last_error = error
            if channel is not None:
                channel.last_error, channel.last_error_at = error, now
            if n.attempts >= max_attempts or channel is None:
                n.status, n.next_attempt_at = NotificationStatus.FAILED, None
            else:
                delay = min(self.max_backoff_seconds, self.backoff_seconds * 2 ** (n.attempts - 1))
                n.next_attempt_at = now + timedelta(seconds=delay)
            n.updated_at = now
            session.commit()
            logger.warning(
                "notification %d via %s failed (attempt %d): %s",
                n.id,
                n.channel_key,
                n.attempts,
                error,
            )
            return "failed" if n.status == NotificationStatus.FAILED else "retry"
        n.attempts += 1
        n.status, n.sent_at, n.next_attempt_at, n.updated_at = (
            NotificationStatus.SENT,
            now,
            None,
            now,
        )
        n.last_error = None
        channel.last_sent_at = now
        session.commit()
        return "sent"

    def retry(self, notification_id: int) -> bool:
        """Re-queue a failed notification (fresh attempt budget)."""
        with self.session_factory() as session:
            n = session.get(Notification, notification_id)
            if (
                n is None
                or n.status != NotificationStatus.FAILED
                or n.kind == NotificationKind.TEST
            ):
                return False
            n.status, n.attempts, n.next_attempt_at = NotificationStatus.QUEUED, 0, self.clock()
            session.commit()
        self.kick()
        return True

    def send_test(self, channel_id: int) -> Notification:
        """Send a clearly marked test message now (one attempt) and log it."""
        now = self.clock()
        with self._lock, self.session_factory() as session:
            channel = session.get(AlertChannelRecord, channel_id)
            profile = session.get(ProfileRecord, channel.profile_id)
            cfg = self.settings(profile.slug)
            payload = {
                "kind": NotificationKind.TEST.value,
                "test": True,
                "profile": profile.slug,
                "rule": "Test",
                "priority": "default",
                "title": f"Test from Discover Feed ({profile.name})",
                "source": "discover-feed",
                "snippet": f"Channel {channel.name!r} ({channel.type}) is set up correctly.",
                "matched": [],
                "url": f"{cfg.public_url}/p/{profile.slug}/alerts",
                "time": now.isoformat(timespec="seconds"),
                "time_local": local_time(now, cfg.timezone),
            }
            n = Notification(
                profile_id=profile.id,
                rule_name="Test",
                channel_id=channel.id,
                channel_key=channel.key,
                kind=NotificationKind.TEST,
                status=NotificationStatus.QUEUED,
                payload=payload,
                created_at=now,
                updated_at=now,
            )
            session.add(n)
            session.commit()
            self._send_one(session, n, now, final=True)
            session.refresh(n)
            session.expunge(n)
            return n


def queue_depth(session) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.status == NotificationStatus.QUEUED)
        )
        or 0
    )
