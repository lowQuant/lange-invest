"""Login (password + TOTP 2FA), logout, and request-access."""
from __future__ import annotations

import pyotp
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import auth
from app.auth import current_user
from app.users import get_user, verify_password

router = APIRouter()


def render(request: Request, name: str, **ctx):
    ctx["user"] = current_user(request)
    return request.app.state.templates.TemplateResponse(request, name, ctx)


def _safe_next(next_url: str | None) -> str:
    # Only allow local redirects (no open redirect).
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/"


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/"):
    if current_user(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    return render(request, "login.html", nav_active=None, next=_safe_next(next), error=None)


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, username: str = Form(...),
                       password: str = Form(...), next: str = Form("/")):
    stored = get_user(username)
    ok = stored is not None and verify_password(password, stored.pw_hash, stored.pw_salt)
    if not ok:
        return render(request, "login.html", nav_active=None, next=_safe_next(next),
                      error="Invalid username or password.")
    # Password OK → second factor. Carry identity in a short-lived signed cookie.
    resp = render(request, "totp.html", nav_active=None, next=_safe_next(next), error=None)
    resp.set_cookie(auth.PENDING_COOKIE, auth.issue_pending(username),
                    max_age=auth.PENDING_MAX_AGE, httponly=True, samesite="lax", secure=auth.cookie_secure())
    return resp


@router.post("/login/verify", response_class=HTMLResponse)
async def login_verify(request: Request, code: str = Form(...), next: str = Form("/")):
    pending = request.cookies.get(auth.PENDING_COOKIE)
    username = auth.read_pending(pending) if pending else None
    stored = get_user(username) if username else None
    if stored is None:
        return render(request, "login.html", nav_active=None, next=_safe_next(next),
                      error="Your sign-in expired. Please start again.")

    valid = pyotp.TOTP(stored.totp_secret).verify(code.strip().replace(" ", ""), valid_window=1)
    if not valid:
        resp = render(request, "totp.html", nav_active=None, next=_safe_next(next),
                      error="Incorrect code. Try again.")
        return resp

    resp = RedirectResponse(_safe_next(next), status_code=303)
    resp.set_cookie(auth.SESSION_COOKIE, auth.issue_session(username),
                    max_age=auth.SESSION_MAX_AGE, httponly=True, samesite="lax", secure=auth.cookie_secure())
    resp.delete_cookie(auth.PENDING_COOKIE)
    return resp


@router.post("/logout")
async def logout(request: Request):
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


@router.get("/request-access", response_class=HTMLResponse)
async def request_access(request: Request):
    return render(request, "request_access.html", nav_active=None)
