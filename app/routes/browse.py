"""Card list with Anki search syntax."""

from fastapi import APIRouter, Depends, Request

from app import anki_service
from app.auth import require_user
from app.models import User
from app.templating import is_htmx, render

router = APIRouter()
LIMIT = 200


@router.get("/browse")
async def browse(request: Request, q: str = "", user: User = Depends(require_user)):
    error = None
    rows, total = [], 0
    try:
        rows, total = await anki_service.search_cards(user.id, q, limit=LIMIT)
    except anki_service.InvalidSearch:
        error = "Die Suche ist ungültig. Beispiele: Joh, deck:\"Meine Karten\", is:due"
    ctx = {"q": q, "rows": rows, "total": total, "limit": LIMIT, "error": error}
    if is_htmx(request) and request.headers.get("HX-Target") == "results":
        return render(request, "partials/browse_results.html", ctx)
    return render(request, "browse.html", ctx)
