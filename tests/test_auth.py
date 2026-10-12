"""Optional API keys and admin key: /api/v1, legacy API, admin routes, /login cookie."""

import hmac
import logging

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import auth
from app.demo import build_demo
from app.main import create_app

KEYS = "client-key-1, client-key-2"
ADMIN = "admin-secret-key"
SLUG = "personal-reader"


@pytest.fixture(scope="module")
def build(tmp_path_factory):
    return build_demo(tmp_path_factory.mktemp("auth") / "demo.db")


def _client(build, **update):
    settings = build.settings.model_copy(update=update)
    return TestClient(create_app(settings, llm_transport=build.llm_transport))


@pytest.fixture(scope="module")
def locked(build):
    with _client(build, api_keys=SecretStr(KEYS), admin_key=SecretStr(ADMIN)) as c:
        yield c


def test_auth_is_off_by_default(build) -> None:
    with _client(build) as c:
        assert c.get(f"/api/v1/profiles/{SLUG}/feed").status_code == 200
        assert c.get(f"/p/{SLUG}/settings").status_code == 200
        assert c.get("/api/admin/articles/1").status_code in (200, 404)


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 401),
        ({"Authorization": "Bearer wrong"}, 401),
        ({"X-API-Key": "wrong"}, 401),
        ({"Authorization": "Bearer client-key-1"}, 200),
        ({"X-API-Key": "client-key-2"}, 200),
        ({"X-API-Key": ADMIN}, 200),
    ],
)
def test_v1_needs_an_api_key(locked, headers, status) -> None:
    for path in (f"/api/v1/profiles/{SLUG}/feed", "/api/v1/health", "/api/v1/profiles"):
        response = locked.get(path, headers=headers)
        assert response.status_code == status, path
        if status == 401:
            assert response.headers["www-authenticate"] == "Bearer"
    stream = locked.get("/api/v1/stream?profile=nope", headers=headers)
    assert stream.status_code == (401 if status == 401 else 404)


def test_legacy_api_and_admin_routes(locked) -> None:
    key = {"X-API-Key": "client-key-1"}
    assert locked.get(f"/api/profiles/{SLUG}/feed").status_code == 401
    assert locked.get(f"/api/profiles/{SLUG}/feed", headers=key).status_code == 200
    since = "2000-01-01T00:00:00Z"
    assert locked.get(f"/api/profiles/{SLUG}/published?since={since}").status_code == 200
    assert locked.get("/api/admin/articles/1", headers=key).status_code == 401
    admin = {"Authorization": f"Bearer {ADMIN}"}
    assert locked.get("/api/admin/articles/999999", headers=admin).status_code == 404
    body = {"name": "Auth test"}
    assert locked.post(f"/api/profiles/{SLUG}/topics", json=body, headers=key).status_code == 401
    created = locked.post(f"/api/profiles/{SLUG}/topics", json=body, headers=admin)
    assert created.status_code == 201
    topic = created.json()["id"]
    assert locked.delete(f"/api/profiles/{SLUG}/topics/{topic}", headers=admin).status_code == 204


def test_settings_redirect_to_login_and_cookie_flow(build) -> None:
    with _client(build, admin_key=SecretStr(ADMIN)) as c:
        page = c.get(f"/p/{SLUG}/settings", follow_redirects=False)
        assert page.status_code == 303
        assert page.headers["location"] == f"/login?next=/p/{SLUG}/settings"
        pause = c.post(f"/p/{SLUG}/settings/pipeline/pause", headers={"HX-Request": "true"})
        assert pause.status_code == 401 and pause.headers["hx-redirect"].startswith("/login")
        assert c.get(f"/p/{SLUG}").status_code == 200  # reading stays open
        assert c.get("/login").status_code == 200
        wrong = c.post("/login", data={"key": "nope", "next": "/"}, follow_redirects=False)
        assert wrong.status_code == 401 and "auth" not in c.cookies
        ok = c.post(
            "/login",
            data={"key": ADMIN, "next": f"/p/{SLUG}/settings"},
            follow_redirects=False,
        )
        assert ok.status_code == 303 and ok.headers["location"] == f"/p/{SLUG}/settings"
        cookie = ok.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie and ADMIN not in cookie
        assert c.get(f"/p/{SLUG}/settings").status_code == 200
        pause = c.post(f"/p/{SLUG}/settings/pipeline/pause", headers={"HX-Request": "true"})
        assert pause.status_code == 200
        c.post(f"/p/{SLUG}/settings/pipeline/resume", headers={"HX-Request": "true"})
        assert c.get("/api/admin/articles/999999").status_code == 404  # cookie works for API
        value = c.cookies[auth.COOKIE]
        c.cookies.set(auth.COOKIE, value[:-1] + ("1" if value.endswith("0") else "0"))
        assert c.get(f"/p/{SLUG}/settings", follow_redirects=False).status_code == 303
        evil = c.post(
            "/login", data={"key": ADMIN, "next": "//evil.example"}, follow_redirects=False
        )
        assert evil.headers["location"] == "/"


def test_cookie_signing() -> None:
    value = auth.make_cookie(ADMIN, now=1000)
    assert auth.cookie_valid(ADMIN, value, now=1001)
    assert not auth.cookie_valid(ADMIN, value, now=1000 + auth.COOKIE_MAX_AGE + 1)
    assert not auth.cookie_valid("other", value, now=1001)
    assert not auth.cookie_valid(ADMIN, "123.abc", now=1)
    assert not auth.cookie_valid(ADMIN, None)


def test_keys_are_compared_in_constant_time(monkeypatch) -> None:
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    assert auth.keys_match("k1", ["k1", "k2", "k3"])
    assert len(calls) == 3  # no early exit on the first match
    assert not auth.keys_match("", ["k1"]) and not auth.keys_match("x", [])


def test_startup_logs_auth_state_without_keys(build, caplog) -> None:
    client = _client(build, api_keys=SecretStr(KEYS), admin_key=SecretStr(ADMIN))
    with caplog.at_level(logging.INFO, logger="discover_feed"), client as c:
        c.get("/api/v1/health", headers={"X-API-Key": "client-key-1"})
    assert "API key auth enabled (2 key(s)); admin auth enabled" in caplog.text
    assert "client-key" not in caplog.text and ADMIN not in caplog.text
