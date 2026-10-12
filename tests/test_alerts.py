"""Keyword alerts: matching, ingest-time firing, exactly-once across merges, quiet hours,
rate limits, delivery to a local fake webhook / ntfy / SMTP sink, retries, API and auth."""

import threading
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select

from app.alerts import repo
from app.alerts.channels import redact
from app.alerts.match import RuleSpec, matched_keywords, normalize
from app.alerts.match import keywords as _keywords
from app.alerts.sender import AlertSender
from app.alerts.sink import FakeSink
from app.config import AlertKeyword, load_config
from app.lifecycle import ensure_stories
from app.main import create_app
from app.models import (
    AlertFiring,
    Article,
    Notification,
    Story,
    StoryEventRow,
    StoryState,
)
from app.profiles import get_profile
from tests.conftest import EXAMPLE_CONFIG
from tests.pipeline_helpers import T0, add_article


def kw(items):
    return _keywords(normalize(items))


NOW = T0 + timedelta(hours=1)
SECRET = "s3cret-webhook-key"
SLUG = "market-monitor"


@pytest.fixture
def sink(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_SECRET", SECRET)
    with FakeSink(webhook_secret=SECRET) as s:
        yield s


@pytest.fixture
def app(settings, sink):
    app = create_app(settings)
    with TestClient(app):
        app.state.alerts.clock = lambda: NOW
        app.state.alerts.on_queued = None
        app.state.alert_sender.clock = lambda: NOW
        app.state.alert_sender.backoff_seconds = 0
        with app.state.session_factory() as session:
            profile = get_profile(session, SLUG)
            for c in repo.list_channels(session, profile):
                if c.type == "webhook":
                    c.config = {**c.config, "url": sink.webhook_url}
                elif c.type == "ntfy":
                    c.config = {**c.config, "server": sink.ntfy_server}
                    c.secret_env = {}
                else:
                    c.config = {
                        **c.config,
                        "host": sink.host,
                        "port": sink.smtp_port,
                        "tls": "none",
                    }
                    c.secret_env = {}
            session.commit()
        yield app


def _add(app, url, title, source="sec-press-releases", hours=0.5, summary="", text=None) -> int:
    sf = app.state.session_factory
    article_id = add_article(
        sf, url=url, title=title, summary=summary, source_id=source, hours=hours, text=text
    )
    with sf() as session:
        ensure_stories(session, NOW)
        session.commit()
    return article_id


def _count(app, model, *where) -> int:
    with app.state.session_factory() as session:
        return session.scalar(select(func.count()).select_from(model).where(*where))


def _rule(app, name):
    with app.state.session_factory() as session:
        return next(
            r for r in repo.list_rules(session, get_profile(session, SLUG)) if r.name == name
        )


# --- matching ---------------------------------------------------------------------------


def test_ticker_is_case_sensitive_and_dollar_is_normalized() -> None:
    assert AlertKeyword.model_validate("$AAPL").model_dump() == {
        "keyword": "AAPL",
        "case_sensitive": True,
    }
    rule = RuleSpec(include=kw(["$AAPL", "rate cut"]))
    match = lambda title: matched_keywords(rule, source_id="x", fields={"title": title})  # noqa: E731
    assert match("Apple (AAPL) jumps") == ["AAPL"]
    assert match("Buy $AAPL now") == ["AAPL"]
    assert match("the aapl meme") is None
    assert match("Fed RATE CUT ahead") == ["rate cut"]
    assert match("AAPLX listing") is None


def test_excludes_win_and_sources_restrict() -> None:
    rule = RuleSpec(include=kw(["settlement"]), exclude=kw(["proposed rule"]), source_ids=["sec"])
    assert matched_keywords(rule, source_id="sec", fields={"title": "SEC settlement"})
    assert not matched_keywords(
        rule, source_id="sec", fields={"title": "Proposed rule on settlement"}
    )
    assert not matched_keywords(rule, source_id="cnbc", fields={"title": "SEC settlement"})
    all_rule = RuleSpec(include=kw(["fed", "cut"]), match_all=True)
    assert matched_keywords(all_rule, source_id="x", fields={"title": "Fed cut"}) == ["fed", "cut"]
    assert matched_keywords(all_rule, source_id="x", fields={"title": "Fed holds"}) is None


def test_channel_secrets_stay_env_references(monkeypatch) -> None:
    monkeypatch.setenv("NTFY_TOKEN", "tk-should-not-be-loaded")
    channels = {c.id: c for c in load_config(EXAMPLE_CONFIG).get_profile(SLUG).alerts.channels}
    assert channels["phone"].token == "NTFY_TOKEN"
    assert channels["webhook"].secret == "ALERT_WEBHOOK_SECRET"
    assert redact("auth failed for tk-should-not-be-loaded", {"t": "tk-should-not-be-loaded"}) == (
        "auth failed for ***"
    )


# --- firing -----------------------------------------------------------------------------


def test_ingest_fires_once_and_delivers_signed_webhook_and_ntfy(app, sink) -> None:
    article_id = _add(app, "https://sec.gov/1", "SEC charges trader with fraud")
    fired = app.state.alerts.on_ingested([article_id])
    assert len(fired) == 1
    assert app.state.alerts.on_ingested([article_id]) == []  # exactly once
    assert app.state.alert_sender.send_due()["sent"] == 2
    (hook,) = sink.received("webhook")
    (ntfy,) = sink.received("ntfy")
    assert hook["signature_ok"] is True
    assert hook["body"]["matched"] == ["SEC charges"]
    assert hook["body"]["story_url"] is None  # unpublished: original article link only
    assert hook["body"]["url"] == "https://sec.gov/1"
    assert SECRET not in str(hook["body"])
    assert ntfy["title"] == "SEC charges trader with fraud"
    assert ntfy["click"] == "https://sec.gov/1"
    with app.state.session_factory() as session:
        event = session.scalars(
            select(StoryEventRow).where(StoryEventRow.type == "alert.fired")
        ).one()
        assert event.data["rule"] == "SEC enforcement" and event.data["article_id"] == article_id
    assert app.state.alert_sender.send_due()["sent"] == 0  # restart-safe: never re-sent
    assert AlertSender(app.state.session_factory, app.state.config).send_due()["sent"] == 0


def test_old_articles_and_disabled_rules_do_not_fire(app) -> None:
    old = _add(app, "https://sec.gov/old", "SEC charges old case", hours=-12)
    assert app.state.alerts.on_ingested([old]) == []
    with app.state.session_factory() as session:
        rule = session.merge(_rule(app, "SEC enforcement"))
        rule.enabled = False
        session.commit()
    fresh = _add(app, "https://sec.gov/new", "SEC charges new case")
    assert app.state.alerts.on_ingested([fresh]) == []


def test_text_rules_fire_after_extraction(app) -> None:
    article_id = _add(app, "https://cnbc.com/t", "Apple supplier news", source="cnbc-markets")
    assert app.state.alerts.on_ingested([article_id]) == []
    with app.state.session_factory() as session:
        article = session.get(Article, article_id)
        article.text, article.text_status = "Shares of AAPL rose after the report.", "ok"
        session.commit()
    assert len(app.state.alerts.on_extracted([article_id])) == 1


def test_exactly_once_across_merge_and_redirect(app) -> None:
    first = _add(
        app, "https://fed.gov/1", "FOMC announces rate cut", source="federal-reserve-press-releases"
    )
    second = _add(app, "https://cnbc.com/1", "Fed rate cut lands", source="cnbc-markets")
    assert len(app.state.alerts.on_ingested([first])) == 1
    with app.state.session_factory() as session:  # the clusterer merges the two stories
        a, b = session.get(Article, first), session.get(Article, second)
        old = session.get(Story, b.story_id)
        b.story_id = a.story_id
        old.state, old.merged_into = StoryState.MERGED.value, a.story_id
        session.commit()
    assert app.state.alerts.on_ingested([second]) == []
    third = _add(app, "https://cnbc.com/2", "Another rate cut take", source="cnbc-markets")
    with app.state.session_factory() as session:
        target = session.get(Article, first).story_id
        session.get(Article, third).story_id = target
        session.commit()
    assert app.state.alerts.on_ingested([third]) == []
    assert _count(app, AlertFiring) == 1


def test_unique_conflict_is_not_a_second_alert(app) -> None:
    article_id = _add(app, "https://sec.gov/u", "SEC charges two")
    other = _add(app, "https://sec.gov/u2", "unrelated")
    rule = _rule(app, "SEC enforcement")
    with app.state.session_factory() as session:
        lineage = session.get(Article, article_id).story_id
        session.add(
            AlertFiring(
                rule_id=rule.id,
                lineage_id=lineage,
                kind="alert",
                slot=0,
                article_id=other,
                source_id="sec-press-releases",
                matched=[],
                created_at=NOW,
            )
        )
        session.commit()
    assert app.state.alerts.on_ingested([article_id]) == []
    assert _count(app, Notification) == 0


def test_concurrent_evaluations_fire_once(app) -> None:
    from app.alerts.engine import AlertEngine

    ids = [_add(app, f"https://sec.gov/c{i}", "SEC charges parallel case") for i in range(1)]
    engines = [
        AlertEngine(app.state.session_factory, app.state.config, clock=lambda: NOW)
        for _ in range(4)
    ]
    results: list = []
    threads = [
        threading.Thread(target=lambda e=e: results.append(e.on_ingested(ids))) for e in engines
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(len(r) for r in results) == 1
    assert _count(app, AlertFiring) == 1


def test_update_notification_is_rate_limited(app) -> None:
    first = _add(app, "https://sec.gov/up", "SEC charges bank")
    assert len(app.state.alerts.on_ingested([first])) == 1
    with app.state.session_factory() as session:
        lineage = session.get(Article, first).story_id
        for i, src in enumerate(["cnbc-markets", "federal-reserve-press-releases"]):
            session.add(
                Article(
                    source_id=src,
                    url=f"https://x/{i}",
                    canonical_url=f"https://x/{i}",
                    title="Bank charged",
                    summary_raw="",
                    published_at=NOW - timedelta(minutes=5),
                    fetched_at=NOW,
                    content_hash=f"h{i}",
                    raw_json={},
                    story_id=lineage,
                )
            )
            session.commit()
            assert len(app.state.alerts.on_clustered([lineage])) == (1 if i == 0 else 0)
    assert _count(app, AlertFiring, AlertFiring.kind == "update") == 1


# --- holding, digests, rate limits ------------------------------------------------------


def test_quiet_hours_hold_then_digest_by_email(app, sink) -> None:
    with app.state.session_factory() as session:
        rule = session.merge(_rule(app, "Big tech tickers"))
        rule.quiet_start, rule.quiet_end = "12:00", "14:00"  # NOW is 13:00 UTC
        session.commit()
    article_id = _add(
        app, "https://cnbc.com/aapl", "Apple (AAPL) hits record", source="cnbc-markets"
    )
    app.state.alerts.on_ingested([article_id])
    assert _count(app, Notification, Notification.status == "held") == 1
    assert app.state.alerts.maintain()["digests"] == 0
    later = NOW + timedelta(hours=2)
    app.state.alerts.clock = app.state.alert_sender.clock = lambda: later
    assert app.state.alerts.maintain()["digests"] == 1
    assert app.state.alert_sender.send_due()["sent"] == 1
    (mail,) = sink.received("email")
    assert mail["has_html"] and "Apple (AAPL) hits record" in mail["text"]


def test_per_hour_limit_holds_overflow(app) -> None:
    with app.state.session_factory() as session:
        session.merge(_rule(app, "SEC enforcement")).max_per_hour = 1
        session.commit()
    for i in range(2):
        app.state.alerts.on_ingested(
            [
                _add(
                    app,
                    f"https://sec.gov/r{i}",
                    ["SEC charges bank over loans", "Crypto exchange settlement reached"][i],
                )
            ]
        )
    held = _count(app, Notification, Notification.hold_reason == "rate_limit")
    assert held == 2  # second alert, on both of the rule's channels


# --- delivery failures ------------------------------------------------------------------


def test_failures_retry_with_backoff_then_fail_and_isolate(app, sink) -> None:
    sink.fail = {"webhook": 100}
    app.state.alerts.on_ingested([_add(app, "https://sec.gov/f", "SEC charges failure")])
    for _ in range(6):
        app.state.alert_sender.send_due()
    with app.state.session_factory() as session:
        statuses = {
            n.channel_key: (n.status, n.attempts) for n in session.scalars(select(Notification))
        }
    assert statuses["webhook"] == ("failed", 5)
    assert statuses["phone"][0] == "sent"  # the failing channel does not block ntfy
    sink.fail = {}
    failed_id = _count(app, Notification)  # ids: highest is one of them; look it up instead
    with app.state.session_factory() as session:
        failed_id = session.scalar(select(Notification.id).where(Notification.status == "failed"))
    assert app.state.alert_sender.retry(failed_id)
    app.state.alert_sender.wait()
    assert len(sink.received("webhook")) == 1


def test_timeout_is_a_failure(app, sink) -> None:
    with app.state.session_factory() as session:
        for c in repo.list_channels(session, get_profile(session, SLUG)):
            c.config = {**c.config, "timeout_seconds": 1}
        session.commit()
    sink.delay = 1.5
    app.state.alerts.on_ingested([_add(app, "https://sec.gov/slow", "SEC charges slow")])
    counts = app.state.alert_sender.send_due()
    assert counts["retry"] >= 1
    with app.state.session_factory() as session:
        errors = [n.last_error for n in session.scalars(select(Notification))]
    assert any(e and "timeout" in e.lower() for e in errors)


def test_send_test_is_marked_and_logged(app, sink) -> None:
    with app.state.session_factory() as session:
        channel = next(
            c for c in repo.list_channels(session, get_profile(session, SLUG)) if c.type == "email"
        )
    result = app.state.alert_sender.send_test(channel.id)
    assert result.status == "sent" and result.kind == "test"
    (mail,) = sink.received("email")
    assert "Test" in mail["title"]


# --- API, UI, auth ----------------------------------------------------------------------


def test_api_crud_and_preview_sends_nothing(app) -> None:
    with TestClient(app) as c:
        app.state.alerts.clock = lambda: NOW
        rules = c.get(f"/api/v1/profiles/{SLUG}/alert-rules").json()
        assert {r["name"] for r in rules} >= {"Rate decisions", "SEC enforcement"}
        body = {"name": "Oil", "include": [{"keyword": "OPEC"}], "channels": ["phone"]}
        created = c.post(f"/api/v1/profiles/{SLUG}/alert-rules", json=body)
        assert created.status_code == 201
        rule_id = created.json()["id"]
        updated = c.put(
            f"/api/v1/profiles/{SLUG}/alert-rules/{rule_id}", json={**body, "priority": "high"}
        )
        assert updated.json()["priority"] == "high"
        _add(app, "https://cnbc.com/opec", "OPEC cuts output", source="cnbc-markets")
        preview = c.post(f"/api/v1/alert-rules/{rule_id}/preview").json()
        assert [m["title"] for m in preview["matches"]] == ["OPEC cuts output"]
        assert preview["sent"] is False
        assert _count(app, Notification) == 0 and _count(app, AlertFiring) == 0
        assert c.get(f"/api/v1/profiles/{SLUG}/notifications").json() == []
        assert c.delete(f"/api/v1/profiles/{SLUG}/alert-rules/{rule_id}").status_code == 204
        page = c.get(f"/p/{SLUG}/alerts")
        assert page.status_code == 200 and "Notification log" in page.text


def test_alert_routes_require_keys(settings, sink) -> None:
    settings = settings.model_copy(
        update={"api_keys": SecretStr("reader-key"), "admin_key": SecretStr("admin-key")}
    )
    with TestClient(create_app(settings)) as c:
        url = f"/api/v1/profiles/{SLUG}/alert-rules"
        assert c.get(url).status_code == 401
        assert c.get(url, headers={"Authorization": "Bearer reader-key"}).status_code == 200
        body = {"name": "X", "include": [{"keyword": "x"}]}
        assert c.post(
            url, json=body, headers={"Authorization": "Bearer reader-key"}
        ).status_code in (401, 403)
        assert (
            c.post(url, json=body, headers={"Authorization": "Bearer admin-key"}).status_code == 201
        )
        page = c.get(f"/p/{SLUG}/alerts", follow_redirects=False)
        assert page.status_code in (302, 303, 401)


def test_story_url_only_once_published(app, sink) -> None:
    article_id = _add(app, "https://sec.gov/p", "SEC charges published case")
    with app.state.session_factory() as session:
        session.get(Story, session.get(Article, article_id).story_id).published_version = 1
        session.commit()
    app.state.alerts.on_ingested([article_id])
    app.state.alert_sender.send_due()
    (hook,) = sink.received("webhook")
    assert hook["body"]["story_url"].endswith(f"/story/{hook['body']['story_id']}")
