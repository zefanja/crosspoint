"""FastAPI application: router registration and lifespan."""

import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from app.config import get_settings
from app.db import get_engine

SOURCE_URL = "https://github.com/zefanja/crosspoint"
BASE_DIR = Path(__file__).resolve().parent

log = logging.getLogger(__name__)

templates = Jinja2Templates(directory=BASE_DIR / "templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()  # fail fast on missing configuration
    templates.env.globals.update(
        app_version=settings.short_version,
        source_url=SOURCE_URL,
    )
    log.info("Starting version %s, data dir %s", settings.app_version, settings.data_dir)
    yield
    # Phase 1: close all open collections here.
    get_engine().dispose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


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


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")
