"""Admin hub: overview, switching to the admin collection, .apkg import, job progress."""

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app import anki_service, distribution
from app.auth import WORKSPACE_COOKIE, cookie_secure, require_admin, require_staff, verify_csrf
from app.db import get_db
from app.i18n import _
from app.models import Group, User
from app.routes.imports import import_upload
from app.templating import is_htmx, render

router = APIRouter(prefix="/admin")


@router.get("")
def home(request: Request, admin: User = Depends(require_staff), db: DbSession = Depends(get_db)):
    users = db.scalar(select(func.count()).select_from(User)) or 0
    groups = db.scalar(select(func.count()).select_from(Group)) or 0
    return render(request, "admin/home.html", {"users": users, "groups": groups,
                  "admin_ws": request.cookies.get(WORKSPACE_COOKIE) == "admin"})


@router.post("/workspace")
async def workspace(request: Request, admin: User = Depends(require_admin),
                    _csrf: User = Depends(verify_csrf)):
    form = await request.form()
    response = RedirectResponse("/", status_code=303)
    if form.get("ws") == "admin":
        await anki_service.ensure_collection(anki_service.ADMIN_COLLECTION)
        response.set_cookie(WORKSPACE_COOKIE, "admin", httponly=True, secure=cookie_secure(),
                            samesite="lax")
    else:
        response.delete_cookie(WORKSPACE_COOKIE)
    return response


@router.get("/import")
def import_page(request: Request, admin: User = Depends(require_admin)):
    return render(request, "admin/import.html", {"message": None, "error": None})


@router.post("/import")
async def import_submit(request: Request, file: UploadFile,
                        admin: User = Depends(require_admin), _csrf: User = Depends(verify_csrf)):
    # The admin is trusted: fields stay as they are (e.g. Bible Memorizer tables, styling).
    count, error = await import_upload(file, anki_service.ADMIN_COLLECTION, sanitize=False)
    ctx = {"message": None, "error": error}
    if not error:
        ctx["message"] = _("“%(file)s” imported: %(count)s notes new or updated. The decks are "
                           "now available in the admin collection.", file=file.filename, count=count)
    return render(request, "admin/import.html", ctx, status_code=400 if error else 200)


@router.get("/jobs/{job_id}")
def job_page(request: Request, job_id: str, back: str = "/admin",
             admin: User = Depends(require_staff)):
    job = distribution.get_job(job_id)
    if job is None:
        return render(request, "error.html", {"message": _("This task is no longer known.")}, 404)
    back = back if back.startswith("/admin") else "/admin"
    ctx = {"job": job, "back": back}
    if is_htmx(request):
        return render(request, "admin/partials/job.html", ctx)
    return render(request, "admin/job.html", ctx)
