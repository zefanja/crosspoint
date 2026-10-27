"""Static content pages."""

from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app import i18n
from app.auth import cookie_secure

from app.templating import render

router = APIRouter()


@router.get("/privacy")
def privacy(request: Request):
    return render(request, "privacy.html")


@router.get("/lang/{code}")
def set_language(code: str, request: Request):
    """Language switcher: remember the choice in a cookie, go back."""
    referer = request.headers.get("referer", "/")
    target = urlsplit(referer)
    back = target.path + (f"?{target.query}" if target.query else "") if target.path else "/"
    response = RedirectResponse(back if back.startswith("/") else "/", status_code=303)
    if code in i18n.available_languages():
        response.set_cookie(i18n.LANG_COOKIE, code, max_age=365 * 86400, httponly=True,
                            secure=cookie_secure(), samesite="lax")
    return response


@router.get("/offline")
def offline(request: Request):
    return render(request, "offline.html")
