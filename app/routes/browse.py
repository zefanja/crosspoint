"""Card list with Anki search syntax."""

from fastapi import APIRouter, Depends, Request

from app import anki_service
from app.auth import current_collection, require_user
from app.models import User
from app.templating import is_htmx, render
from app.i18n import _

router = APIRouter()
LIMIT = 200


@router.get("/browse")
async def browse(request: Request, q: str = "", user: User = Depends(require_user),
    col: str = Depends(current_collection)):
    error = None
    rows, total = [], 0
    try:
        rows, total = await anki_service.search_cards(col, q, limit=LIMIT)
    except anki_service.InvalidSearch:
        error = _("The search is not valid. Examples: John, is:due, is:new")
    ctx = {"q": q, "rows": rows, "total": total, "limit": LIMIT, "error": error}
    if is_htmx(request) and request.headers.get("HX-Target") == "results":
        return render(request, "partials/browse_results.html", ctx)
    return render(request, "browse.html", ctx)
