"""Login codes, sessions, rate limiting, CSRF and role dependencies.

Login codes are never logged or stored in plain text (only HMAC-SHA256).
"""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from fastapi import Depends, Request
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session as DbSession

from app.config import get_settings
from app.db import get_db
from app.models import LoginAttempt, Session, User

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
USER_CODE_LEN = 8
ADMIN_CODE_LEN = 16
SESSION_COOKIE = "vf_session"
LOGIN_CSRF_COOKIE = "vf_login_csrf"
SESSION_DAYS = 180
MAX_FAILURES = 10
FAILURE_WINDOW = timedelta(minutes=15)
LOCKOUT = timedelta(minutes=15)


class LoginRequired(Exception):
    """Raised by require_user; turned into a redirect to /login."""


class Forbidden(Exception):
    pass


class CsrfError(Exception):
    pass


# --------------------------------------------------------------------------- codes


def generate_code(admin: bool = False) -> str:
    length = ADMIN_CODE_LEN if admin else USER_CODE_LEN
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def normalize_code(raw: str) -> str:
    """Accept lower case, dashes and spaces: 'k7mx-p3qr' -> 'K7MXP3QR'."""
    return "".join(ch for ch in raw.upper() if ch.isalnum())


def format_code(code: str) -> str:
    """'K7MXP3QR' -> 'K7MX-P3QR' (groups of four)."""
    return "-".join(code[i : i + 4] for i in range(0, len(code), 4))


def hash_code(code: str) -> str:
    key = get_settings().secret_key.encode()
    return hmac.new(key, normalize_code(code).encode(), hashlib.sha256).hexdigest()


def create_user(db: DbSession, display_name: str, role: str = "learner") -> tuple[User, str]:
    """Create a user with a fresh code. Returns (user, plain code) – show the code once."""
    while True:
        code = generate_code(admin=role == "admin")
        code_hash = hash_code(code)
        if db.scalar(select(User.id).where(User.code_hash == code_hash)) is None:
            break
    user = User(display_name=display_name.strip()[:100], role=role, code_hash=code_hash)
    db.add(user)
    db.commit()
    return user, code


def reset_code(db: DbSession, user: User) -> str:
    """Issue a new code; the old one stops working and all sessions end."""
    while True:
        code = generate_code(admin=user.is_admin)
        code_hash = hash_code(code)
        if db.scalar(select(User.id).where(User.code_hash == code_hash)) is None:
            break
    user.code_hash = code_hash
    db.execute(delete(Session).where(Session.user_id == user.id))
    db.commit()
    return code


# --------------------------------------------------------------------------- rate limit


def is_locked_out(db: DbSession, ip: str, now: datetime | None = None) -> bool:
    """Locked for LOCKOUT after MAX_FAILURES failures within FAILURE_WINDOW."""
    now = now or datetime.now()
    recent = db.scalars(
        select(LoginAttempt.ts)
        .where(LoginAttempt.ip == ip, LoginAttempt.success.is_(False))
        .where(LoginAttempt.ts > now - FAILURE_WINDOW - LOCKOUT)
        .order_by(LoginAttempt.ts.desc())
        .limit(MAX_FAILURES)
    ).all()
    if len(recent) < MAX_FAILURES:
        return False
    newest, oldest = recent[0], recent[-1]
    return newest - oldest <= FAILURE_WINDOW and now < newest + LOCKOUT


def authenticate(db: DbSession, raw_code: str, ip: str) -> User | None:
    """Check a code. Records the attempt. Caller must check is_locked_out first."""
    code = normalize_code(raw_code)
    user = None
    if len(code) in (USER_CODE_LEN, ADMIN_CODE_LEN):
        user = db.scalar(select(User).where(User.code_hash == hash_code(code)))
    ok = user is not None and not user.disabled
    db.add(LoginAttempt(ip=ip, success=ok))
    if ok:
        user.last_login_at = datetime.now()
    db.commit()
    return user if ok else None


def prune(db: DbSession) -> None:
    """Housekeeping: old login attempts and expired sessions."""
    now = datetime.now()
    db.execute(delete(LoginAttempt).where(LoginAttempt.ts < now - timedelta(days=1)))
    db.execute(delete(Session).where(Session.expires_at < now))
    db.commit()


# --------------------------------------------------------------------------- sessions


def _session_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def cookie_secure() -> bool:
    return get_settings().base_url.startswith("https://")


def create_session(db: DbSession, user: User, user_agent: str | None) -> str:
    """Returns the raw session id for the cookie."""
    raw = secrets.token_urlsafe(32)
    db.add(
        Session(
            id=_session_key(raw),
            user_id=user.id,
            expires_at=datetime.now() + timedelta(days=SESSION_DAYS),
            user_agent=(user_agent or "")[:300],
        )
    )
    db.commit()
    return raw


def set_session_cookie(response, raw: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        raw,
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        secure=cookie_secure(),
        samesite="lax",
    )


def delete_session(db: DbSession, raw: str | None) -> None:
    if raw:
        db.execute(delete(Session).where(Session.id == _session_key(raw)))
        db.commit()


def csrf_token_for(raw_session: str) -> str:
    key = get_settings().secret_key.encode()
    return hmac.new(key, b"csrf:" + raw_session.encode(), hashlib.sha256).hexdigest()


def current_user(request: Request, db: DbSession = Depends(get_db)) -> User | None:
    """Resolve the session cookie. Extends the session (sliding) at most once a day."""
    if hasattr(request.state, "user"):
        return request.state.user
    request.state.user = None
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    sess = db.get(Session, _session_key(raw))
    now = datetime.now()
    if sess is None or sess.expires_at < now:
        return None
    user = db.get(User, sess.user_id)
    if user is None or user.disabled:
        return None
    if sess.expires_at - now < timedelta(days=SESSION_DAYS - 1):
        sess.expires_at = now + timedelta(days=SESSION_DAYS)
        db.commit()
        request.state.refresh_cookie = raw
    request.state.user = user
    request.state.csrf_token = csrf_token_for(raw)
    return user


def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise LoginRequired()
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise Forbidden()
    return user


async def verify_csrf(request: Request, user: User = Depends(require_user)) -> User:
    """For every POST route: login required + CSRF token (form field or X-CSRF-Token)."""
    token = request.headers.get("X-CSRF-Token")
    if not token:
        form = await request.form()
        token = form.get("csrf_token")
    expected = getattr(request.state, "csrf_token", None)
    if not token or not expected or not hmac.compare_digest(str(token), expected):
        raise CsrfError()
    return user


def count_admins(db: DbSession) -> int:
    return db.scalar(select(func.count()).select_from(User).where(User.role == "admin")) or 0
