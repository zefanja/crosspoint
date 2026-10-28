"""FastAPI application: router registration, lifespan, error handling, housekeeping."""

import asyncio
import contextlib
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app import anki_service, auth, backup, i18n
from app.config import get_settings
from app.db import get_engine, get_sessionmaker
from app.routes import auth as auth_routes
from app.routes import admin, admin_groups, admin_progress, admin_users
from app.routes import bible, browse, decks, export, notes, pages, review, statistics
from app.templating import BASE_DIR, is_htmx, render, templates
from app.i18n import _

log = logging.getLogger(__name__)

HOUSEKEEPING_INTERVAL = 60
EXPORT_MAX_AGE = 3600

CSP = (
    "default-src 'self'; script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; frame-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'self'"
)


class RedactLoginCode(logging.Filter):
    """Remove the login code (?c=...) from uvicorn access log lines."""

    _pattern = re.compile(r"([?&]c=)[^&\s]*", re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                self._pattern.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args
            )
        return True


def exports_dir():
    path = get_settings().data_dir / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_exports(max_age: float = EXPORT_MAX_AGE) -> None:
    cutoff = time.time() - max_age
    for f in exports_dir().iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)


async def housekeeping() -> None:
    last_prune = 0.0
    while True:
        await asyncio.sleep(HOUSEKEEPING_INTERVAL)
        try:
            await anki_service.close_idle()
            cleanup_exports()
            if time.monotonic() - last_prune > 3600:
                with get_sessionmaker()() as db:
                    auth.prune(db)
                last_prune = time.monotonic()
            if backup.backup_due():
                await backup.run_backup()
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("Housekeeping failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()  # fail fast on missing configuration
    logging.getLogger("uvicorn.access").addFilter(RedactLoginCode())
    templates.env.globals["app_version"] = settings.short_version
    exports_dir()
    log.info("Starting version %s, data dir %s", settings.app_version, settings.data_dir)
    task = asyncio.create_task(housekeeping())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await anki_service.close_all()
    get_engine().dispose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
for module in (auth_routes, decks, review, bible, notes, browse, export, pages, statistics,
               admin, admin_users, admin_groups, admin_progress):
    app.include_router(module.router)


@app.middleware("http")
async def headers_and_session_refresh(request: Request, call_next):
    lang = i18n.negotiate(request.cookies.get(i18n.LANG_COOKIE),
                          request.headers.get("accept-language"))
    token = i18n.set_language(lang)
    try:
        response = await call_next(request)
    finally:
        i18n._current.reset(token)
    response.headers.setdefault("Content-Language", lang)
    response.headers.setdefault("Vary", "Accept-Language, Cookie")
    raw = getattr(request.state, "refresh_cookie", None)
    if raw:
        auth.set_session_cookie(response, raw)
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.exception_handler(auth.LoginRequired)
async def login_required(request: Request, exc):
    if is_htmx(request):
        return Response(status_code=200, headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(auth.Forbidden)
async def forbidden(request: Request, exc):
    return render(request, "error.html", {"message": _("You don't have permission to do that.")}, 403)


@app.exception_handler(auth.CsrfError)
async def csrf_error(request: Request, exc):
    return render(
        request, "error.html",
        {"message": _("Your session has expired. Please reload the page and try again.")},
        403,
    )


@app.exception_handler(anki_service.NotFound)
async def not_found(request: Request, exc):
    return render(request, "error.html", {"message": _(str(exc))}, 404)


@app.get("/healthz")
def healthz():
    """Checks the DB connection and write access to DATA_DIR."""
    settings = get_settings()
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        probe = settings.data_dir / f".healthz-{uuid.uuid4().hex}"
        probe.write_text("ok")
        probe.unlink()
    except Exception as exc:  # noqa: BLE001 - report any failure as unhealthy
        log.error("Health check failed: %s", exc)
        return JSONResponse({"status": "error"}, status_code=503)
    return {"status": "ok", "version": settings.app_version}


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return FileResponse(
        BASE_DIR / "static" / "sw.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    icons = "/static/icons"
    data = {
        "name": _("CrossPoint – memorize the Bible"),
        "short_name": "CrossPoint",
        "description": _("Memorize Bible verses with spaced repetition – compatible with Anki."),
        "lang": i18n.get_language(),
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#f1f7fa",
        "theme_color": "#001a3f",
        "icons": [
            {"src": f"{icons}/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": f"{icons}/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": f"{icons}/maskable-512.png", "sizes": "512x512", "type": "image/png",
             "purpose": "maskable"},
            {"src": f"{icons}/icon.svg", "sizes": "any", "type": "image/svg+xml"},
        ],
    }
    return JSONResponse(data, media_type="application/manifest+json")
