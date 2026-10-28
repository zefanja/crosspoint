"""Deck list (start page), creating decks, deck options."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app import anki_service
from app.auth import current_collection, require_user, verify_csrf
from app.models import User
from app.templating import render
from app.i18n import _, format_decimal

router = APIRouter()

RETENTION_MIN, RETENTION_MAX = 0.80, 0.97


@router.get("/")
async def deck_list(request: Request, user: User = Depends(require_user),
    col: str = Depends(current_collection), error: str | None = None):
    decks = await anki_service.deck_list(col)
    _rows, total_cards = await anki_service.search_cards(col, "", limit=0)
    all_done = total_cards > 0 and all(d.new + d.learn + d.review == 0 for d in decks)
    return render(
        request, "decks.html",
        {"decks": decks, "total_cards": total_cards, "all_done": all_done, "error": error},
    )


@router.post("/decks")
async def create_deck(
    request: Request, name: str = Form(""), user: User = Depends(verify_csrf),
    col: str = Depends(current_collection)
):
    try:
        await anki_service.create_deck(col, name)
    except anki_service.AnkiServiceError as exc:
        return await deck_list(request, user, col, error=_(str(exc)))
    return RedirectResponse("/", status_code=303)


@router.get("/decks/{deck_id}/options")
async def options_page(
    request: Request, deck_id: int, user: User = Depends(require_user),
    col: str = Depends(current_collection), saved: int = 0, optimized: str | None = None
):
    opts = await anki_service.get_deck_options(col, deck_id)
    names = dict(await anki_service.deck_names(col))
    return render(
        request, "deck_options.html",
        {"deck_id": deck_id, "deck_name": names.get(deck_id, ""), "opts": opts,
         "saved": bool(saved), "errors": [], "optimized": optimized,
         "reviews": await anki_service.review_count(col, deck_id),
         "min_reviews": anki_service.MIN_OPTIMIZE_REVIEWS},
    )


@router.post("/decks/{deck_id}/options")
async def options_save(
    request: Request,
    deck_id: int,
    new_per_day: int = Form(...),
    reviews_per_day: int = Form(...),
    desired_retention: float = Form(...),
    bury_new: str | None = Form(None),  # checkboxes: only sent when checked
    bury_reviews: str | None = Form(None),
    user: User = Depends(verify_csrf),
    col: str = Depends(current_collection),
):
    opts = anki_service.DeckOptions(new_per_day, reviews_per_day, round(desired_retention, 2),
                                    bury_new=bool(bury_new), bury_reviews=bool(bury_reviews))
    errors = []
    if not 0 <= new_per_day <= 9999:
        errors.append(_("New cards per day: 0 to 9999."))
    if not 0 <= reviews_per_day <= 9999:
        errors.append(_("Reviews per day: 0 to 9999."))
    if not RETENTION_MIN <= opts.desired_retention <= RETENTION_MAX:
        errors.append(_("Desired retention: %(min)s to %(max)s.", min=format_decimal(RETENTION_MIN), max=format_decimal(RETENTION_MAX)))
    if errors:
        names = dict(await anki_service.deck_names(col))
        return render(
            request, "deck_options.html",
            {"deck_id": deck_id, "deck_name": names.get(deck_id, ""), "opts": opts,
             "saved": False, "errors": errors, "optimized": None,
             "reviews": await anki_service.review_count(col, deck_id),
             "min_reviews": anki_service.MIN_OPTIMIZE_REVIEWS},
            status_code=400,
        )
    await anki_service.set_deck_options(col, deck_id, opts)
    return RedirectResponse(f"/decks/{deck_id}/options?saved=1", status_code=303)


@router.post("/decks/{deck_id}/optimize")
async def optimize(deck_id: int, user: User = Depends(verify_csrf),
                   col: str = Depends(current_collection)):
    result = await anki_service.optimize_fsrs(col, deck_id)
    state = "changed" if result.changed else "unchanged"
    return RedirectResponse(f"/decks/{deck_id}/options?optimized={state}", status_code=303)
