"""Badges: things the diary shows a person has done. Counted from the diary each time,
so nothing is stored and nothing can drift from what is actually logged."""

from calendar import monthrange
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import diary, recap
from app.models import Food, Meal, MealItem, User

SNIPER_RUN = 3
SNIPER_MISS = 0.10


def _best_run(flags: list[bool]) -> int:
    best = n = 0
    for ok in flags:
        n = n + 1 if ok else 0
        best = max(best, n)
    return best


def _full_months(days: set[date], today: date) -> int:
    """Finished calendar months with a record on every day."""
    per_month: dict[tuple[int, int], int] = {}
    for d in days:
        per_month[(d.year, d.month)] = per_month.get((d.year, d.month), 0) + 1
    return sum(1 for (y, m), n in per_month.items()
               if (y, m) < (today.year, today.month) and n == monthrange(y, m)[1])


def earned(db: Session, user: User, today: date) -> list[dict]:
    kcal = func.coalesce(func.sum(MealItem.kcal_100 * MealItem.grams / 100), 0.0)
    protein = func.coalesce(func.sum(MealItem.protein_100 * MealItem.grams / 100), 0.0)
    meals = db.execute(
        select(Meal.eaten_at, Meal.photo, Meal.guess_kcal, kcal, protein)
        .outerjoin(MealItem, MealItem.meal_id == Meal.id)
        .where(Meal.user_id == user.id).group_by(Meal.id).order_by(Meal.eaten_at)
    ).all()
    days: dict[date, list[float]] = {}
    for at, _, _, k, p in meals:
        t = days.setdefault(diary.local_day(at, user.timezone), [0.0, 0.0])
        t[0] += k
        t[1] += p
    in_goal = {d for d, (k, _) in days.items() if diary.in_goal(k, user.kcal_goal)}
    protein_ok = {d for d, (_, p) in days.items()
                  if user.protein_goal > 0 and p >= diary.PROTEIN_OK * user.protein_goal}
    logged_run = recap.longest_run(set(days))
    sniper = _best_run([abs(g - k) / k <= SNIPER_MISS for _, _, g, k, _ in meals if g is not None and k > 0])
    photos = sum(1 for m in meals if m.photo)
    dishes = db.scalar(select(func.count()).select_from(Food)
                       .where(Food.source == "recipe", Food.owner_id == user.id)) or 0

    def badge(key, icon, title, hint, value, target):
        return {"key": key, "icon": icon, "title": title, "hint": hint,
                "value": min(value, target), "target": target, "earned": value >= target}

    return [
        badge("first", "plate", "Первая тарелка", "Записать первый приём пищи", len(meals), 1),
        badge("plates_100", "bowl", "100 тарелок", "Записать 100 приёмов пищи", len(meals), 100),
        badge("plates_500", "pot", "500 тарелок", "Записать 500 приёмов пищи", len(meals), 500),
        badge("run_7", "flame", "Неделя подряд", "7 дней подряд с записями", logged_run, 7),
        badge("run_30", "flame", "30 дней подряд", "30 дней подряд с записями", logged_run, 30),
        badge("run_100", "flame", "100 дней подряд", "100 дней подряд с записями", logged_run, 100),
        badge("full_month", "flag", "Месяц без пропусков", "Календарный месяц, где записан каждый день",
              _full_months(set(days), today), 1),
        badge("goal_7", "target", "Неделя в норме", "7 дней подряд в коридоре 85–105% нормы",
              recap.longest_run(in_goal), 7),
        badge("protein_7", "dumbbell", "Белковая неделя", "7 дней подряд с белком от 90% нормы",
              recap.longest_run(protein_ok), 7),
        badge("sniper", "camera", "Снайпер", "3 догадки подряд в «Угадай калории» с промахом до 10%",
              sniper, SNIPER_RUN),
        badge("photo_50", "image", "Фотограф", "50 приёмов пищи с фото", photos, 50),
        badge("chef", "egg", "Шеф", "Сохранить своё блюдо", dishes, 1),
    ]
