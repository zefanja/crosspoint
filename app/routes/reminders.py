"""Daily study reminder: register/unregister this device for Web Push."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session as DbSession

from app import i18n, reminders
from app.auth import verify_csrf
from app.db import get_db
from app.models import User

router = APIRouter()


async def _json(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


@router.post("/reminder")
async def reminder_on(request: Request, user: User = Depends(verify_csrf),
                      db: DbSession = Depends(get_db)):
    """Body: {"endpoint", "keys": {"p256dh", "auth"}, "time": "HH:MM"} (PushSubscription JSON)."""
    data = await _json(request)
    keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
    try:
        reminders.subscribe(db, user.id, str(data.get("endpoint", "")),
                            str(keys.get("p256dh", "")), str(keys.get("auth", "")),
                            str(data.get("time", "")), i18n.get_language())
    except reminders.InvalidSubscription:
        return Response(status_code=400)
    return Response(status_code=204)


@router.post("/reminder/off")
async def reminder_off(request: Request, user: User = Depends(verify_csrf),
                       db: DbSession = Depends(get_db)):
    data = await _json(request)
    reminders.unsubscribe(db, user.id, str(data.get("endpoint", "")))
    return Response(status_code=204)


@router.post("/reminder/test")
async def reminder_test(user: User = Depends(verify_csrf), db: DbSession = Depends(get_db)):
    """Send a test notification to all devices of the user."""
    reached = await reminders.push_to_user(
        db, user.id, reminders.reminder_payload(i18n.get_language()))
    return JSONResponse({"reached": reached})
