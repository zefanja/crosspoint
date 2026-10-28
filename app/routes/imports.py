"""Importing ready-made Anki packages (.apkg) into the learner's own collection."""

import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Request, UploadFile

from app import anki_service
from app.auth import current_collection, require_user, verify_csrf
from app.config import get_settings
from app.i18n import _
from app.models import User
from app.sanitize import field_from_import
from app.templating import render

router = APIRouter()

MAX_UPLOAD = 100 * 1024 * 1024


async def save_upload(file: UploadFile) -> Path:
    """Write an .apkg upload to a temp file under DATA_DIR. Raises ValueError with a
    user-facing message; the caller deletes the returned file."""
    if not (file.filename or "").lower().endswith(".apkg"):
        raise ValueError(_("Please choose an .apkg file."))
    tmp = get_settings().data_dir / "exports" / f"upload-{secrets.token_hex(8)}.apkg"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with tmp.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise ValueError(_("The file is too large (max. 100 MB)."))
                out.write(chunk)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


async def import_upload(file: UploadFile, col: str, *, sanitize: bool) -> tuple[int | None, str | None]:
    """Save and import an upload. Returns (count, error)."""
    try:
        tmp = await save_upload(file)
    except ValueError as exc:
        return None, str(exc)
    try:
        count = await anki_service.import_package(
            col, tmp, clean_field=field_from_import if sanitize else None)
    except Exception:  # noqa: BLE001 - anki rejects broken packages with various errors
        return None, _("The file could not be imported. Is it a valid Anki file?")
    finally:
        tmp.unlink(missing_ok=True)
    return count, None


@router.get("/import")
def import_page(request: Request, deck_id: int | None = None, user: User = Depends(require_user)):
    # deck_id only keeps the selected deck when switching back to the other add tabs
    return render(request, "import.html", {"message": None, "error": None, "deck_id": deck_id})


@router.post("/import")
async def import_submit(request: Request, file: UploadFile, user: User = Depends(verify_csrf),
                        col: str = Depends(current_collection)):
    count, error = await import_upload(file, col, sanitize=True)
    message = None if error else _("“%(file)s” imported: %(count)s notes new or updated. The decks are now in your deck list.",
                                   file=file.filename, count=count)
    return render(request, "import.html", {"message": message, "error": error, "deck_id": None},
                  status_code=400 if error else 200)
