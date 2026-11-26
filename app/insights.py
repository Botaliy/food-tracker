"""Free, everyday help from the diary itself: no model calls.

- observations: patterns over the last two weeks worth knowing (late eating, protein at
  breakfast, weekends), each only when it's clear in the numbers;
- suggestions: which of the user's own usual meals fit what's left of today.
"""

from datetime import UTC, date, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import usage
from app.models import Meal, User

WINDOW_DAYS = 14
MIN_DAYS = 5
EVENING_HOUR = 18


def _totals(items) -> tuple[float, float]:
    kcal = sum(i.kcal_100 * i.grams / 100 for i in items)
    protein = sum(i.protein_100 * i.grams / 100 for i in items)
    return kcal, protein


def _meals(db: Session, user: User, first: date, last: date) -> list[Meal]:
    start, _ = usage.day_bounds_utc(user.timezone, first)
    _, end = usage.day_bounds_utc(user.timezone, last)
    return list(db.scalars(
        select(Meal).where(Meal.user_id == user.id, Meal.eaten_at >= start, Meal.eaten_at < end)
        .options(selectinload(Meal.items))
    ))


def observations(db: Session, user: User, today: date) -> list[dict]:
    """Up to four findings, most useful first. Today is left out: it isn't over."""
    last = today - timedelta(days=1)
    zone = ZoneInfo(user.timezone)
    days: dict[date, dict] = {}
    foods: dict[str, float] = {}
    for m in _meals(db, user, today - timedelta(days=WINDOW_DAYS), last):
        local = m.eaten_at.replace(tzinfo=UTC).astimezone(zone)
        kcal, protein = _totals(m.items)
        d = days.setdefault(local.date(), {"kcal": 0.0, "evening": 0.0, "breakfast_protein": None})
        d["kcal"] += kcal
        if local.hour >= EVENING_HOUR:
            d["evening"] += kcal
        if local.hour < 11:
            d["breakfast_protein"] = (d["breakfast_protein"] or 0) + protein
        for i in m.items:
            key = i.name.strip()
            foods[key] = foods.get(key, 0.0) + i.kcal_100 * i.grams / 100
    if len(days) < MIN_DAYS:
        return []

    out = []
    total = sum(d["kcal"] for d in days.values()) or 1
    evening = 100 * sum(d["evening"] for d in days.values()) / total
    if evening >= 45:
        out.append({"icon": "🌙", "text": f"После {EVENING_HOUR}:00 — {round(evening)}% калорий дня. "
                                          "Плотнее обед — и вечером легче удержаться в норме."})

    breakfasts = [d["breakfast_protein"] for d in days.values() if d["breakfast_protein"] is not None]
    if len(breakfasts) >= 3 and sum(breakfasts) / len(breakfasts) < 20 and user.protein_goal >= 80:
        avg = round(sum(breakfasts) / len(breakfasts))
        out.append({"icon": "🍳", "text": f"На завтрак в среднем {avg} г белка. Яйца, творог или йогурт "
                                          "закроют 20–30 г — и норма за день наберётся проще."})

    weekday = [d["kcal"] for day, d in days.items() if day.weekday() < 5]
    weekend = [d["kcal"] for day, d in days.items() if day.weekday() >= 5]
    if len(weekday) >= 3 and len(weekend) >= 2:
        diff = sum(weekend) / len(weekend) - sum(weekday) / len(weekday)
        if diff >= 300:
            out.append({"icon": "🎉", "text": f"В выходные в среднем на {round(diff)} ккал больше, чем в будни."})

    if foods:
        name, kcal = max(foods.items(), key=lambda kv: kv[1])
        share = 100 * kcal / total
        if share >= 12:
            out.append({"icon": "🔎", "text": f"«{name}» — {round(share)}% всех калорий за две недели."})
    return out[:4]


def suggestions(left_kcal: float, left_protein: float, usual: list[dict], limit: int = 3) -> list[dict]:
    """Usual meals (as /api/meals/recent returns them) that fit what's left, best first:
    filling most of the remaining calories, with protein where protein is short."""
    if left_kcal < 120:
        return []
    fits = []
    for m in usual:
        kcal, protein = m["totals"]["kcal"], m["totals"]["protein"]
        if kcal <= 0 or kcal > left_kcal * 1.05:
            continue
        score = kcal / left_kcal  # use the room, don't leave it empty
        if left_protein > 15:
            score += min(protein, left_protein) / left_protein
        fits.append((score, m))
    fits.sort(key=lambda sm: -sm[0])
    return [m for _, m in fits[:limit]]
