"""Statistics page for learners: heatmap, forecast, card counts."""

import math
from datetime import timedelta

from fastapi import APIRouter, Depends, Request

from app import anki_service
from app.auth import current_collection, require_user
from app.models import User
from app.templating import render

router = APIRouter()


def _heatmap_weeks(days: list[tuple]) -> list[list[dict | None]]:
    """Group days into Monday-based week columns; cells get a level 0–4."""
    peak = max((n for _, n in days), default=0)
    first = days[0][0]
    pad = [None] * first.weekday()
    cells = pad + [
        {"day": d, "count": n, "level": 0 if not n else min(4, math.ceil(4 * n / peak))}
        for d, n in days
    ]
    return [cells[i:i + 7] for i in range(0, len(cells), 7)]


HEATMAP_WEEKS = 16  # "Last 16 weeks" on the scoreboard


def _recent_weeks(weeks: list[list[dict | None]]) -> list[list[dict | None]]:
    """The last weeks, the current one filled up to Sunday with future days."""
    recent = [list(w) for w in weeks[-HEATMAP_WEEKS:]]
    recent[-1] += [{"future": True}] * (7 - len(recent[-1]))
    return recent


@router.get("/stats")
async def stats_page(request: Request, deck: int | None = None,
                     user: User = Depends(require_user), col: str = Depends(current_collection)):
    decks = await anki_service.deck_names(col)
    if deck not in dict(decks):
        deck = None
    s = await anki_service.learner_stats(col, deck)
    peak = max((n for _, n in s.forecast), default=0)
    forecast = [{"day": d, "count": n, "pct": round(100 * n / peak) if peak else 0}
                for d, n in s.forecast]
    week_end = s.forecast[0][0] + timedelta(days=6)
    weeks = _recent_weeks(_heatmap_weeks(s.heatmap))
    return render(request, "stats.html", {
        "s": s, "decks": decks, "deck": deck, "weeks": weeks, "this_week": weeks[-1],
        "forecast": forecast, "tomorrow": s.forecast[1][1] if len(s.forecast) > 1 else 0,
        "next_week": sum(n for d, n in s.forecast if d <= week_end),
    })
