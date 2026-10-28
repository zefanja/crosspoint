"""Deck list (start page), creating and renaming decks, deck options."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session as DbSession

from app import anki_service, workspace
from app.auth import current_collection, require_user, verify_csrf
from app.db import get_db
from app.models import User
from app.workspace import DeckAccess, deck_access, no_leader_workspace
from app.templating import render
from app.i18n import _, format_decimal

router = APIRouter()

RETENTION_MIN, RETENTION_MAX = 0.80, 0.97


SECONDS_PER_CARD = 15  # rough estimate for the "~N min" hint


@router.get("/")
async def deck_list(request: Request, user: User = Depends(require_user),
    col: str = Depends(current_collection), access: DeckAccess = Depends(deck_access),
    error: str | None = None):
    if access.restricted:
        return await _leader_deck_list(request, access, error)
    decks = await anki_service.deck_list(col)
    stats = await anki_service.collection_stats(col, days=1)
    top = [d for d in decks if d.depth == 0]  # parent counts include their subdecks
    totals = {"new": sum(d.new for d in top), "learn": sum(d.learn for d in top),
              "review": sum(d.review for d in top)}
    todo = sum(totals.values())
    return render(
        request, "decks.html",
        {"decks": decks, "total_cards": stats.total_cards, "totals": totals, "todo": todo,
         "minutes": max(1, round(todo * SECONDS_PER_CARD / 60)),
         "all_done": stats.total_cards > 0 and todo == 0,
         "streak": stats.streak_days, "reviews_today": stats.reviews_today,
         "first_name": user.display_name.split()[0] if user.display_name.strip() else "",
         "all_decks": anki_service.ALL_DECKS, "error": error},
    )


async def _leader_deck_list(request: Request, access: DeckAccess, error: str | None):
    """Group leaders in the admin collection: their groups' decks, to browse and edit."""
    decks = access.deck_list(await anki_service.deck_list(anki_service.ADMIN_COLLECTION))
    return render(request, "decks_workspace.html", {
        "decks": decks, "editable": access.editable, "groups": access.groups,
        "searches": {d.id: anki_service.deck_search(d.name) for d in decks}, "error": error,
    }, status_code=400 if error else 200)


@router.post("/decks")
async def create_deck(
    request: Request, name: str = Form(""), group_id: int | None = Form(None),
    user: User = Depends(verify_csrf), col: str = Depends(current_collection),
    access: DeckAccess = Depends(deck_access), db: DbSession = Depends(get_db),
):
    try:
        if access.restricted:
            await workspace.create_deck(db, access, name, group_id)
        else:
            await anki_service.create_deck(col, name)
    except anki_service.AnkiServiceError as exc:
        return await deck_list(request, user, col, access, error=_(str(exc)))
    return RedirectResponse("/", status_code=303)


async def _options_page(request: Request, col: str, deck_id: int, opts=None, *,
                        status_code: int = 200, **ctx):
    names = dict(await anki_service.deck_names(col))
    if deck_id not in names:
        raise anki_service.NotFound(_("Deck not found."))
    return render(
        request, "deck_options.html",
        {"deck_id": deck_id, "deck_name": names[deck_id],
         "opts": opts or await anki_service.get_deck_options(col, deck_id),
         "saved": False, "renamed": False, "errors": [], "rename_error": None, "optimized": None,
         "can_rename": col == anki_service.ADMIN_COLLECTION
                       or not await anki_service.is_group_deck(col, deck_id),
         "group_rename": col == anki_service.ADMIN_COLLECTION,
         "reviews": await anki_service.review_count(col, deck_id),
         "min_reviews": anki_service.MIN_OPTIMIZE_REVIEWS, **ctx},
        status_code=status_code,
    )


@router.get("/decks/{deck_id}/options", dependencies=[Depends(no_leader_workspace)])
async def options_page(
    request: Request, deck_id: int, user: User = Depends(require_user),
    col: str = Depends(current_collection), saved: int = 0, renamed: int = 0,
    optimized: str | None = None
):
    return await _options_page(request, col, deck_id, saved=bool(saved), renamed=bool(renamed),
                               optimized=optimized)


@router.post("/decks/{deck_id}/options", dependencies=[Depends(no_leader_workspace)])
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
        return await _options_page(request, col, deck_id, opts, errors=errors, status_code=400)
    await anki_service.set_deck_options(col, deck_id, opts)
    return RedirectResponse(f"/decks/{deck_id}/options?saved=1", status_code=303)


@router.post("/decks/{deck_id}/optimize", dependencies=[Depends(no_leader_workspace)])
async def optimize(deck_id: int, user: User = Depends(verify_csrf),
                   col: str = Depends(current_collection)):
    result = await anki_service.optimize_fsrs(col, deck_id)
    state = "changed" if result.changed else "unchanged"
    return RedirectResponse(f"/decks/{deck_id}/options?optimized={state}", status_code=303)


@router.get("/decks/{deck_id}/rename")
async def rename_page(request: Request, deck_id: int, user: User = Depends(require_user),
                      col: str = Depends(current_collection),
                      access: DeckAccess = Depends(deck_access)):
    """Own page for group leaders (the options page is blocked in their workspace)."""
    return await _rename_page(request, col, access, deck_id)


async def _rename_page(request: Request, col: str, access: DeckAccess, deck_id: int,
                       error: str | None = None):
    if not access.can_edit(deck_id):
        raise anki_service.NotFound(_("Deck not found."))
    names = dict(await anki_service.deck_names(col))
    if deck_id not in names:
        raise anki_service.NotFound(_("Deck not found."))
    is_group = col == anki_service.ADMIN_COLLECTION
    return render(request, "deck_rename.html", {
        "deck_id": deck_id, "deck_name": names[deck_id], "rename_error": error,
        "can_rename": is_group or not await anki_service.is_group_deck(col, deck_id),
        "group_rename": is_group,
    }, status_code=400 if error else 200)


@router.post("/decks/{deck_id}/rename")
async def rename(request: Request, deck_id: int, name: str = Form(""),
                 user: User = Depends(verify_csrf), col: str = Depends(current_collection),
                 access: DeckAccess = Depends(deck_access), db: DbSession = Depends(get_db)):
    job = None
    try:
        if col == anki_service.ADMIN_COLLECTION:
            job = await workspace.rename_deck(db, access, deck_id, name)
        else:
            await anki_service.rename_deck(col, deck_id, name)
    except anki_service.NotFound:
        raise
    except anki_service.AnkiServiceError as exc:
        if access.restricted:
            return await _rename_page(request, col, access, deck_id, _(str(exc)))
        return await _options_page(request, col, deck_id, rename_error=_(str(exc)),
                                   status_code=400)
    if job is not None:
        return RedirectResponse(f"/admin/jobs/{job.id}?back=/admin/group-decks", status_code=303)
    if access.restricted:
        return RedirectResponse("/", status_code=303)
    return RedirectResponse(f"/decks/{deck_id}/options?renamed=1", status_code=303)
