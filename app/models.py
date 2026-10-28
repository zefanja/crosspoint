"""app.db tables. New tables only via Alembic migration."""

import os
import time
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    """26-char ULID: 48-bit millisecond timestamp + 80 random bits, Crockford base32."""
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD[(value >> (5 * i)) & 31] for i in reversed(range(26)))


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_ulid)
    display_name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(10), default="learner")  # learner|leader|admin
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_staff(self) -> bool:
        """Admins and group leaders (dashboard access)."""
        return self.role in ("admin", "leader")


class Session(Base):
    __tablename__ = "sessions"

    # SHA-256 of the cookie value; the raw session id is never stored.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    user_agent: Mapped[str | None] = mapped_column(String(300))


class LoginAttempt(Base):
    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ip: Mapped[str] = mapped_column(String(64), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    success: Mapped[bool] = mapped_column(Boolean)


class UserStats(Base):
    """Dashboard cache; the collection stays the source of truth."""

    __tablename__ = "user_stats"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    total_cards: Mapped[int] = mapped_column(Integer, default=0)
    due_today: Mapped[int] = mapped_column(Integer, default=0)
    new_today: Mapped[int] = mapped_column(Integer, default=0)
    reviews_today: Mapped[int] = mapped_column(Integer, default=0)
    retention_30d: Mapped[float | None] = mapped_column(Float)
    streak_days: Mapped[int] = mapped_column(Integer, default=0)
    last_review_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class DailyReviews(Base):
    __tablename__ = "daily_reviews"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    reviews: Mapped[int] = mapped_column(Integer, default=0)
    again_count: Mapped[int] = mapped_column(Integer, default=0)
    new_count: Mapped[int] = mapped_column(Integer, default=0)


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class GroupMember(Base):
    __tablename__ = "group_members"

    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class GroupDeck(Base):
    """A deck of the admin collection assigned to a group."""

    __tablename__ = "group_decks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), index=True)
    admin_deck_id: Mapped[int] = mapped_column(Integer)
    deck_name: Mapped[str] = mapped_column(String(200))
    last_distributed_at: Mapped[datetime | None] = mapped_column(DateTime)


class GroupLeader(Base):
    """Group leaders see progress and can print codes for their groups only."""

    __tablename__ = "group_leaders"

    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class GroupDeckEdit(Base):
    """Who changed which note of the admin collection (shared editing of group decks)."""

    __tablename__ = "group_deck_edits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    note_id: Mapped[int] = mapped_column(Integer, index=True)
    deck_id: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(10))  # add|edit|delete
    summary: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
