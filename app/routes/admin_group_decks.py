"""Group decks for admins and group leaders: distribute, see who changed what."""

from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app import anki_service, workspace
from app.auth import WORKSPACE_COOKIE, require_staff, verify_csrf, visible_group_ids
from app.db import get_db
from app.i18n import _
from app.models import Group, GroupDeck, User
from app.routes.admin_groups import start_distribution
from app.templating import render

router = APIRouter(prefix="/admin/group-decks")

RECENT = 30


@dataclass
class DeckRow:
    deck: GroupDeck
    name: str
    group_name: str
    exists: bool
    last_edit: workspace.EditInfo | None
    pending: int  # changes since the last distribution


@router.get("")
async def group_decks(request: Request, user: User = Depends(require_staff),
                      db: DbSession = Depends(get_db)):
    groups = visible_group_ids(db, user)
    query = select(GroupDeck, Group.name).join(Group, Group.id == GroupDeck.group_id)
    if groups is not None:
        query = query.where(GroupDeck.group_id.in_(groups))
    rows = db.execute(query.order_by(Group.name, GroupDeck.deck_name)).all()

    names = dict(await anki_service.deck_names(anki_service.ADMIN_COLLECTION))
    access = await workspace.access_for(db, user, anki_service.ADMIN_COLLECTION)
    edits = [e for e in workspace.edits_since(db, None)
             if e.deck_id in names and access.can_see(e.deck_id)]
    for e in edits:
        e.deck_name = names[e.deck_id]

    decks = []
    for gd, group_name in rows:
        name = names.get(gd.admin_deck_id, gd.deck_name)
        mine = [e for e in edits if workspace.deck_within(e.deck_name, name)]
        since = gd.last_distributed_at or datetime.min
        decks.append(DeckRow(gd, name, group_name, gd.admin_deck_id in names,
                             mine[0] if mine else None,
                             sum(1 for e in mine if e.created_at >= since)))
    return render(request, "admin/group_decks.html", {
        "decks": decks, "recent": edits[:RECENT],
        "admin_ws": request.cookies.get(WORKSPACE_COOKIE) == "admin",
    })


@router.post("/{group_deck_id}/distribute")
async def distribute(group_deck_id: int, user: User = Depends(require_staff),
                     _csrf: User = Depends(verify_csrf), db: DbSession = Depends(get_db)):
    """Admins and the leaders of the deck's group."""
    gd = db.get(GroupDeck, group_deck_id)
    groups = visible_group_ids(db, user)
    if gd is None or (groups is not None and gd.group_id not in groups):
        raise anki_service.NotFound(_("Deck not found."))
    job = await start_distribution(db, gd)
    return RedirectResponse(f"/admin/jobs/{job.id}?back=/admin/group-decks", status_code=303)
