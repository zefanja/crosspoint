"""Static content pages."""

import hmac
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from app import anki_service, i18n
from app.auth import cookie_secure, media_token, require_user
from app.models import User

from app.templating import render

router = APIRouter()


@router.get("/privacy")
def privacy(request: Request):
    return render(request, "privacy.html")


@router.get("/me")
def me(request: Request, user: User = Depends(require_user)):
    """Account page (tab "Me"): export, language, log out."""
    return render(request, "me.html")


@router.get("/media/{collection_id}/{token}/{filename}")
def media(collection_id: str, token: str, filename: str):
    """Images for cards; authorized by the per-collection token (no cookie needed)."""
    try:
        valid = hmac.compare_digest(token, media_token(collection_id))
        path = anki_service.media_file(collection_id, filename) if valid else None
    except ValueError:
        path = None
    if path is None:
        return Response(status_code=404)
    return FileResponse(path, headers={"Cache-Control": "private, max-age=31536000, immutable"})


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
