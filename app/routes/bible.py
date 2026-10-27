"""Bible text import: creates Bible Memorizer notes (zefanja/bible-memorizer)."""

from fastapi import APIRouter, Depends, Form, Request

from app import anki_service
from app.auth import require_user, verify_csrf
from app.bible_memorizer.formatter import auto_format_verses, parse_for_preview
from app.models import User
from app.sanitize import plain_text_lines
from app.templating import render

router = APIRouter()

MAX_TEXT = 50_000
TABLE_MIN_VERSES = 5  # gen_notes only builds tables for more than 4 verses


def _prepare(text: str) -> tuple[str, list[dict]]:
    formatted = auto_format_verses(plain_text_lines(text[:MAX_TEXT]))
    return formatted, parse_for_preview(formatted)


async def _page(request, user, *, deck_id=None, title="", recite=2, table=False, text="",
                message=None, error=None, status_code=200):
    decks = await anki_service.deck_names(user.id)
    if deck_id not in dict(decks):
        deck_id = next((d for d, name in decks if name == anki_service.DEFAULT_DECK), decks[0][0])
    _, rows = _prepare(text)
    return render(
        request, "note_bible.html",
        {"decks": decks, "deck_id": deck_id, "title": title, "recite": recite, "table": table,
         "text": text, "rows": rows, "verses": len({r["verse"] for r in rows}),
         "table_min": TABLE_MIN_VERSES, "message": message, "error": error},
        status_code=status_code,
    )


@router.get("/notes/bible")
async def bible_page(request: Request, deck_id: int | None = None,
                     user: User = Depends(require_user)):
    return await _page(request, user, deck_id=deck_id)


@router.post("/notes/bible/preview")
async def bible_preview(request: Request, text: str = Form(""), user: User = Depends(verify_csrf)):
    _, rows = _prepare(text)
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
):
    recite = min(max(recite, 1), 10)
    title = plain_text_lines(title).strip()[:200]
    formatted, _ = _prepare(text)
    form = {"deck_id": deck_id, "title": title, "recite": recite, "table": table}
    if not title:
        return await _page(request, user, **form, text=text, status_code=400,
                           error="Bitte einen Titel eingeben, z. B. „Psalm 23“.")
    try:
        notes, cards = await anki_service.add_bible_text(
            user.id, deck_id, title, recite, formatted, table)
    except anki_service.AnkiServiceError as exc:
        return await _page(request, user, **form, text=text, error=str(exc), status_code=400)
    return await _page(request, user, deck_id=deck_id, recite=recite, table=table,
                       message=f"„{title}“: {notes} Abschnitte mit {cards} Karten hinzugefügt.")
