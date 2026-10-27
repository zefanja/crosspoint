"""Review screen. htmx swaps only #review; one request per answer (next card included)."""

import time

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app import anki_service, stats
from app.auth import current_collection, require_user, verify_csrf
from app.models import User
from app.templating import is_htmx, render

router = APIRouter()

MAX_TAKEN_SECS = 3600  # anki caps further via the deck's maxTaken


def _render_state(request: Request, state, full_page: bool, undone: bool = False):
    ctx = {"state": state, "shown_at": f"{time.time():.3f}", "undone": undone}
    return render(request, "review.html" if full_page else "partials/review_card.html", ctx)


@router.get("/review/{deck_id}")
async def review_page(request: Request, deck_id: int, user: User = Depends(require_user),
    col: str = Depends(current_collection)):
    state = await anki_service.review_state(col, deck_id)
    return _render_state(request, state, full_page=True)


@router.post("/review/{deck_id}/answer")
async def answer(
    request: Request,
    deck_id: int,
    tasks: BackgroundTasks,
    card_id: int = Form(...),
    rating: int = Form(...),
    shown_at: float = Form(0),
    user: User = Depends(verify_csrf),
    col: str = Depends(current_collection),
):
    taken = min(max(time.time() - shown_at, 0.0), MAX_TAKEN_SECS) if shown_at else 0.0
    state, answered = await anki_service.answer_card(col, deck_id, card_id, rating, taken)
    if answered and col == user.id:
        tasks.add_task(stats.update_after_review, user.id)
    if not is_htmx(request):
        return RedirectResponse(f"/review/{deck_id}", status_code=303)
    return _render_state(request, state, full_page=False)


@router.post("/review/{deck_id}/undo")
async def undo(
    request: Request, deck_id: int, tasks: BackgroundTasks, user: User = Depends(verify_csrf),
    col: str = Depends(current_collection)
):
    state, undone = await anki_service.undo_review(col, deck_id)
    if undone and col == user.id:
        tasks.add_task(stats.update_after_review, user.id)
    if not is_htmx(request):
        return RedirectResponse(f"/review/{deck_id}", status_code=303)
    return _render_state(request, state, full_page=False, undone=undone)
