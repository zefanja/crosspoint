"""Deck list (start page), creating decks, deck options."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app import anki_service
from app.auth import require_user, verify_csrf
from app.models import User
from app.templating import render

router = APIRouter()

RETENTION_MIN, RETENTION_MAX = 0.80, 0.97


@router.get("/")
async def deck_list(request: Request, user: User = Depends(require_user), error: str | None = None):
    decks = await anki_service.deck_list(user.id)
    _, total_cards = await anki_service.search_cards(user.id, "", limit=0)
    all_done = total_cards > 0 and all(d.new + d.learn + d.review == 0 for d in decks)
    return render(
        request, "decks.html",
        {"decks": decks, "total_cards": total_cards, "all_done": all_done, "error": error},
    )


@router.post("/decks")
async def create_deck(
    request: Request, name: str = Form(""), user: User = Depends(verify_csrf)
):
    try:
        await anki_service.create_deck(user.id, name)
    except anki_service.AnkiServiceError as exc:
        return await deck_list(request, user, error=str(exc))
    return RedirectResponse("/", status_code=303)


@router.get("/decks/{deck_id}/options")
async def options_page(
    request: Request, deck_id: int, user: User = Depends(require_user), saved: int = 0
):
    opts = await anki_service.get_deck_options(user.id, deck_id)
    names = dict(await anki_service.deck_names(user.id))
    return render(
        request, "deck_options.html",
        {"deck_id": deck_id, "deck_name": names.get(deck_id, ""), "opts": opts,
         "saved": bool(saved), "errors": []},
    )


@router.post("/decks/{deck_id}/options")
async def options_save(
    request: Request,
    deck_id: int,
    new_per_day: int = Form(...),
    reviews_per_day: int = Form(...),
    desired_retention: float = Form(...),
    user: User = Depends(verify_csrf),
):
    opts = anki_service.DeckOptions(new_per_day, reviews_per_day, round(desired_retention, 2))
    errors = []
    if not 0 <= new_per_day <= 9999:
        errors.append("Neue Karten pro Tag: 0 bis 9999.")
    if not 0 <= reviews_per_day <= 9999:
        errors.append("Wiederholungen pro Tag: 0 bis 9999.")
    if not RETENTION_MIN <= opts.desired_retention <= RETENTION_MAX:
        errors.append("Gewünschte Behaltensquote: 0,80 bis 0,97.")
    if errors:
        names = dict(await anki_service.deck_names(user.id))
        return render(
            request, "deck_options.html",
            {"deck_id": deck_id, "deck_name": names.get(deck_id, ""), "opts": opts,
             "saved": False, "errors": errors},
            status_code=400,
        )
    await anki_service.set_deck_options(user.id, deck_id, opts)
    return RedirectResponse(f"/decks/{deck_id}/options?saved=1", status_code=303)
