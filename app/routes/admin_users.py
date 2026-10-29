"""Admin: users, login codes, print sheet with QR codes."""

import segno
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from markupsafe import Markup
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app import anki_service, distribution
from app.auth import (
    create_user, format_code, require_admin, require_staff, reset_code, verify_csrf,
    visible_group_ids, visible_user_ids,
)
from app.config import get_settings
from app.db import get_db
from app.i18n import _
from app.models import Group, GroupMember, Session, User, UserStats
from app.templating import render

router = APIRouter(prefix="/admin/users")

ROLES = ("learner", "leader", "admin")
MAX_BATCH = 300


def login_url(code: str) -> str:
    return f"{get_settings().base_url}/login?c={code}"


def qr_svg(data: str) -> Markup:
    return Markup(segno.make(data, error="m").svg_inline(scale=3, border=2, dark="#000"))


def print_sheet(request: Request, entries: list[tuple[str, str]], back: str = "/admin/users"):
    """The only place where plain codes are shown – right after creating/resetting them."""
    cards = [
        {"name": name, "code": format_code(code), "qr": qr_svg(login_url(code))}
        for name, code in entries
    ]
    response = render(request, "admin/print_sheet.html", {
        "cards": cards, "login_base": f"{get_settings().base_url}/login", "back": back,
    })
    response.headers["Cache-Control"] = "no-store"
    return response


def _groups(db: DbSession) -> list[Group]:
    return list(db.scalars(select(Group).order_by(Group.name)))


def _visible_groups(db: DbSession, viewer: User) -> list[Group]:
    visible = visible_group_ids(db, viewer)
    return [g for g in _groups(db) if visible is None or g.id in visible]


def _active_admins(db: DbSession) -> int:
    return db.scalar(select(func.count()).select_from(User)
                     .where(User.role == "admin", User.disabled.is_(False))) or 0


def _get_user(db: DbSession, user_id: str) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise anki_service.NotFound(_("User not found."))
    return user


@router.get("")
def user_list(request: Request, group: str = "",
              admin: User = Depends(require_staff), db: DbSession = Depends(get_db)):
    # the "All users" option submits an empty value
    group = int(group) if group.isdigit() else None
    query = select(User).order_by(User.display_name)
    visible = visible_user_ids(db, admin)
    if visible is not None:  # group leader: members of the own groups only
        query = query.where(User.id.in_(visible))
    groups = _visible_groups(db, admin)
    if group and group not in {g.id for g in groups}:
        group = None
    if group:
        query = query.join(GroupMember, GroupMember.user_id == User.id).where(
            GroupMember.group_id == group)
    users = list(db.scalars(query))
    memberships: dict[str, list[str]] = {}
    for user_id, name in db.execute(select(GroupMember.user_id, Group.name)
                                    .join(Group, Group.id == GroupMember.group_id)):
        memberships.setdefault(user_id, []).append(name)
    return render(request, "admin/users.html", {
        "users": users, "memberships": memberships, "groups": groups, "group": group,
        "error": None,
    })


@router.post("")
async def users_create(request: Request, names: str = Form(""), role: str = Form("learner"),
                       group_id: int | None = Form(None), admin: User = Depends(require_admin),
                       _csrf: User = Depends(verify_csrf), db: DbSession = Depends(get_db)):
    wanted = [n.strip()[:100] for n in names.splitlines() if n.strip()]
    if not wanted or len(wanted) > MAX_BATCH or role not in ROLES:
        groups = _groups(db)
        return render(request, "admin/users.html", {
            "users": list(db.scalars(select(User).order_by(User.display_name))),
            "memberships": {}, "groups": groups, "group": None,
            "error": _("Please enter one name per line (at most %(max)s).", max=MAX_BATCH),
        }, status_code=400)
    entries, created = [], []
    for name in wanted:
        user, code = create_user(db, name, role)
        entries.append((user.display_name, code))
        created.append((user.id, user.display_name))
    if group_id and db.get(Group, group_id):
        for user_id, _name in created:
            db.add(GroupMember(group_id=group_id, user_id=user_id))
        db.commit()
        distribution.distribute_group_to_users(group_id, created, _("Group decks for new members"))
    return print_sheet(request, entries)


@router.post("/print")
async def users_print(request: Request, admin: User = Depends(require_staff),
                      _csrf: User = Depends(verify_csrf), db: DbSession = Depends(get_db)):
    """New codes for the selected users + print sheet (old codes stop working).
    Group leaders: only members of their groups, never admins."""
    form = await request.form()
    ids = [str(i) for i in form.getlist("user_ids")]
    visible = visible_user_ids(db, admin)
    users = [u for u in (db.get(User, i) for i in ids)
             if u is not None and u.id != admin.id
             and (visible is None or (u.id in visible and not u.is_staff))]
    if not users:
        return RedirectResponse(str(form.get("back") or "/admin/users"), status_code=303)
    entries = [(u.display_name, reset_code(db, u)) for u in sorted(users, key=lambda u: u.display_name)]
    back = str(form.get("back") or "/admin/users")
    return print_sheet(request, entries, back=back if back.startswith("/admin") else "/admin/users")


@router.get("/{user_id}")
def user_detail(request: Request, user_id: str, admin: User = Depends(require_admin),
                db: DbSession = Depends(get_db), job: str | None = None, saved: int = 0):
    user = _get_user(db, user_id)
    member_of = set(db.scalars(select(GroupMember.group_id).where(GroupMember.user_id == user_id)))
    return render(request, "admin/user_detail.html", {
        "u": user, "groups": _groups(db), "member_of": member_of,
        "stats": db.get(UserStats, user_id), "job": job and distribution.get_job(job),
        "saved": bool(saved), "error": None, "is_self": user.id == admin.id,
    })


@router.post("/{user_id}")
async def user_update(request: Request, user_id: str, display_name: str = Form(...),
                      role: str = Form("learner"), admin: User = Depends(require_admin),
                      _csrf: User = Depends(verify_csrf), db: DbSession = Depends(get_db)):
    user = _get_user(db, user_id)
    form = await request.form()
    group_ids = {int(g) for g in form.getlist("groups") if str(g).isdigit()}
    valid = set(db.scalars(select(Group.id)))
    group_ids &= valid
    if role in ROLES and role != user.role:
        if user.id == admin.id and role != "admin":
            return _detail_error(request, db, user, admin, _("You cannot remove your own admin role."))
        user.role = role
    if display_name.strip():
        user.display_name = display_name.strip()[:100]
    current = set(db.scalars(select(GroupMember.group_id).where(GroupMember.user_id == user_id)))
    for gid in current - group_ids:
        db.query(GroupMember).filter_by(group_id=gid, user_id=user_id).delete()
    for gid in group_ids - current:
        db.add(GroupMember(group_id=gid, user_id=user_id))
    db.commit()
    job = None
    for gid in group_ids - current:
        job = distribution.distribute_group_to_users(
            gid, [(user.id, user.display_name)], _("Group decks for %(name)s", name=user.display_name)
        ) or job
    target = f"/admin/users/{user_id}?saved=1" + (f"&job={job.id}" if job else "")
    return RedirectResponse(target, status_code=303)


def _detail_error(request, db, user, admin, message: str):
    member_of = set(db.scalars(select(GroupMember.group_id).where(GroupMember.user_id == user.id)))
    return render(request, "admin/user_detail.html", {
        "u": user, "groups": _groups(db), "member_of": member_of,
        "stats": db.get(UserStats, user.id), "job": None, "saved": False,
        "error": message, "is_self": user.id == admin.id,
    }, status_code=400)


@router.post("/{user_id}/reset-code")
def user_reset_code(request: Request, user_id: str, admin: User = Depends(require_admin),
                    _csrf: User = Depends(verify_csrf), db: DbSession = Depends(get_db)):
    user = _get_user(db, user_id)
    code = reset_code(db, user)
    return print_sheet(request, [(user.display_name, code)], back=f"/admin/users/{user_id}")


@router.post("/{user_id}/toggle-disabled")
def user_toggle_disabled(request: Request, user_id: str, admin: User = Depends(require_admin),
                         _csrf: User = Depends(verify_csrf), db: DbSession = Depends(get_db)):
    user = _get_user(db, user_id)
    if user.id == admin.id:
        return _detail_error(request, db, user, admin, _("You cannot deactivate yourself."))
    user.disabled = not user.disabled
    if user.disabled:
        db.query(Session).filter_by(user_id=user.id).delete()
    db.commit()
    return RedirectResponse(f"/admin/users/{user_id}", status_code=303)


@router.post("/{user_id}/delete")
async def user_delete(request: Request, user_id: str, confirm_name: str = Form(""),
                      admin: User = Depends(require_admin), _csrf: User = Depends(verify_csrf),
                      db: DbSession = Depends(get_db)):
    user = _get_user(db, user_id)
    if user.id == admin.id:
        return _detail_error(request, db, user, admin, _("You cannot delete yourself."))
    if confirm_name.strip() != user.display_name:
        return _detail_error(request, db, user, admin,
                             _("The name does not match. The user was not deleted."))
    db.delete(user)  # sessions, stats, memberships cascade
    db.commit()
    await anki_service.delete_collection(user_id)
    return RedirectResponse("/admin/users", status_code=303)
