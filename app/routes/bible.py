"""Bible text import: creates Bible Memorizer notes (zefanja/bible-memorizer)."""

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session as DbSession

from app import anki_service, workspace
from app.auth import current_collection, require_user, verify_csrf
from app.db import get_db
from app.bible_memorizer.formatter import auto_format_verses, parse_for_preview
from app.models import User
from app.sanitize import plain_text_lines
from app.templating import render
from app.i18n import _
from app.workspace import DeckAccess, deck_access

router = APIRouter()

MAX_TEXT = 50_000
TABLE_MIN_VERSES = 5  # gen_notes only builds tables for more than 4 verses


def _prepare(text: str) -> tuple[str, list[dict]]:
    formatted = auto_format_verses(plain_text_lines(text[:MAX_TEXT]))
    return formatted, parse_for_preview(formatted)


async def _page(request, col: str, access: DeckAccess, *, deck_id=None, title="", recite=2,
                table=False, text="", message=None, error=None, distribute=(),
                status_code=200):
    decks = access.filter(await anki_service.deck_names(col), edit=True)
    if deck_id not in dict(decks):
        deck_id = await anki_service.current_deck_id(col)
        if deck_id not in dict(decks):
            deck_id = decks[0][0] if decks else None
    if not decks and not error:
        error = _("There is no deck you can add cards to yet. Create one on the start page.")
    _formatted, rows = _prepare(text)
    return render(
        request, "note_bible.html",
        {"decks": decks, "deck_id": deck_id, "title": title, "recite": recite, "table": table,
         "text": text, "rows": rows, "verses": len({r["verse"] for r in rows}),
         "table_min": TABLE_MIN_VERSES, "message": message, "error": error,
         "distribute": distribute},
        status_code=status_code,
    )


@router.get("/notes/bible")
async def bible_page(request: Request, deck_id: int | None = None,
                     user: User = Depends(require_user),
    col: str = Depends(current_collection), access: DeckAccess = Depends(deck_access)):
    return await _page(request, col, access, deck_id=deck_id)


@router.post("/notes/bible/preview")
async def bible_preview(request: Request, text: str = Form(""), user: User = Depends(verify_csrf),
    col: str = Depends(current_collection)):
    _formatted, rows = _prepare(text)
    return render(request, "partials/bible_preview.html",
                  {"rows": rows, "verses": len({r["verse"] for r in rows}),
                   "table_min": TABLE_MIN_VERSES})


@router.post("/notes/bible")
async def bible_submit(
    request: Request,
    deck_id: int = Form(...),
    title: str = Form(""),
    recite: int = Form(2),
    table: bool = Form(False),
    text: str = Form(""),
    user: User = Depends(verify_csrf),
    col: str = Depends(current_collection),
    access: DeckAccess = Depends(deck_access),
    db: DbSession = Depends(get_db),
):
    access.require_edit([deck_id])
    recite = min(max(recite, 1), 10)
    title = plain_text_lines(title).strip()[:200]
    formatted, _rows = _prepare(text)
    form = {"deck_id": deck_id, "title": title, "recite": recite, "table": table}
    if not title:
        return await _page(request, col, access, **form, text=text, status_code=400,
                           error=_("Please enter a title, e.g. “Psalm 23”."))
    try:
        notes, cards = await anki_service.add_bible_text(
            col, deck_id, title, recite, formatted, table,
            (anki_service.GROUP_TAG,) if col == anki_service.ADMIN_COLLECTION else ())
    except anki_service.AnkiServiceError as exc:
        return await _page(request, col, access, **form, text=text, error=_(str(exc)),
                           status_code=400)
    workspace.log_edit(db, user, col, 0, deck_id, "add", title)
    return await _page(request, col, access, deck_id=deck_id, recite=recite, table=table,
                       distribute=await workspace.distribute_targets(db, user, col, deck_id),
                       message=_("“%(title)s”: %(notes)s steps with %(cards)s cards added.",
                                 title=title, notes=notes, cards=cards))
