"""A weekly look at the diary by Claude: what went well, what to change.

Only aggregates go to the model (daily totals, meal timing, the foods that
brought the most calories, weigh-ins), never photos or the user's name.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, timedelta
from zoneinfo import ZoneInfo

import anthropic
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import diary, foods, usage
from app.config import get_settings
from app.models import Activity, Meal, Review, User, Weight
from app.recognize import RecognizeError as ReviewError
from app.recognize import RecognizeErrorWithResponse as ReviewErrorWithResponse
from app.recognize import _client

logger = logging.getLogger(__name__)

REVIEW_DAYS = 7
MIN_LOGGED_DAYS = 3
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
# Local hour -> part of the day, for "most calories come in the evening" kind of advice.
PARTS = [(0, "ночь"), (5, "утро"), (11, "день"), (16, "вечер"), (22, "ночь")]


class ReviewOut(BaseModel):
    summary: str
    good: list[str]
    improve: list[str]
    tips: list[str]


SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "1-2 предложения: главное о неделе."},
        "good": {"type": "array", "items": {"type": "string"}, "description": "1-3 пункта: что получается хорошо."},
        "improve": {"type": "array", "items": {"type": "string"}, "description": "1-3 пункта: что мешает цели, с цифрами из данных."},
        "tips": {"type": "array", "items": {"type": "string"}, "description": "2-4 конкретных совета на следующую неделю."},
    },
    "required": ["summary", "good", "improve", "tips"],
    "additionalProperties": False,
}

SYSTEM = """Ты разбираешь неделю дневника питания в трекере калорий и даёшь короткий обзор по-русски, обращаясь на «ты».

Данные — суммы, посчитанные приложением: калории и БЖУ по дням, доля калорий по времени суток, продукты, давшие больше всего калорий, взвешивания. Если есть, по дням добавлены активные калории и шаги с Apple Watch; часы обычно завышают активные калории на 20–40%, так что сравнивай движение между днями, а не складывай его с нормой. Нормы пользователь задал сам. Клетчатка, сахар и соль (fiber_g, sugar_g, salt_g) есть не у всех продуктов, поэтому это нижняя оценка, а если ключа нет — данных за день нет; ориентиры в reference_per_day: клетчатка 14 г на 1000 ккал нормы, соль не больше 5 г; сахар — это весь сахар (вместе с фруктовым и молочным), а не добавленный, ориентира для него нет. Дни без записей — это пропуски в дневнике, а не голодание; не делай из них выводов о питании.

Как писать:
- Опирайся на цифры из данных и называй их: «белок в среднем 85 г из 120», «вечером съедается 48% калорий».
- Советы конкретные и выполнимые: что заменить, добавить или перенести, с примерами обычной еды из того, что человек уже ест.
- Каждый пункт — одно короткое предложение. Без вступлений, без общих фраз вроде «пей больше воды», если данные этого не подсказывают.
- Если неделя хорошая, так и скажи, не выдумывай проблем.

Ты не врач. Не ставь диагнозов и не советуй жёстких диет, голодания или добавок. Если калорий стабильно меньше 1200 в день или вес падает быстрее 1 кг в неделю, мягко посоветуй обсудить это с врачом."""


@dataclass
class ReviewResult:
    review: ReviewOut
    response: object = field(repr=False)


def _part_of_day(hour: int) -> str:
    return next(name for start, name in reversed(PARTS) if hour >= start)


def build_digest(meals, weights, goals: dict, tz: str, today: date, activity=()) -> dict:
    """The week as plain numbers. `meals` carry `.eaten_at` (naive UTC) and `.items`;
    `activity` rows carry `.day`, `.active_kcal` and `.steps`, for the days the watch sent."""
    first = today - timedelta(days=REVIEW_DAYS - 1)
    zone = ZoneInfo(tz)
    days = {first + timedelta(days=k): {"kcal": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0, "meals": 0}
            for k in range(REVIEW_DAYS)}
    day_items: dict[date, list] = {d: [] for d in days}
    parts: dict[str, float] = {}
    foods: dict[str, dict] = {}
    for m in meals:
        local = m.eaten_at.replace(tzinfo=UTC).astimezone(zone)
        day = days.get(local.date())
        if day is None:
            continue
        day["meals"] += 1
        day_items[local.date()].extend(m.items)
        for i in m.items:
            k = i.grams / 100
            kcal = i.kcal_100 * k
            day["kcal"] += kcal
            day["protein"] += i.protein_100 * k
            day["fat"] += i.fat_100 * k
            day["carbs"] += i.carbs_100 * k
            parts[_part_of_day(local.hour)] = parts.get(_part_of_day(local.hour), 0.0) + kcal
            f = foods.setdefault(i.name.strip().lower(), {"kcal": 0.0, "times": 0})
            f["kcal"] += kcal
            f["times"] += 1

    moves = {a.day: {k: v for k, v in (("active_kcal", a.active_kcal), ("steps", a.steps)) if v is not None}
             for a in activity}
    total = sum(parts.values()) or 1.0
    top = sorted(foods.items(), key=lambda kv: -kv[1]["kcal"])[:10]
    return {
        "goals_per_day": goals,
        "reference_per_day": _refs(goals),
        "days": [
            {"date": d.isoformat(), "weekday": WEEKDAYS[d.weekday()],
             **({k: round(x) for k, x in t.items()} | _extras(day_items[d]) if t["meals"] else {"logged": False}),
             **{k: round(v) for k, v in moves.get(d, {}).items()}}
            for d, t in days.items()
        ],
        "kcal_share_by_part_of_day_percent": {p: round(100 * v / total) for p, v in parts.items()},
        "top_foods_by_kcal": [{"name": n, "kcal_week": round(f["kcal"]), "times": f["times"]} for n, f in top],
        "weigh_ins_kg": [{"date": w.day.isoformat(), "kg": w.kg} for w in weights],
    }


def _refs(goals: dict) -> dict:
    r = diary.nutrient_refs(goals["kcal"])
    return {"fiber_g_target": r["fiber"], "salt_g_limit": r["salt"]}


def _extras(items) -> dict:
    """fiber_g, sugar_g, salt_g for a day; a key is left out when no item had the value."""
    return {f"{k}_g": round(v, 1) for k, v in foods.extra_totals(items).items() if v is not None}


def logged_days(digest: dict) -> int:
    return sum(1 for d in digest["days"] if d.get("logged", True))


def review(digest: dict, client: anthropic.Anthropic | None = None) -> ReviewResult:
    settings = get_settings()
    kwargs: dict = {
        "model": settings.review_model,
        "max_tokens": 4000,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": "Дневник за неделю:\n" + json.dumps(digest, ensure_ascii=False, indent=1)}],
        "output_config": {"effort": settings.review_effort, "format": {"type": "json_schema", "schema": SCHEMA}},
    }
    if not settings.review_model.startswith("claude-haiku"):
        kwargs["betas"] = ["server-side-fallback-2026-07-01"]
        kwargs["fallbacks"] = "default"

    client = client or _client()
    try:
        response = client.beta.messages.create(**kwargs)
    except anthropic.RateLimitError as exc:
        raise ReviewError("Сервис перегружен, попробуй через минуту") from exc
    except anthropic.APIStatusError as exc:
        logger.error("Claude API error %s: %s", exc.status_code, exc.message)
        raise ReviewError("Не удалось получить обзор: ошибка сервиса") from exc
    except anthropic.APIConnectionError as exc:
        raise ReviewError("Нет связи с сервисом") from exc

    if response.stop_reason == "refusal":
        raise ReviewErrorWithResponse("Модель отказалась делать обзор", response)
    if response.stop_reason == "max_tokens":
        raise ReviewErrorWithResponse("Ответ модели оборвался, попробуй ещё раз", response)
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        out = ReviewOut.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.error("Unparseable review: %r", text[:500])
        raise ReviewErrorWithResponse("Не удалось разобрать ответ модели", response) from exc
    return ReviewResult(review=out, response=response)


class NotEnoughData(ReviewError):
    pass


class LimitReached(ReviewError):
    pass


def create_for(db: Session, user: User) -> Review:
    """Today's review for the user: the stored one if it's already made, else a new one.
    Raises NotEnoughData, LimitReached or ReviewError; a billed failure is recorded."""
    today = diary.today(user)
    done = db.scalar(select(Review).where(Review.user_id == user.id, Review.day == today))
    if done is not None:
        return done

    first = today - timedelta(days=REVIEW_DAYS - 1)
    start, _ = usage.day_bounds_utc(user.timezone, first)
    _, end = usage.day_bounds_utc(user.timezone, today)
    meals = db.scalars(
        select(Meal).where(Meal.user_id == user.id, Meal.eaten_at >= start, Meal.eaten_at < end)
        .options(selectinload(Meal.items))
    ).all()
    weights = db.scalars(
        select(Weight).where(Weight.user_id == user.id, Weight.day >= first, Weight.day <= today).order_by(Weight.day)
    ).all()
    activity = db.scalars(
        select(Activity).where(Activity.user_id == user.id, Activity.day >= first, Activity.day <= today)
    ).all()
    goals = {"kcal": user.kcal_goal, "protein": user.protein_goal, "fat": user.fat_goal, "carbs": user.carbs_goal}
    digest = build_digest(meals, weights, goals, user.timezone, today, activity)
    if logged_days(digest) < MIN_LOGGED_DAYS:
        raise NotEnoughData(f"Для обзора нужны записи хотя бы за {MIN_LOGGED_DAYS} дня из последних 7")
    if problem := usage.limit_problem(db, user):
        raise LimitReached(problem)

    model = get_settings().review_model
    try:
        result = review(digest)
    except ReviewErrorWithResponse as exc:
        usage.record_call(db, user, exc.response, model=model, error=str(exc))
        raise
    usage.record_call(db, user, result.response, model=model)
    saved = Review(user_id=user.id, day=today, content=result.review.model_dump_json())
    db.add(saved)
    db.commit()
    return saved
