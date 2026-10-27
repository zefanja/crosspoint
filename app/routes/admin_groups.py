"""Admin: groups, members, group decks and distribution."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app import anki_service, distribution
from app.auth import require_admin, verify_csrf
from app.db import get_db
from app.i18n import _
from app.models import Group, GroupDeck, GroupMember, User
from app.templating import render

router = APIRouter(prefix="/admin/groups")


def _get_group(db: DbSession, group_id: int) -> Group:
    group = db.get(Group, group_id)
    if group is None:
        raise anki_service.NotFound(_("Group not found."))
    return group


def _job_redirect(group_id: int, job) -> RedirectResponse:
    if job is None:
        return RedirectResponse(f"/admin/groups/{group_id}", status_code=303)
    return RedirectResponse(f"/admin/jobs/{job.id}?back=/admin/groups/{group_id}", status_code=303)


@router.get("")
def group_list(request: Request, admin: User = Depends(require_admin),
               db: DbSession = Depends(get_db), error: str | None = None):
    groups = list(db.scalars(select(Group).order_by(Group.name)))
    counts = {g.id: 0 for g in groups}
    for (gid,) in db.execute(select(GroupMember.group_id)):
        counts[gid] = counts.get(gid, 0) + 1
    deck_counts: dict[int, int] = {}
    for (gid,) in db.execute(select(GroupDeck.group_id)):
        deck_counts[gid] = deck_counts.get(gid, 0) + 1
    return render(request, "admin/groups.html", {
        "groups": groups, "counts": counts, "deck_counts": deck_counts, "error": error,
    })


@router.post("")
def group_create(request: Request, name: str = Form(""), admin: User = Depends(require_admin),
                 _csrf: User = Depends(verify_csrf), db: DbSession = Depends(get_db)):
    name = name.strip()[:100]
    if not name:
        return group_list(request, admin, db, error=_("Please enter a group name."))
    db.add(Group(name=name))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return group_list(request, admin, db, error=_("A group with this name already exists."))
    return RedirectResponse("/admin/groups", status_code=303)


@router.get("/{group_id}")
async def group_detail(request: Request, group_id: int, admin: User = Depends(require_admin),
                       db: DbSession = Depends(get_db), error: str | None = None):
    group = _get_group(db, group_id)
    members = set(db.scalars(select(GroupMember.user_id).where(GroupMember.group_id == group_id)))
    users = list(db.scalars(select(User).order_by(User.display_name)))
    decks = list(db.scalars(select(GroupDeck).where(GroupDeck.group_id == group_id)
                            .order_by(GroupDeck.deck_name)))
    assigned = {d.admin_deck_id for d in decks}
    admin_decks = [(did, name) for did, name in
                   await anki_service.deck_names(anki_service.ADMIN_COLLECTION)
                   if did not in assigned]
    return render(request, "admin/group_detail.html", {
        "group": group, "users": users, "members": members, "decks": decks,
        "admin_decks": admin_decks, "error": error,
    })


@router.post("/{group_id}")
def group_rename(request: Request, group_id: int, name: str = Form(""),
                 admin: User = Depends(require_admin), _csrf: User = Depends(verify_csrf),
                 db: DbSession = Depends(get_db)):
    group = _get_group(db, group_id)
    if name.strip():
        group.name = name.strip()[:100]
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return RedirectResponse(f"/admin/groups/{group_id}", status_code=303)
    return RedirectResponse(f"/admin/groups/{group_id}", status_code=303)


@router.post("/{group_id}/members")
async def group_members(request: Request, group_id: int, admin: User = Depends(require_admin),
                        _csrf: User = Depends(verify_csrf), db: DbSession = Depends(get_db)):
    _get_group(db, group_id)
    form = await request.form()
    wanted = {str(u) for u in form.getlist("user_ids")}
    names = dict(db.execute(select(User.id, User.display_name)).all())
    wanted &= set(names)
    current = set(db.scalars(select(GroupMember.user_id).where(GroupMember.group_id == group_id)))
    for uid in current - wanted:
        db.query(GroupMember).filter_by(group_id=group_id, user_id=uid).delete()
    added = sorted(wanted - current, key=lambda uid: names[uid])
    for uid in added:
        db.add(GroupMember(group_id=group_id, user_id=uid))
    db.commit()
    job = distribution.distribute_group_to_users(
        group_id, [(uid, names[uid]) for uid in added], _("Group decks for new members"))
    return _job_redirect(group_id, job)


@router.post("/{group_id}/decks")
async def group_add_deck(request: Request, group_id: int, admin_deck_id: int = Form(...),
                         admin: User = Depends(require_admin), _csrf: User = Depends(verify_csrf),
                         db: DbSession = Depends(get_db)):
    _get_group(db, group_id)
    names = dict(await anki_service.deck_names(anki_service.ADMIN_COLLECTION))
    if admin_deck_id not in names:
        raise anki_service.NotFound(_("Deck not found."))
    gd = GroupDeck(group_id=group_id, admin_deck_id=admin_deck_id, deck_name=names[admin_deck_id])
    db.add(gd)
    db.commit()
    return _job_redirect(group_id, distribution.distribute_group_deck(gd))


@router.post("/{group_id}/decks/{group_deck_id}/distribute")
async def group_distribute(request: Request, group_id: int, group_deck_id: int,
                           admin: User = Depends(require_admin), _csrf: User = Depends(verify_csrf),
                           db: DbSession = Depends(get_db)):
    gd = db.get(GroupDeck, group_deck_id)
    if gd is None or gd.group_id != group_id:
        raise anki_service.NotFound(_("Deck not found."))
    names = dict(await anki_service.deck_names(anki_service.ADMIN_COLLECTION))
    if gd.admin_deck_id in names and names[gd.admin_deck_id] != gd.deck_name:
        gd.deck_name = names[gd.admin_deck_id]  # renamed in the admin collection
        db.commit()
    return _job_redirect(group_id, distribution.distribute_group_deck(gd))


@router.post("/{group_id}/decks/{group_deck_id}/remove")
def group_remove_deck(group_id: int, group_deck_id: int, admin: User = Depends(require_admin),
                      _csrf: User = Depends(verify_csrf), db: DbSession = Depends(get_db)):
    gd = db.get(GroupDeck, group_deck_id)
    if gd is not None and gd.group_id == group_id:
        db.delete(gd)  # members keep their cards
        db.commit()
    return RedirectResponse(f"/admin/groups/{group_id}", status_code=303)


@router.post("/{group_id}/delete")
def group_delete(group_id: int, confirm_name: str = Form(""), admin: User = Depends(require_admin),
                 _csrf: User = Depends(verify_csrf), db: DbSession = Depends(get_db)):
    group = _get_group(db, group_id)
    if confirm_name.strip() != group.name:
        return RedirectResponse(f"/admin/groups/{group_id}?error=name", status_code=303)
    db.delete(group)
    db.commit()
    return RedirectResponse("/admin/groups", status_code=303)
