"""Admin: progress table per group (from the user_stats/daily_reviews cache)."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app import distribution
from app.auth import require_staff, verify_csrf, visible_group_ids, visible_user_ids
from app.db import get_db
from app.i18n import _
from app.models import DailyReviews, Group, GroupMember, User, UserStats
from app.templating import render

router = APIRouter(prefix="/admin/progress")

INACTIVE_DAYS = 7
MAX_DUE = 100
SORT_KEYS = ("name", "last", "today", "week", "due", "retention", "streak")


@dataclass
class Row:
    user_id: str
    name: str
    last_review_at: datetime | None
    reviews_today: int
    reviews_week: int
    due: int
    retention: float | None
    streak: int
    alert: bool


def _rows(db: DbSession, group_id: int | None, viewer: User) -> list[Row]:
    query = select(User).where(User.role != "admin").order_by(User.display_name)
    visible = visible_user_ids(db, viewer)
    if visible is not None:
        query = query.where(User.id.in_(visible))
    if group_id:
        query = query.join(GroupMember, GroupMember.user_id == User.id).where(
            GroupMember.group_id == group_id)
    users = list(db.scalars(query))
    stats = {s.user_id: s for s in db.scalars(select(UserStats))}
    week_start = date.today() - timedelta(days=6)
    week = dict(db.execute(
        select(DailyReviews.user_id, func.sum(DailyReviews.reviews))
        .where(DailyReviews.day >= week_start).group_by(DailyReviews.user_id)
    ).all())
    today = date.today()
    inactive_before = datetime.now() - timedelta(days=INACTIVE_DAYS)
    rows = []
    for u in users:
        s = stats.get(u.id)
        last = s.last_review_at if s else None
        # reviews_today in the cache is only valid if it was updated today
        reviews_today = s.reviews_today if s and s.updated_at.date() == today else 0
        due = s.due_today if s else 0
        rows.append(Row(
            user_id=u.id, name=u.display_name, last_review_at=last,
            reviews_today=reviews_today, reviews_week=int(week.get(u.id) or 0), due=due,
            retention=s.retention_30d if s else None, streak=s.streak_days if s else 0,
            alert=last is None or last < inactive_before or due > MAX_DUE,
        ))
    return rows


def _sort(rows: list[Row], key: str, desc: bool) -> list[Row]:
    getters = {
        "name": lambda r: r.name.lower(),
        "last": lambda r: r.last_review_at or datetime.min,
        "today": lambda r: r.reviews_today,
        "week": lambda r: r.reviews_week,
        "due": lambda r: r.due,
        "retention": lambda r: -1 if r.retention is None else r.retention,
        "streak": lambda r: r.streak,
    }
    return sorted(rows, key=getters.get(key, getters["name"]), reverse=desc)


def _groups(db: DbSession, viewer: User) -> list[Group]:
    query = select(Group).order_by(Group.name)
    visible = visible_group_ids(db, viewer)
    if visible is not None:
        query = query.where(Group.id.in_(visible))
    return list(db.scalars(query))


@router.get("")
def progress(request: Request, group: int | None = None, sort: str = "name", desc: int = 0,
             admin: User = Depends(require_staff), db: DbSession = Depends(get_db)):
    groups = _groups(db, admin)
    if group is not None and group not in {g.id for g in groups}:
        group = None
    sort = sort if sort in SORT_KEYS else "name"
    rows = _sort(_rows(db, group, admin), sort, bool(desc))
    return render(request, "admin/progress.html", {
        "groups": groups, "group": group, "rows": rows, "sort": sort, "desc": bool(desc),
        "inactive_days": INACTIVE_DAYS, "max_due": MAX_DUE,
    })


@router.post("/refresh")
async def refresh(group: int | None = Form(None), admin: User = Depends(require_staff),
                  _csrf: User = Depends(verify_csrf), db: DbSession = Depends(get_db)):
    if group is not None and group not in {g.id for g in _groups(db, admin)}:
        group = None
    rows = _rows(db, group, admin)
    job = distribution.refresh_stats([(r.user_id, r.name) for r in rows], _("Refresh figures"))
    back = f"/admin/progress?group={group}" if group else "/admin/progress"
    return RedirectResponse(f"/admin/jobs/{job.id}?back={quote(back)}", status_code=303)
