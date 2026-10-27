"""Login per code (form or QR link ?c=...), logout."""

import hmac
import secrets

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session as DbSession

from app import auth, stats
from app.db import get_db
from app.models import User
from app.templating import render
from app.i18n import N_, _

router = APIRouter()


def _client_ip(request: Request) -> str:
    # uvicorn --proxy-headers resolves X-Forwarded-For for trusted proxies only.
    return request.client.host if request.client else "unknown"


def _login_success(request: Request, db: DbSession, user: User, tasks: BackgroundTasks):
    raw = auth.create_session(db, user, request.headers.get("user-agent"))
    response = RedirectResponse("/", status_code=303)
    auth.set_session_cookie(response, raw)
    response.delete_cookie(auth.LOGIN_CSRF_COOKIE)
    tasks.add_task(stats.update_after_login, user.id)
    return response


def _login_form(request: Request, error: str | None = None, status_code: int = 200):
    token = request.cookies.get(auth.LOGIN_CSRF_COOKIE) or secrets.token_urlsafe(24)
    response = render(
        request, "login.html", {"login_token": token, "error": error and _(error)},
        status_code=status_code,
    )
    response.set_cookie(
        auth.LOGIN_CSRF_COOKIE, token, httponly=True, secure=auth.cookie_secure(),
        samesite="strict", max_age=3600,
    )
    return response


LOCKED = N_("Too many failed attempts. Please wait 15 minutes and try again.")
WRONG = N_("This code is not valid. Please check your input.")


@router.get("/login")
def login_page(
    request: Request,
    tasks: BackgroundTasks,
    c: str | None = None,
    error: str | None = None,
    user: User | None = Depends(auth.current_user),
    db: DbSession = Depends(get_db),
):
    if c is not None:
        # QR link: log in and redirect, so the code disappears from the address bar.
        ip = _client_ip(request)
        if auth.is_locked_out(db, ip):
            return RedirectResponse("/login?error=locked", status_code=303)
        found = auth.authenticate(db, c, ip)
        if found is None:
            return RedirectResponse("/login?error=code", status_code=303)
        return _login_success(request, db, found, tasks)
    if user is not None:
        return RedirectResponse("/", status_code=303)
    error = {"locked": LOCKED, "code": WRONG}.get(error or "")
    return _login_form(request, error)


@router.post("/login")
def login_submit(
    request: Request,
    tasks: BackgroundTasks,
    code: str = Form(""),
    login_token: str = Form(""),
    db: DbSession = Depends(get_db),
):
    cookie = request.cookies.get(auth.LOGIN_CSRF_COOKIE, "")
    if not cookie or not hmac.compare_digest(cookie, login_token):
        return _login_form(request, N_("Please try again."), 400)
    ip = _client_ip(request)
    if auth.is_locked_out(db, ip):
        return _login_form(request, LOCKED, 429)
    user = auth.authenticate(db, code, ip)
    if user is None:
        return _login_form(request, WRONG, 401)
    return _login_success(request, db, user, tasks)


@router.post("/logout")
def logout(
    request: Request,
    _csrf: User = Depends(auth.verify_csrf),
    db: DbSession = Depends(get_db),
):
    auth.delete_session(db, request.cookies.get(auth.SESSION_COOKIE))
    request.state.refresh_cookie = None
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE)
    return response
