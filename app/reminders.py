"""Daily study reminders via Web Push (VAPID). Sent from the housekeeping loop.

The VAPID key pair is generated on first use and kept in DATA_DIR (persistent); if it
is lost, browsers have to subscribe again. Times are wall-clock times in the server TZ.
"""

import asyncio
import base64
import json
import logging
import re
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid02
from pywebpush import WebPushException, webpush
from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from app import anki_service
from app.config import get_settings
from app.db import get_sessionmaker
from app.i18n import N_, translate
from app.models import PushSubscription, Reminder

log = logging.getLogger(__name__)

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DEFAULT_TIME = "19:00"
PUSH_TTL = 6 * 3600  # a reminder older than that is pointless
# Push services of the major browsers; the server only ever POSTs to these (no SSRF).
PUSH_HOSTS = (".googleapis.com", ".mozilla.com", ".mozaws.net", ".push.apple.com",
              ".notify.windows.com")


class InvalidSubscription(ValueError):
    pass


def _key_path() -> Path:
    return get_settings().data_dir / "vapid_private.pem"


@lru_cache
def _vapid(path: Path) -> Vapid02:
    if path.exists():
        return Vapid02.from_file(str(path))
    key = Vapid02()
    key.generate_keys()
    key.save_key(str(path))
    path.chmod(0o600)
    log.info("Generated VAPID key pair in %s", path)
    return key


def vapid() -> Vapid02:
    return _vapid(_key_path())


def public_key() -> str:
    """applicationServerKey for PushManager.subscribe (base64url, uncompressed point)."""
    raw = vapid().public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def validate_endpoint(endpoint: str) -> str:
    parts = urlsplit(endpoint or "")
    host = (parts.hostname or "").lower()
    if (parts.scheme != "https" or len(endpoint) > 1000
            or not any(host.endswith(suffix) for suffix in PUSH_HOSTS)):
        raise InvalidSubscription(endpoint)
    return endpoint


def subscribe(db: DbSession, user_id: str, endpoint: str, p256dh: str, auth: str,
              time: str, lang: str, now: datetime | None = None) -> None:
    """Register this device and set the user's reminder time."""
    if not TIME_RE.match(time or ""):
        raise InvalidSubscription(time)
    validate_endpoint(endpoint)
    if not p256dh or not auth or len(p256dh) > 200 or len(auth) > 100:
        raise InvalidSubscription("keys")
    now = now or datetime.now()
    sub = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == endpoint))
    if sub is None:
        sub = PushSubscription(endpoint=endpoint)
        db.add(sub)
    sub.user_id, sub.p256dh, sub.auth = user_id, p256dh, auth
    reminder = db.get(Reminder, user_id) or Reminder(user_id=user_id)
    if reminder.time != time:
        # A time that has already passed today starts tomorrow (no instant reminder).
        reminder.last_sent_on = now.date() if time <= now.strftime("%H:%M") else None
    reminder.time, reminder.lang = time, lang
    db.merge(reminder)
    db.commit()


def unsubscribe(db: DbSession, user_id: str, endpoint: str) -> None:
    """Remove this device; without devices left the reminder is switched off."""
    db.execute(delete(PushSubscription).where(
        PushSubscription.user_id == user_id, PushSubscription.endpoint == endpoint))
    _drop_reminder_without_devices(db, user_id)
    db.commit()


def _drop_reminder_without_devices(db: DbSession, user_id: str) -> None:
    if db.scalar(select(PushSubscription.id).where(PushSubscription.user_id == user_id)) is None:
        db.execute(delete(Reminder).where(Reminder.user_id == user_id))


def get_reminder(db: DbSession, user_id: str) -> Reminder | None:
    return db.get(Reminder, user_id)


def _send(sub: PushSubscription, payload: dict) -> bool:
    """Push one message. False if the subscription is gone (expired/revoked)."""
    settings = get_settings()
    try:
        webpush(
            {"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
            json.dumps(payload),
            vapid_private_key=vapid(),
            vapid_claims={"sub": settings.base_url},
            ttl=PUSH_TTL,
            timeout=10,
        )
    except WebPushException as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in (404, 410):
            return False
        log.warning("Push to %s failed: %s", urlsplit(sub.endpoint).hostname, status or exc)
    return True


async def push_to_user(db: DbSession, user_id: str, payload: dict) -> int:
    """Send to all devices of a user, drop dead subscriptions. Returns devices reached."""
    subs = db.scalars(select(PushSubscription).where(PushSubscription.user_id == user_id)).all()
    reached = 0
    for sub in subs:
        if await asyncio.to_thread(_send, sub, payload):
            reached += 1
        else:
            db.delete(sub)
    if reached < len(subs):
        db.flush()
        _drop_reminder_without_devices(db, user_id)
    db.commit()
    return reached


def reminder_payload(lang: str, cards: int | None = None) -> dict:
    body = (translate(N_("Cards waiting for you today: %(num)s"), lang) % {"num": cards}
            if cards is not None
            else translate(N_("Reminders are working."), lang))
    return {"title": translate(N_("Time to review"), lang), "body": body, "url": "/"}


async def send_due(now: datetime | None = None) -> int:
    """Send today's reminders whose time has come; only if cards are waiting."""
    now = now or datetime.now()
    today: date = now.date()
    sent = 0
    with get_sessionmaker()() as db:
        due = db.scalars(select(Reminder).where(Reminder.time <= now.strftime("%H:%M"))).all()
        for reminder in due:
            if reminder.last_sent_on == today:
                continue
            reminder.last_sent_on = today
            db.commit()
            try:
                stats = await anki_service.collection_stats(reminder.user_id, days=1)
            except Exception:  # noqa: BLE001 - one broken collection must not stop the rest
                log.exception("Reminder for %s: reading the collection failed", reminder.user_id)
                continue
            cards = stats.due_today + stats.new_today
            if cards and await push_to_user(db, reminder.user_id,
                                            reminder_payload(reminder.lang, cards)):
                sent += 1
    return sent
