"""Optional authentication, off by default.

``DISCOVER_API_KEYS`` (comma-separated): ``/api/v1`` and the legacy read API need
``Authorization: Bearer <key>`` or ``X-API-Key: <key>``. ``DISCOVER_ADMIN_KEY``:
``/api/admin/*``, topic changes and the Settings pages need the admin key, sent the same
way or as the signed HttpOnly cookie that ``/login`` sets. Keys are compared in constant
time and are never logged or stored."""

import hashlib
import hmac
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import quote

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

COOKIE = "discover_admin"
COOKIE_MAX_AGE = 30 * 24 * 3600

bearer = HTTPBearer(auto_error=False, description="`Authorization: Bearer <key>`")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="The API key.")


@dataclass(frozen=True)
class AuthConfig:
    api_keys: tuple[str, ...] = ()
    admin_key: str | None = None

    @classmethod
    def from_settings(cls, settings) -> "AuthConfig":
        raw = settings.api_keys.get_secret_value() if settings.api_keys else ""
        admin = settings.admin_key.get_secret_value().strip() if settings.admin_key else ""
        keys = tuple(k.strip() for k in raw.split(",") if k.strip())
        return cls(keys, admin or None)

    @property
    def api_enabled(self) -> bool:
        return bool(self.api_keys)

    @property
    def admin_enabled(self) -> bool:
        return self.admin_key is not None

    def describe(self) -> str:
        api = f"enabled ({len(self.api_keys)} key(s))" if self.api_enabled else "disabled"
        admin = "enabled" if self.admin_enabled else "disabled"
        return f"API key auth {api}; admin auth {admin}"


def keys_match(given: str | None, keys: Sequence[str]) -> bool:
    """Constant-time check of ``given`` against every key (no early exit)."""
    if not given or not keys:
        return False
    candidate = given.encode()
    found = False
    for key in keys:
        found |= hmac.compare_digest(candidate, key.encode())
    return found


def _signer(admin_key: str) -> bytes:
    return hashlib.sha256(b"discover-feed admin cookie\0" + admin_key.encode()).digest()


def make_cookie(admin_key: str, now: float | None = None) -> str:
    expires = int(now if now is not None else time.time()) + COOKIE_MAX_AGE
    sig = hmac.new(_signer(admin_key), str(expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def cookie_valid(admin_key: str, value: str | None, now: float | None = None) -> bool:
    expires, _, sig = (value or "").partition(".")
    if not expires.isdigit() or not sig:
        return False
    expected = hmac.new(_signer(admin_key), expires.encode(), hashlib.sha256).hexdigest()
    fresh = int(expires) > (now if now is not None else time.time())
    return hmac.compare_digest(sig.encode(), expected.encode()) and fresh


def _config(request: Request) -> AuthConfig:
    return getattr(request.app.state, "auth", None) or AuthConfig()


def presented_key(request: Request) -> str | None:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return request.headers.get("x-api-key") or None


def is_admin(request: Request) -> bool:
    auth = _config(request)
    if not auth.admin_enabled:
        return True
    admin = [auth.admin_key or ""]
    return keys_match(presented_key(request), admin) or cookie_valid(
        auth.admin_key or "", request.cookies.get(COOKIE)
    )


def _api_allowed(request: Request) -> bool:
    auth = _config(request)
    if not auth.api_enabled:
        return True
    if keys_match(presented_key(request), auth.api_keys):
        return True
    return auth.admin_enabled and is_admin(request)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})


async def require_api_key(
    request: Request,
    _bearer: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)] = None,
    _header: Annotated[str | None, Security(api_key_header)] = None,
) -> None:
    """``/api/v1`` dependency (the parameters only document the schemes in OpenAPI)."""
    if not _api_allowed(request):
        raise _unauthorized("missing or invalid API key")


def require_admin(request: Request) -> None:
    if not is_admin(request):
        raise _unauthorized("missing or invalid admin key")


_TOPIC_WRITE = re.compile(r"^/api/profiles/[^/]+/topics(/|$)")
_UI_LIVE = re.compile(r"^/api/profiles/[^/]+/(stream|published)$")


def legacy_api_guard(request: Request) -> None:
    """The unversioned ``/api`` router: admin and topic writes need the admin key; reads need
    an API key, except the two id-only endpoints the pages poll for live arrivals."""
    path = request.url.path
    if path.startswith("/api/admin/") or (request.method != "GET" and _TOPIC_WRITE.match(path)):
        require_admin(request)
    elif not _UI_LIVE.match(path) and not _api_allowed(request):
        raise _unauthorized("missing or invalid API key")


_SETTINGS = re.compile(r"^/(p/[^/]+/(settings|alerts)|demo/sink)")


def web_guard(request: Request) -> None:
    """Pages: Settings and every button that changes something need the admin cookie; a
    browser without it is sent to ``/login``."""
    if request.method == "GET" and not _SETTINGS.match(request.url.path):
        return
    if is_admin(request):
        return
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    login = f"/login?next={quote(target)}"
    if request.headers.get("hx-request"):
        raise HTTPException(status_code=401, detail="log in first", headers={"HX-Redirect": login})
    raise HTTPException(status_code=303, detail="log in first", headers={"Location": login})


def safe_next(value: str | None) -> str:
    """A local path to return to after login (never another site)."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value
