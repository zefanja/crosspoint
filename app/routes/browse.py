"""Note and card list with Anki search syntax; single cards can be suspended."""

from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app import anki_service
from app.auth import current_collection, require_user, verify_csrf
from app.models import User
from app.templating import is_htmx, render
from app.i18n import _

router = APIRouter()
LIMIT = 200
VIEWS = ("notes", "cards")


async def _filters(col: str) -> list[tuple[str, str]]:
    """Quick filter chips: (label, Anki search); top-level decks after the states."""
    decks = [name for _id, name in await anki_service.deck_names(col) if "::" not in name]
    return ([(_("All"), ""), (_("Due"), "is:due"), (_("New"), "is:new"),
             (_("Suspended"), "is:suspended")]
            + [(name, anki_service.deck_search(name)) for name in decks])


@router.get("/browse")
async def browse(request: Request, q: str = "", view: str = "notes",
    user: User = Depends(require_user), col: str = Depends(current_collection)):
    view = view if view in VIEWS else "notes"
    search = anki_service.search_cards if view == "cards" else anki_service.search_notes
    error = None
    rows, total = [], 0
    try:
        rows, total = await search(col, q, limit=LIMIT)
    except anki_service.InvalidSearch:
        error = _("The search is not valid. Examples: John, is:due, is:new")
    ctx = {"q": q, "view": view, "rows": rows, "total": total, "limit": LIMIT, "error": error,
           "today": date.today()}
    if is_htmx(request) and request.headers.get("HX-Target") == "results":
        return render(request, "partials/browse_results.html", ctx)
    ctx["filters"] = await _filters(col)
    return render(request, "browse.html", ctx)


@router.post("/cards/{card_id}/suspend")
async def suspend_card(request: Request, card_id: int, suspended: int = Form(...),
                       q: str = Form(""), user: User = Depends(verify_csrf),
                       col: str = Depends(current_collection)):
    row = await anki_service.set_card_suspended(col, card_id, bool(suspended))
    if not is_htmx(request):
        return RedirectResponse("/browse?" + urlencode({"view": "cards", "q": q}), status_code=303)
    return render(request, "partials/browse_card_row.html", {"r": row, "q": q, "today": date.today()})
