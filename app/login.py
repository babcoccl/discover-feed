"""``/login`` and ``/logout``: the admin key, exchanged for a signed HttpOnly cookie."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import profiles as repo
from app.auth import COOKIE, COOKIE_MAX_AGE, _config, keys_match, make_cookie, safe_next
from app.web import TEMPLATES

router = APIRouter(include_in_schema=False)


def _page(request: Request, next_: str, error: str | None = None, status: int = 200):
    with request.app.state.session_factory() as session:
        profiles = repo.list_profiles(session)
    context = {"profiles": profiles, "profile": None, "next": next_, "error": error}
    return TEMPLATES.TemplateResponse(request, "login.html", context, status_code=status)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/") -> Response:
    return _page(request, safe_next(next))


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request) -> Response:
    form = await request.form()
    target = safe_next(str(form.get("next") or "/"))
    auth = _config(request)
    if not auth.admin_enabled:
        return RedirectResponse(target, status_code=303)
    if not keys_match(str(form.get("key") or ""), [auth.admin_key or ""]):
        return _page(request, target, "That key is not the admin key.", status=401)
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        COOKIE,
        make_cookie(auth.admin_key or ""),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/",
    )
    return response


@router.post("/logout")
def logout() -> Response:
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(COOKIE, path="/")
    return response
