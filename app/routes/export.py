"""Exports: whole collection (.colpkg) or one deck (.apkg), both with scheduling."""

import re
import secrets
from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app import anki_service
from app.auth import current_collection, require_user, verify_csrf
from app.config import get_settings
from app.models import User
from app.templating import render
from app.i18n import N_

router = APIRouter()


def _temp_path(suffix: str):
    path = get_settings().data_dir / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{secrets.token_hex(12)}{suffix}"


def _download(path, filename: str):
    return FileResponse(
        path, filename=filename, media_type="application/octet-stream",
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w\-]+", "_", name.replace("::", "-")).strip("_") or "Deck"


@router.get("/export")
async def export_page(request: Request, user: User = Depends(require_user),
    col: str = Depends(current_collection)):
    decks = await anki_service.deck_names(col)
    return render(request, "export.html", {"decks": decks})


@router.post("/export/collection")
async def export_collection(user: User = Depends(verify_csrf),
    col: str = Depends(current_collection)):
    path = _temp_path(".colpkg")
    await anki_service.export_collection(col, path)
    return _download(path, f"CrossPoint-{date.today().isoformat()}.colpkg")


@router.post("/export/deck")
async def export_deck(deck_id: int = Form(...), user: User = Depends(verify_csrf),
    col: str = Depends(current_collection)):
    names = dict(await anki_service.deck_names(col))
    if deck_id not in names:
        raise anki_service.NotFound(N_("Deck not found."))
    path = _temp_path(".apkg")
    await anki_service.export_deck(col, deck_id, path)
    return _download(path, f"{_safe_name(names[deck_id])}-{date.today().isoformat()}.apkg")
