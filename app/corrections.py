"""What a user keeps fixing after recognition, as short hints for the next one.

A recognized item is saved with the grams the model suggested (MealItem.ai_grams). If the
user consistently changes a food's weight, or adds the same thing by hand to recognized
meals (oil, a sauce), the model is told so next time. The hints are the user's history,
not rules: the photo still wins.
"""

from datetime import timedelta
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Meal, User, utcnow

LOOKBACK_DAYS = 60
MIN_SAMPLES = 3        # per food, to call it a habit
MIN_SHIFT = 0.15       # a 15% change either way is worth mentioning
MIN_OVERALL = 8        # recognized items before a general "portions run bigger"
MAX_HINTS = 6


def _key(name: str) -> str:
    return " ".join(name.lower().split())


def hints(db: Session, user: User) -> list[str]:
    meals = db.scalars(
        select(Meal)
        .where(Meal.user_id == user.id, Meal.created_at >= utcnow() - timedelta(days=LOOKBACK_DAYS))
        .options(selectinload(Meal.items))
    ).all()
    ratios: dict[str, list[float]] = {}
    names: dict[str, str] = {}
    added: dict[str, list[float]] = {}
    all_ratios: list[float] = []
    for meal in meals:
        recognized = [i for i in meal.items if i.ai_grams]
        if not recognized:
            continue
        for i in meal.items:
            k = _key(i.name)
            names.setdefault(k, i.name)
            if i.ai_grams:
                r = i.grams / i.ai_grams
                ratios.setdefault(k, []).append(r)
                all_ratios.append(r)
            else:
                added.setdefault(k, []).append(i.grams)

    out = []
    for k, rs in sorted(ratios.items(), key=lambda kv: -len(kv[1])):
        m = median(rs)
        if len(rs) >= MIN_SAMPLES and abs(m - 1) >= MIN_SHIFT:
            out.append(f"«{names[k]}»: пользователь обычно меняет вес на ×{m:.2f} от твоей оценки ({len(rs)} раз)")
    for k, gs in sorted(added.items(), key=lambda kv: -len(kv[1])):
        if len(gs) >= MIN_SAMPLES:
            out.append(f"«{names[k]}»: пользователь часто добавляет это сам, обычно ~{round(median(gs))} г ({len(gs)} раз)")
    if len(all_ratios) >= MIN_OVERALL and abs(median(all_ratios) - 1) >= MIN_SHIFT and not out:
        out.append(f"в целом пользователь правит порции на ×{median(all_ratios):.2f} от твоих оценок")
    return out[:MAX_HINTS]
