"""Dashboard cache: user_stats / daily_reviews, refreshed after reviews and logins."""

import logging
from datetime import datetime

from sqlalchemy import delete

from app import anki_service
from app.db import get_sessionmaker
from app.models import DailyReviews, UserStats

log = logging.getLogger(__name__)


async def update_after_review(user_id: str) -> None:
    """Recompute the cached figures from the collection (the source of truth)."""
    try:
        s = await anki_service.collection_stats(user_id)
    except Exception:  # noqa: BLE001 - a stale cache must never break reviewing
        log.exception("Updating stats for %s failed", user_id)
        return
    with get_sessionmaker()() as db:
        row = db.get(UserStats, user_id) or UserStats(user_id=user_id)
        row.total_cards = s.total_cards
        row.due_today = s.due_today
        row.new_today = s.new_today
        row.reviews_today = s.reviews_today
        row.retention_30d = s.retention_30d
        row.streak_days = s.streak_days
        row.last_review_at = s.last_review_at
        row.updated_at = datetime.now()
        db.merge(row)
        if s.daily:
            oldest = s.daily[-1].day
            db.execute(
                delete(DailyReviews).where(
                    DailyReviews.user_id == user_id, DailyReviews.day >= oldest
                )
            )
            for d in s.daily:
                db.add(
                    DailyReviews(
                        user_id=user_id,
                        day=d.day,
                        reviews=d.reviews,
                        again_count=d.again,
                        new_count=d.new,
                    )
                )
        db.commit()


# Logins refresh the cache too (due counts change with the day).
update_after_login = update_after_review
