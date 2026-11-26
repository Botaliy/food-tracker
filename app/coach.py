"""A chat with an AI coach that knows the user's diary.

Every message goes with a fresh snapshot of the user's data (goals, today, the last 7
days, weight trend, streaks), so answers are about this person, not generic advice.
Bounded by a daily message cap per user and by the same $ limits as recognition.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta

import anthropic
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app import diary, energy, foods, review, usage
from app.config import get_settings
from app.models import Activity, ChatMessage, Meal, Profile, User, Water, Weight
from app.recognize import RecognizeError as CoachError
from app.recognize import RecognizeErrorWithResponse as CoachErrorWithResponse
from app.recognize import _client

logger = logging.getLogger(__name__)

HISTORY_MESSAGES = 20   # sent to the model; older ones stay in the chat but out of context
MAX_MESSAGE_CHARS = 2000

SYSTEM = """Ты — коуч по питанию и здоровым привычкам внутри личного трекера калорий. Общаешься по-русски, на «ты», дружелюбно и по делу.

Перед каждым сообщением приложение даёт свежие данные пользователя: норму, профиль, что съедено сегодня, сводку за 7 дней, вес и его сглаженный тренд, серии, воду и активность. Опирайся на них и называй цифры, когда это помогает. Дни без записей — пропуски в дневнике, а не голодание. Клетчатка, сахар и соль (fiber, sugar, salt в граммах) известны не для всех продуктов — это нижняя оценка, null значит «нет данных»; ориентиры в reference_per_day (клетчатка 14 г на 1000 ккал, соль до 5 г). Сахар — весь сахар, включая фруктовый и молочный, а не только добавленный; не ругай за него, если это фрукты и молочное.

Как отвечать:
- Коротко: обычно 2–6 предложений или короткий список. Длиннее — только если просят план или разбор.
- Конкретно: предлагай обычную еду, порции в граммах, простые действия на сегодня и завтра. Учитывай, что человек уже ест.
- Если вопрос о том, что съесть, — посчитай, сколько осталось до нормы по калориям и белку, и предложи 2–3 варианта.
- Без морали и запугивания. Похвали, когда есть за что.
- Можно использовать **жирный** и списки с «- ». Без заголовков и таблиц.

Границы: ты не врач. Не ставь диагнозов, не назначай лекарства и добавки, не советуй голодание и диеты ниже 1200 ккал. При симптомах, расстройствах пищевого поведения, беременности или хронических болезнях мягко советуй обратиться к врачу. Вопросы не про питание, вес, движение, сон и привычки вежливо возвращай к теме."""


@dataclass
class CoachReply:
    text: str
    response: object = field(repr=False)


def used_today(db: Session, user: User) -> int:
    start, end = usage.day_bounds_utc(user.timezone)
    return db.scalar(select(func.count()).select_from(ChatMessage).where(
        ChatMessage.user_id == user.id, ChatMessage.role == "user",
        ChatMessage.created_at >= start, ChatMessage.created_at < end)) or 0


def snapshot(db: Session, user: User) -> dict:
    """What the coach knows about the user right now."""
    today = diary.today(user)
    profile = db.get(Profile, user.id)
    first = today - timedelta(days=review.REVIEW_DAYS - 1)
    start, _ = usage.day_bounds_utc(user.timezone, first)
    meals = db.scalars(select(Meal).where(Meal.user_id == user.id, Meal.eaten_at >= start)
                       .options(selectinload(Meal.items))).all()
    weights = db.scalars(select(Weight).where(Weight.user_id == user.id, Weight.day >= today - timedelta(days=90))
                         .order_by(Weight.day)).all()
    activity = db.scalars(select(Activity).where(Activity.user_id == user.id, Activity.day >= first)).all()
    goals = {"kcal": user.kcal_goal, "protein": user.protein_goal, "fat": user.fat_goal, "carbs": user.carbs_goal}
    week = review.build_digest(meals, [w for w in weights if w.day >= first], goals, user.timezone, today, activity)

    today_meals = [m for m in meals if diary.local_day(m.eaten_at, user.timezone) == today]
    eaten = {"kcal": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0}
    for m in today_meals:
        for i in m.items:
            k = i.grams / 100
            eaten["kcal"] += i.kcal_100 * k
            eaten["protein"] += i.protein_100 * k
            eaten["fat"] += i.fat_100 * k
            eaten["carbs"] += i.carbs_100 * k
    extra = foods.extra_totals([i for m in today_meals for i in m.items])
    water = db.scalar(select(Water).where(Water.user_id == user.id, Water.day == today))
    return {
        "now_local": diary.today(user).isoformat(),
        "profile": None if profile is None or not profile.sex else {
            "sex": profile.sex, "age": today.year - profile.birth_year if profile.birth_year else None,
            "height_cm": profile.height_cm, "pace_kg_per_week": profile.rate_kg_week,
            "goal_weight_kg": profile.goal_weight_kg,
        },
        "goals_per_day": goals,
        "reference_per_day": week["reference_per_day"],
        "today": {
            "eaten": {k: round(v) for k, v in eaten.items()},
            # Only for items that have the values; None = nothing known today.
            "eaten_extra_g": {k: None if v is None else round(v, 1) for k, v in extra.items()},
            "left": {k: round(goals[k] - eaten[k]) for k in goals},
            "meals": [{"time": m.eaten_at.isoformat(), "title": m.title,
                       "items": [f"{i.name} {round(i.grams)} г" for i in m.items]} for m in today_meals],
            "water_ml": water.ml if water else 0,
        },
        "last_7_days": {k: week[k] for k in ("days", "kcal_share_by_part_of_day_percent", "top_foods_by_kcal")},
        "weight": energy.weight_outlook([(w.day, w.kg) for w in weights],
                                        profile.goal_weight_kg if profile else None, today),
        "streaks": diary.streaks(db, user, today),
    }


def history(db: Session, user: User, limit: int) -> list[ChatMessage]:
    rows = db.scalars(select(ChatMessage).where(ChatMessage.user_id == user.id)
                      .order_by(ChatMessage.id.desc()).limit(limit)).all()
    return list(reversed(rows))


def ask(db: Session, user: User, text: str, client: anthropic.Anthropic | None = None) -> CoachReply:
    settings = get_settings()
    past = history(db, user, HISTORY_MESSAGES)
    messages = [{"role": m.role, "content": m.content} for m in past]
    # The API wants the first message from the user.
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    messages.append({"role": "user", "content": text})
    data = json.dumps(snapshot(db, user), ensure_ascii=False, default=str)
    kwargs: dict = {
        "model": settings.coach_model,
        "max_tokens": 4000,
        "system": [
            {"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": f"Данные пользователя на сейчас (JSON):\n{data}"},
        ],
        "messages": messages,
        "output_config": {"effort": settings.coach_effort},
    }
    if not settings.coach_model.startswith("claude-haiku"):
        kwargs["betas"] = ["server-side-fallback-2026-07-01"]
        kwargs["fallbacks"] = "default"

    client = client or _client()
    try:
        response = client.beta.messages.create(**kwargs)
    except anthropic.RateLimitError as exc:
        raise CoachError("Коуч перегружен, попробуй через минуту") from exc
    except anthropic.APIStatusError as exc:
        logger.error("Claude API error %s: %s", exc.status_code, exc.message)
        raise CoachError("Коуч не ответил: ошибка сервиса") from exc
    except anthropic.APIConnectionError as exc:
        raise CoachError("Нет связи с сервисом") from exc

    if response.stop_reason == "refusal":
        raise CoachErrorWithResponse("На это коуч ответить не может — попробуй спросить иначе", response)
    reply = "".join(b.text for b in response.content if b.type == "text").strip()
    if not reply:
        raise CoachErrorWithResponse("Коуч ничего не ответил, попробуй ещё раз", response)
    if response.stop_reason == "max_tokens":
        reply += "…"
    return CoachReply(text=reply, response=response)
