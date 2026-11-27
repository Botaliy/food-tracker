"""A chat with an AI coach that knows the user's diary.

Every message goes with a fresh snapshot of the user's data (goals, today, the last 7
days, workouts, weight trend, streaks), so answers are about this person, not generic advice.
Bounded by a daily message cap per user and by the same $ limits as recognition.

The coach never writes to the diary. It may answer with tool calls (TOOLS): those are
proposals the app shows as cards, and only the user's tap applies one (app.routers.api).
One model call per message: a tool call ends the turn, there is no tool loop.
A message may carry a picture (a watch app, a plate, a menu): it goes to the model once and is not kept.
"""

import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta

import anthropic
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app import diary, energy, foods, review, usage
from app.config import get_settings
from app.models import Activity, ChatMessage, Meal, Profile, User, Water, Weight, Workout
from app.recognize import RecognizeError as CoachError
from app.recognize import RecognizeErrorWithResponse as CoachErrorWithResponse
from app.recognize import _client

logger = logging.getLogger(__name__)

HISTORY_MESSAGES = 20   # sent to the model; older ones stay in the chat but out of context
MAX_MESSAGE_CHARS = 2000

SYSTEM = """Ты — коуч по питанию и здоровым привычкам внутри личного трекера калорий. Общаешься по-русски, на «ты», дружелюбно и по делу.

Перед каждым сообщением приложение даёт свежие данные пользователя: норму, профиль, что съедено сегодня, сводку за 7 дней, вес и его сглаженный тренд, серии, воду, активность и тренировки за 7 дней (workouts_last_7_days). Опирайся на них и называй цифры, когда это помогает. Дни без записей — пропуски в дневнике, а не голодание. Клетчатка, сахар и соль (fiber, sugar, salt в граммах) известны не для всех продуктов — это нижняя оценка, null значит «нет данных»; ориентиры в reference_per_day (клетчатка 14 г на 1000 ккал, соль до 5 г). Сахар — весь сахар, включая фруктовый и молочный, а не только добавленный; не ругай за него, если это фрукты и молочное. Дневные active_kcal с часов уже включают тренировки — не складывай их.

Как отвечать:
- Коротко: обычно 2–6 предложений или короткий список. Длиннее — только если просят план или разбор.
- Конкретно: предлагай обычную еду, порции в граммах, простые действия на сегодня и завтра. Учитывай, что человек уже ест.
- Если вопрос о том, что съесть, — посчитай, сколько осталось до нормы по калориям и белку, и предложи 2–3 варианта.
- Без морали и запугивания. Похвали, когда есть за что.
- Можно использовать **жирный** и списки с «- ». Без заголовков и таблиц.

Записи в дневник:
- Когда просят записать еду, воду, вес, тренировку или активность, поменять норму или явно соглашаются на твоё предложение («да, запиши») — вызови инструмент (log_meal, add_water, log_weight, set_goals, log_workout, log_activity), а не описывай запись словами.
- Ничего не сохраняется, пока пользователь не нажмёт «Записать» на карточке под твоим ответом. Поэтому никогда не пиши «записал» или «готово» — скажи коротко, что карточка ниже и её нужно подтвердить.
- log_meal: раздели еду на продукты с граммами, как она съедена (приготовленный вес); названия по-русски и по-английски в терминах базы USDA; цифры на 100 г — твоя оценка, их сверят с базой. time — «ЧЧ:ММ», только если время названо или очевидно, иначе пустая строка.
- set_goals: считай от реального расхода (expenditure: по дневнику и весу, по формуле) и активности из данных, не от общих норм; не ниже 1200 ккал; в reason — одно короткое предложение, почему так.
- Тренировка словами («пробежал 5 км за 30 минут») или скриншот из приложения часов (Huawei Health, Garmin, Strava, Apple Fitness…): возьми цифры и предложи log_workout для тренировки и log_activity для итогов дня (шаги, активные ккал). Не придумывай цифр, которых нет в тексте или на скриншоте — неизвестное это 0. Если ккал тренировки не видны, можно оценить по виду, длительности и весу, но скажи, что это оценка.
- Фото еды в чате — log_meal, как обычно.
- Фото или скриншот меню (кафе, столовая, доставка) — это вопрос «что взять», а не запись. Выбери 2–3 позиции из этого меню под остаток дня (today.left): сначала калории и белок. Называй позиции так, как они написаны в меню; для каждой — одной строкой примерные ккал и белок на порцию (это оценка, так и скажи один раз) и почему подходит. Если в меню указаны граммы или калории — бери их, а не оценивай. Если есть явная ловушка (на вид лёгкое, а на деле половина дневной нормы) — назови одну. Если остаток маленький или отрицательный, предложи самое лёгкое и скажи об этом прямо, без морали. log_meal не вызывай, пока человек не скажет, что выбрал или съел. Если текст не читается — попроси переснять ближе, не выдумывай позиции.
- Не предлагай записей, о которых не просили: если хочешь что-то предложить сам, спроси в тексте, а инструмент вызывай после согласия.
- Строки вида «[предложено: … — записано / отклонено / ждёт подтверждения]» в истории — это что стало с твоими прошлыми карточками.

Нагрузка: советы о тренировках связывай с дневником — хватает ли белка, какой дефицит, куда идёт вес.

Границы: ты не врач. Не ставь диагнозов, не назначай лекарства и добавки, не советуй голодание и диеты ниже 1200 ккал. При симптомах, расстройствах пищевого поведения, беременности или хронических болезнях мягко советуй обратиться к врачу. Вопросы не про питание, вес, движение, сон и привычки вежливо возвращай к теме."""


MAX_ACTIONS = 3  # tool calls taken from one reply; the rest are ignored
NO_TEXT_REPLY = "Могу записать — проверь и подтверди."

_ITEM = {
    "type": "object",
    "properties": {
        "name_ru": {"type": "string", "description": "Название продукта по-русски, коротко."},
        "name_en": {"type": "string", "description": "Generic English name in USDA SR Legacy wording, with the cooking state: "
                                                     "'egg whole cooked scrambled', 'milk reduced fat 2%'. No brands."},
        "grams": {"type": "number", "description": "Weight as eaten, grams."},
        "kcal_100": {"type": "number", "description": "Your estimate of kcal per 100 g."},
        "protein_100": {"type": "number", "description": "Protein, g per 100 g."},
        "fat_100": {"type": "number", "description": "Fat, g per 100 g."},
        "carbs_100": {"type": "number", "description": "Carbohydrates, g per 100 g."},
        "fiber_100": {"type": "number", "description": "Dietary fibre, g per 100 g."},
        "sugar_100": {"type": "number", "description": "Total sugars, g per 100 g."},
        "salt_100": {"type": "number", "description": "Salt (sodium x 2.5), g per 100 g."},
    },
    "required": ["name_ru", "name_en", "grams", "kcal_100", "protein_100", "fat_100", "carbs_100",
                 "fiber_100", "sugar_100", "salt_100"],
    "additionalProperties": False,
}


def _tool(name: str, description: str, properties: dict) -> dict:
    return {"name": name, "description": description, "strict": True, "input_schema": {
        "type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}}


TOOLS = [
    _tool("log_meal", "Предложить запись приёма пищи за сегодня. Сохранится, только когда пользователь подтвердит карточку.", {
        "title": {"type": "string", "description": "Короткое название: «Завтрак», «Омлет с кофе»."},
        "time": {"type": "string", "description": "Местное время сегодня, «ЧЧ:ММ», или пустая строка, если время не названо."},
        "items": {"type": "array", "items": _ITEM},
    }),
    _tool("set_goals", "Предложить новую дневную норму. Применится после подтверждения.", {
        "kcal": {"type": "integer", "description": "Ккал в день, не ниже 1200."},
        "protein": {"type": "integer", "description": "Белки, г в день."},
        "fat": {"type": "integer", "description": "Жиры, г в день."},
        "carbs": {"type": "integer", "description": "Углеводы, г в день."},
        "reason": {"type": "string", "description": "Одно короткое предложение по-русски: почему такая норма."},
    }),
    _tool("add_water", "Предложить добавить выпитую сегодня воду.", {
        "ml": {"type": "integer", "description": "Сколько миллилитров добавить, больше нуля."},
    }),
    _tool("log_weight", "Предложить записать вес за сегодня.", {
        "kg": {"type": "number", "description": "Вес в килограммах."},
    }),
    _tool("log_workout", "Предложить записать тренировку за сегодня. Неизвестное — 0 или пустая строка.", {
        "kind": {"type": "string", "description": "Вид по-русски, коротко: «Бег», «Силовая», «Велосипед»."},
        "minutes": {"type": "integer", "description": "Длительность в минутах, больше нуля."},
        "kcal": {"type": "number", "description": "Ккал за тренировку, 0 — неизвестно."},
        "distance_km": {"type": "number", "description": "Дистанция в км, 0 — нет или неизвестно."},
        "avg_hr": {"type": "integer", "description": "Средний пульс, уд/мин, 0 — неизвестно."},
        "time": {"type": "string", "description": "Начало, местное время «ЧЧ:ММ», или пустая строка."},
    }),
    _tool("log_activity", "Предложить записать итоги дня с часов за сегодня: шаги и активные ккал. 0 — неизвестно.", {
        "steps": {"type": "integer", "description": "Шаги за день, 0 — неизвестно."},
        "active_kcal": {"type": "number", "description": "Активные ккал за день, 0 — неизвестно."},
    }),
]


@dataclass
class ToolCall:
    name: str
    input: dict


@dataclass
class CoachReply:
    text: str  # may be empty when the reply is only tool calls
    response: object = field(repr=False)
    calls: list[ToolCall] = field(default_factory=list)


_STATUS_WORD = {"done": "записано", "declined": "отклонено", "pending": "ждёт подтверждения"}


def action_line(action: dict) -> str:
    """How a proposal and its fate read in the history sent to the model (never shown in the bubble)."""
    p, kind = action["payload"], action["type"]
    if kind == "log_meal":
        kcal = round(sum(i["kcal_100"] * i["grams"] / 100 for i in p["items"]))
        what = f"записать «{p['title'] or 'приём пищи'}», {kcal} ккал"
    elif kind == "set_goals":
        what = f"изменить норму на {p['kcal']} ккал (Б {p['protein']} · Ж {p['fat']} · У {p['carbs']})"
    elif kind == "add_water":
        what = f"добавить воду {p['ml']} мл"
    elif kind == "log_weight":
        what = f"записать вес {_num(p['kg'])} кг"
    elif kind == "log_workout":
        parts = [p["kind"], f"{p['minutes']} мин"]
        parts += [f"{_num(p['distance_km'])} км"] if p.get("distance_km") else []
        parts += [f"{round(p['kcal'])} ккал"] if p.get("kcal") else []
        parts += [f"пульс {p['avg_hr']}"] if p.get("avg_hr") else []
        what = "записать тренировку: " + ", ".join(parts)
    elif kind == "log_activity":
        parts = [f"{p['steps']} шагов"] if p.get("steps") else []
        parts += [f"{round(p['active_kcal'])} активных ккал"] if p.get("active_kcal") else []
        what = "записать активность за день: " + ", ".join(parts)
    else:
        what = kind
    return f"[предложено: {what} — {_STATUS_WORD.get(action['status'], action['status'])}]"


def _num(x) -> str:
    return f"{x:g}".replace(".", ",")  # 5.0 -> "5", 72.4 -> "72,4"


IMAGE_NOTE = "[к сообщению был приложен скриншот — в истории его нет]"


def model_content(m: ChatMessage) -> str:
    if m.role == "user":
        return f"{m.content}\n{IMAGE_NOTE}" if m.has_image else m.content
    if not m.actions:
        return m.content
    try:
        lines = [action_line(a) for a in json.loads(m.actions)]
    except (ValueError, KeyError, TypeError):
        logger.warning("Unreadable actions on chat message %s", m.id)
        return m.content
    return "\n".join([m.content, *lines])


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
    workouts = db.scalars(select(Workout).where(Workout.user_id == user.id, Workout.day >= first)
                          .order_by(Workout.day, Workout.started, Workout.id)).all()
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
        "workouts_last_7_days": [{"date": w.day.isoformat(), "time": w.started, "kind": w.kind, "minutes": w.minutes,
                                  "kcal": w.kcal, "distance_km": w.distance_km, "avg_hr": w.avg_hr} for w in workouts],
    }


def history(db: Session, user: User, limit: int) -> list[ChatMessage]:
    rows = db.scalars(select(ChatMessage).where(ChatMessage.user_id == user.id)
                      .order_by(ChatMessage.id.desc()).limit(limit)).all()
    return list(reversed(rows))


def ask(db: Session, user: User, text: str, client: anthropic.Anthropic | None = None,
        extra: dict | None = None, image: bytes | None = None) -> CoachReply:
    """`extra` joins the snapshot: data the API layer computes (expenditure). `image`: a JPEG
    for this message only; later turns see just a note that there was one."""
    settings = get_settings()
    past = history(db, user, HISTORY_MESSAGES)
    messages = [{"role": m.role, "content": model_content(m)} for m in past]
    # The API wants the first message from the user.
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    if image is None:
        messages.append({"role": "user", "content": text})
    else:
        messages.append({"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(image).decode("ascii")}},
            {"type": "text", "text": text},
        ]})
    data = json.dumps({**snapshot(db, user), **(extra or {})}, ensure_ascii=False, default=str)
    kwargs: dict = {
        "model": settings.coach_model,
        "max_tokens": 4000,
        "system": [
            {"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": f"Данные пользователя на сейчас (JSON):\n{data}"},
        ],
        "messages": messages,
        "tools": TOOLS,
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
    # A cut-off reply may hold a half-written call: only a finished turn proposes anything.
    calls = [] if response.stop_reason == "max_tokens" else [
        ToolCall(b.name, b.input if isinstance(b.input, dict) else {})
        for b in response.content if b.type == "tool_use"][:MAX_ACTIONS]
    if not reply and not calls:
        raise CoachErrorWithResponse("Коуч ничего не ответил, попробуй ещё раз", response)
    if reply and response.stop_reason == "max_tokens":
        reply += "…"
    return CoachReply(text=reply, response=response, calls=calls)
