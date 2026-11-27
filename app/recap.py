"""«Итоги месяца»: one calendar month of the diary as a handful of facts and photos.

Plain counting over what is already logged; no model is involved, so it costs nothing.
"""

from collections import Counter
from datetime import date, timedelta
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import diary, usage
from app.models import Meal, User, Weight

MIN_DAYS = 3   # logged days before a month is worth a recap (and its push)
MIN_GUESSES = 3
TOP = 5
PHOTOS = 9     # a 3×3 collage

MONTHS_GEN = ("января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря")


def first_of(day: date) -> date:
    return day.replace(day=1)


def previous(first: date) -> date:
    return first_of(first - timedelta(days=1))


def following(first: date) -> date:
    return first_of(first + timedelta(days=32))


def _spread(items: list, n: int) -> list:
    """At most n items, evenly over the list: the collage shows the whole month, not its first week."""
    if len(items) <= n:
        return items
    return [items[round(k * (len(items) - 1) / (n - 1))] for k in range(n)]


def _longest_run(days: set[date]) -> int:
    best = 0
    for d in days:
        if d - timedelta(days=1) not in days:
            n = 1
            while d + timedelta(days=n) in days:
                n += 1
            best = max(best, n)
    return best


def build(db: Session, user: User, first: date, today: date) -> dict:
    """The month starting on `first`, up to `today` when it is the month still going."""
    nxt = following(first)
    last = min(nxt - timedelta(days=1), today)
    start, _ = usage.day_bounds_utc(user.timezone, first)
    _, end = usage.day_bounds_utc(user.timezone, last)
    meals = db.scalars(
        select(Meal).where(Meal.user_id == user.id, Meal.eaten_at >= start, Meal.eaten_at < end)
        .options(selectinload(Meal.items)).order_by(Meal.eaten_at)
    ).all()

    days: dict[date, list[float]] = {}
    dishes: Counter[str] = Counter()
    names: dict[str, str] = {}
    misses = []
    for m in meals:
        t = days.setdefault(diary.local_day(m.eaten_at, user.timezone), [0.0, 0.0])
        kcal = 0.0
        for i in m.items:
            kcal += i.kcal_100 * i.grams / 100
            t[1] += i.protein_100 * i.grams / 100
        t[0] += kcal
        # A dish counts once per meal, however it was split into lines.
        for key in {i.name.strip().lower() for i in m.items if i.name.strip()}:
            dishes[key] += 1
        for i in m.items:
            names.setdefault(i.name.strip().lower(), i.name.strip())
        if m.guess_kcal is not None and kcal > 0:
            misses.append(abs(m.guess_kcal - kcal) / kcal)

    in_goal = [d for d, (k, _) in days.items() if diary.in_goal(k, user.kcal_goal)]
    protein_ok = [d for d, (_, p) in days.items() if user.protein_goal > 0 and p >= diary.PROTEIN_OK * user.protein_goal]
    # The best day: in the calorie corridor with the most protein.
    best = max(in_goal, key=lambda d: (days[d][1], d), default=None)
    weights = db.scalars(
        select(Weight).where(Weight.user_id == user.id, Weight.day >= first, Weight.day <= last).order_by(Weight.day)
    ).all()
    earlier = db.scalar(select(Meal.id).where(Meal.user_id == user.id, Meal.eaten_at < start).limit(1))
    return {
        "month": first.strftime("%Y-%m"),
        "complete": nxt <= today,
        "prev": previous(first).strftime("%Y-%m") if earlier is not None else None,
        "next": nxt.strftime("%Y-%m") if nxt <= today else None,
        "days_total": (last - first).days + 1,
        "logged_days": len(days),
        "longest_run": _longest_run(set(days)),
        "meals": len(meals),
        "in_goal_days": len(in_goal),
        "protein_days": len(protein_ok),
        "avg_kcal": round(sum(k for k, _ in days.values()) / len(days)) if days else 0,
        "best_day": {"date": best.isoformat(), "kcal": round(days[best][0]), "protein": round(days[best][1])} if best else None,
        "top": [{"name": names[key], "times": n} for key, n in sorted(dishes.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP]],
        "photos": [{"photo": m.photo, "date": diary.local_day(m.eaten_at, user.timezone).isoformat()}
                   for m in _spread([m for m in meals if m.photo], PHOTOS)],
        "weight": ({"from": weights[0].kg, "to": weights[-1].kg, "delta": round(weights[-1].kg - weights[0].kg, 1)}
                   if len(weights) >= 2 else None),
        "guess": ({"n": len(misses), "off_percent": round(median(misses) * 100)}
                  if len(misses) >= MIN_GUESSES else None),
    }


def latest(db: Session, user: User, today: date) -> dict:
    """What the app offers by itself: the month that just ended, or the one going on while
    the diary is younger than that."""
    done = build(db, user, previous(first_of(today)), today)
    return done if done["logged_days"] >= MIN_DAYS else build(db, user, first_of(today), today)


def push_text(recap: dict) -> tuple[str, str]:
    month = MONTHS_GEN[int(recap["month"][5:]) - 1]
    body = f"Дней с записями: {recap['logged_days']}, в норме по калориям: {recap['in_goal_days']}."
    if recap["top"]:
        body += f" Чаще всего: {recap['top'][0]['name'].lower()}."
    return f"📅 Итоги {month}", body
