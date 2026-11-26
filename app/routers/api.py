import hashlib
import logging
import json
import secrets
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app import auth, barcodes, corrections, diary, energy, foods, push, review as review_mod, usage
from app.config import get_settings
from app.database import get_db
from app.drafts import build_draft
from app.images import release_memory, shrink
from app.models import Activity, Food, Invite, Meal, MealItem, Profile, PushSubscription, Review, User, Weight, utcnow
from app.recognize import RecognizeError, RecognizeErrorWithResponse, recognize

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

MAX_UPLOAD_BYTES = 15 * 1024 * 1024


def _valid_tz(name: str | None) -> str | None:
    if not name:
        return None
    try:
        ZoneInfo(name)
        return name
    except (ZoneInfoNotFoundError, ValueError):
        return None


# ---------- auth ----------

class LoginIn(BaseModel):
    username: str
    password: str
    timezone: str | None = None


class RegisterIn(LoginIn):
    invite: str


def _me(db: Session, user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "is_admin": user.is_admin,
        "timezone": user.timezone,
        "goals": {
            "kcal": user.kcal_goal,
            "protein": user.protein_goal,
            "fat": user.fat_goal,
            "carbs": user.carbs_goal,
        },
        "spent_today": round(usage.spent_today(db, user), 4),
        "daily_limit": usage.daily_limit(user),
        "onboarded": bool((p := db.get(Profile, user.id)) and p.onboarded),
    }


@router.post("/auth/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == body.username.strip().lower()))
    if user is None or not auth.check_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    if tz := _valid_tz(body.timezone):
        user.timezone = tz
        db.commit()
    auth.login(request, user)
    return _me(db, user)


@router.post("/auth/register")
def register(body: RegisterIn, request: Request, db: Session = Depends(get_db)):
    invite = db.get(Invite, body.invite.strip())
    if invite is None or invite.used_by is not None:
        raise HTTPException(status_code=400, detail="Приглашение не найдено или уже использовано")
    username = body.username.strip().lower()
    if not (2 <= len(username) <= 32) or not username.replace("_", "").replace(".", "").isalnum():
        raise HTTPException(status_code=400, detail="Логин: 2–32 символа, буквы, цифры, _ и .")
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Пароль — не короче 8 символов")
    if db.scalar(select(User).where(User.username == username)):
        raise HTTPException(status_code=400, detail="Такой логин уже занят")
    user = User(
        username=username,
        password_hash=auth.hash_password(body.password),
        timezone=_valid_tz(body.timezone) or get_settings().timezone,
    )
    db.add(user)
    db.flush()
    invite.used_by, invite.used_at = user.id, utcnow()
    db.commit()
    auth.login(request, user)
    return _me(db, user)


@router.post("/auth/logout")
def logout(request: Request):
    auth.logout(request)
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    return _me(db, user)


class GoalsIn(BaseModel):
    kcal: int = Field(ge=500, le=10000)
    protein: int = Field(ge=0, le=1000)
    fat: int = Field(ge=0, le=1000)
    carbs: int = Field(ge=0, le=2000)
    timezone: str | None = None


@router.put("/me")
def update_me(body: GoalsIn, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    user.kcal_goal, user.protein_goal, user.fat_goal, user.carbs_goal = body.kcal, body.protein, body.fat, body.carbs
    if tz := _valid_tz(body.timezone):
        user.timezone = tz
    db.commit()
    return _me(db, user)


# ---------- recognition ----------

@router.post("/recognize")
def recognize_meal(
    photo: UploadFile | None = File(None),
    note: str = Form(""),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
):
    if problem := usage.limit_problem(db, user):
        raise HTTPException(status_code=429, detail=problem)

    image = None
    if photo is not None and photo.filename:
        raw = photo.file.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Фото слишком большое")
        image = shrink(raw)
        del raw
        release_memory()
        if image is None:
            raise HTTPException(status_code=400, detail="Не получилось открыть фото")

    model = get_settings().recognize_model
    hints = corrections.hints(db, user)
    try:
        result = recognize(image, note[:500], hints)
    except RecognizeErrorWithResponse as exc:
        usage.record_call(db, user, exc.response, model=model, error=str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except RecognizeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    cost = usage.record_call(db, user, result.response, model=model)
    draft = build_draft(db, result.recognition, user.id)
    draft["personal_hints"] = len(hints)
    draft["cost_usd"] = round(cost, 4)
    draft["spent_today"] = round(usage.spent_today(db, user), 4)
    return draft


# ---------- foods ----------

@router.get("/foods/search")
def search_foods(q: str, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    found = foods.search(db, q, user.id, limit=15)
    last = _last_grams(db, user, [f.id for f in found])
    return [{**foods.food_dict(f), "last_grams": last.get(f.id)} for f in found]


def _last_grams(db: Session, user: User, food_ids: list[int]) -> dict[int, float]:
    """How much of each food the user took last time: a better start than 100 g."""
    if not food_ids:
        return {}
    rows = db.execute(
        select(MealItem.food_id, MealItem.grams)
        .join(Meal, Meal.id == MealItem.meal_id)
        .where(Meal.user_id == user.id, MealItem.food_id.in_(food_ids))
        .order_by(MealItem.id.desc())
    ).all()
    out: dict[int, float] = {}
    for food_id, grams in rows:
        out.setdefault(food_id, grams)
    return out


class FoodIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kcal_100: float = Field(ge=0, le=900)
    protein_100: float = Field(ge=0, le=100)
    fat_100: float = Field(ge=0, le=100)
    carbs_100: float = Field(ge=0, le=100)
    barcode: str | None = Field(None, max_length=32)


@router.post("/foods")
def add_food(body: FoodIn, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    food = Food(
        source="custom", name=body.name.strip(), name_ru=body.name.strip(), owner_id=user.id,
        barcode=barcodes.normalize(body.barcode) if body.barcode else None,
        kcal=body.kcal_100, protein=body.protein_100, fat=body.fat_100, carbs=body.carbs_100,
    )
    db.add(food)
    db.commit()
    return foods.food_dict(food)


@router.get("/foods/barcode/{code}")
def food_by_barcode(code: str, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    norm = barcodes.normalize(code)
    if norm is None:
        raise HTTPException(status_code=400, detail="Это не похоже на штрихкод")
    try:
        food = barcodes.lookup(db, norm, user)
    except barcodes.LookupError_ as exc:
        logger.warning("Barcode lookup failed: %s", exc)
        raise HTTPException(status_code=503, detail="База штрихкодов не отвечает — введи значения с этикетки") from exc
    if food is None:
        raise HTTPException(status_code=404, detail="Такого продукта ещё нет в базе")
    return {**foods.food_dict(food), "barcode": norm}


# ---------- meals ----------

class ItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    grams: float = Field(gt=0, le=5000)
    food_id: int | None = None
    source: str = "estimate"
    kcal_100: float = Field(ge=0, le=900)
    protein_100: float = Field(ge=0, le=100)
    fat_100: float = Field(ge=0, le=100)
    carbs_100: float = Field(ge=0, le=100)
    ai_grams: float | None = Field(None, gt=0, le=5000)


class MealIn(BaseModel):
    title: str = Field(default="", max_length=200)
    eaten_at: datetime | None = None
    items: list[ItemIn] = Field(min_length=1, max_length=30)


def _to_utc(dt: datetime | None, tz: str) -> datetime:
    if dt is None:
        return utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return dt.astimezone(UTC).replace(tzinfo=None)


def _item(db: Session, body: ItemIn, user: User) -> MealItem:
    item = MealItem(
        name=body.name.strip(), grams=body.grams, source=body.source if body.source in {"estimate", "manual"} else "estimate",
        kcal_100=body.kcal_100, protein_100=body.protein_100, fat_100=body.fat_100, carbs_100=body.carbs_100,
        ai_grams=body.ai_grams,
    )
    # A database food's numbers are taken from the database, not from the client.
    food = db.get(Food, body.food_id) if body.food_id is not None else None
    if food is not None:
        item.food_id, item.source = food.id, food.source
        item.kcal_100, item.protein_100, item.fat_100, item.carbs_100 = food.kcal, food.protein, food.fat, food.carbs
    return item


def _totals(items) -> dict:
    t = {"kcal": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0}
    for i in items:
        k = i.grams / 100
        t["kcal"] += i.kcal_100 * k
        t["protein"] += i.protein_100 * k
        t["fat"] += i.fat_100 * k
        t["carbs"] += i.carbs_100 * k
    return {key: round(v, 1) for key, v in t.items()}


def _meal_dict(meal: Meal, tz: str) -> dict:
    eaten = meal.eaten_at.replace(tzinfo=UTC).astimezone(ZoneInfo(tz))
    return {
        "id": meal.id,
        "title": meal.title,
        "eaten_at": eaten.isoformat(),
        "items": [
            {
                "id": i.id, "name": i.name, "grams": i.grams, "food_id": i.food_id, "source": i.source,
                "kcal_100": i.kcal_100, "protein_100": i.protein_100, "fat_100": i.fat_100, "carbs_100": i.carbs_100,
                "ai_grams": i.ai_grams,
            }
            for i in meal.items
        ],
        "totals": _totals(meal.items),
    }


def _own_meal(db: Session, meal_id: int, user: User) -> Meal:
    meal = db.get(Meal, meal_id)
    if meal is None or meal.user_id != user.id:
        raise HTTPException(status_code=404, detail="Приём пищи не найден")
    return meal


@router.post("/meals")
def add_meal(body: MealIn, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    meal = Meal(user_id=user.id, title=body.title.strip(), eaten_at=_to_utc(body.eaten_at, user.timezone))
    meal.items = [_item(db, i, user) for i in body.items]
    db.add(meal)
    db.commit()
    return _meal_dict(meal, user.timezone)


@router.put("/meals/{meal_id}")
def update_meal(meal_id: int, body: MealIn, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    meal = _own_meal(db, meal_id, user)
    meal.title = body.title.strip()
    if body.eaten_at is not None:
        meal.eaten_at = _to_utc(body.eaten_at, user.timezone)
    meal.items = [_item(db, i, user) for i in body.items]
    db.commit()
    return _meal_dict(meal, user.timezone)


@router.delete("/meals/{meal_id}")
def delete_meal(meal_id: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    db.delete(_own_meal(db, meal_id, user))
    db.commit()
    return {"ok": True}


RECENT_DAYS = 30


@router.get("/meals/recent")
def recent_meals(limit: int = Query(8, ge=1, le=20), user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    """Meals worth repeating: the same title and set of products counts as one, most frequent first."""
    since = utcnow() - timedelta(days=RECENT_DAYS)
    meals = db.scalars(
        select(Meal)
        .where(Meal.user_id == user.id, Meal.eaten_at >= since)
        .options(selectinload(Meal.items))
        .order_by(Meal.eaten_at.desc())
    ).all()
    groups: dict[tuple, dict] = {}
    for m in meals:  # newest first, so the first meal seen is the template
        key = (m.title.strip().lower(), tuple(sorted(i.name.strip().lower() for i in m.items)))
        if key in groups:
            groups[key]["count"] += 1
        else:
            groups[key] = {"count": 1, "meal": m}
    ranked = sorted(groups.values(), key=lambda g: (-g["count"], -g["meal"].eaten_at.timestamp()))
    return [{**_meal_dict(g["meal"], user.timezone), "count": g["count"]} for g in ranked[:limit]]


@router.get("/day")
def day(date_: date | None = Query(None, alias="date"), user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    start, end = usage.day_bounds_utc(user.timezone, date_)
    meals = db.scalars(
        select(Meal)
        .where(Meal.user_id == user.id, Meal.eaten_at >= start, Meal.eaten_at < end)
        .options(selectinload(Meal.items))
        .order_by(Meal.eaten_at)
    ).all()
    out = [_meal_dict(m, user.timezone) for m in meals]
    totals = _totals([i for m in meals for i in m.items])
    the_day = date_ or datetime.now(ZoneInfo(user.timezone)).date()
    act = db.scalar(select(Activity).where(Activity.user_id == user.id, Activity.day == the_day))
    return {
        "date": the_day.isoformat(),
        "meals": out,
        "totals": totals,
        "goals": _me(db, user)["goals"],
        "activity": _activity_dict(act),
        "streaks": diary.streaks(db, user, _today(user)),
        "weight": _weight_outlook(db, user),
    }


WEIGHT_OUTLOOK_DAYS = 90


def _weight_outlook(db: Session, user: User) -> dict | None:
    today = _today(user)
    weights = _weights(db, user, today - timedelta(days=WEIGHT_OUTLOOK_DAYS), today)
    profile = db.get(Profile, user.id)
    out = energy.weight_outlook([(w.day, w.kg) for w in weights], profile.goal_weight_kg if profile else None, today)
    if out is not None:
        out["today"] = weights[-1].kg if weights[-1].day == today else None
    return out


def _activity_dict(a: Activity | None) -> dict | None:
    return None if a is None else {"active_kcal": a.active_kcal, "steps": a.steps}


# ---------- reports ----------

_today = diary.today
_local_day = diary.local_day


def _daily_rows(db: Session, user: User, first: date, last: date) -> list[dict]:
    """Totals for every local day from first to last, empty days included."""
    start, _ = usage.day_bounds_utc(user.timezone, first)
    _, end = usage.day_bounds_utc(user.timezone, last)
    meals = db.scalars(
        select(Meal)
        .where(Meal.user_id == user.id, Meal.eaten_at >= start, Meal.eaten_at < end)
        .options(selectinload(Meal.items))
    ).all()
    by_day: dict[date, list[Meal]] = {}
    for m in meals:
        by_day.setdefault(_local_day(m.eaten_at, user.timezone), []).append(m)
    acts = {a.day: a for a in db.scalars(
        select(Activity).where(Activity.user_id == user.id, Activity.day >= first, Activity.day <= last))}
    rows = []
    for k in range((last - first).days + 1):
        d = first + timedelta(days=k)
        day_meals = by_day.get(d, [])
        a = acts.get(d)
        rows.append({
            "date": d.isoformat(), "meals": len(day_meals), **_totals([i for m in day_meals for i in m.items]),
            "active_kcal": a.active_kcal if a else None, "steps": a.steps if a else None,
        })
    return rows


def _weights(db: Session, user: User, first: date, last: date) -> list[Weight]:
    return list(db.scalars(
        select(Weight).where(Weight.user_id == user.id, Weight.day >= first, Weight.day <= last).order_by(Weight.day)
    ))


def _avg(values) -> float | None:
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values)) if values else None


def _energy(db: Session, user: User, today: date, rows: list[dict]) -> dict:
    """The formula's estimate, the diary's estimate, and how much the watch says you move."""
    window_first = today - timedelta(days=energy.ADAPTIVE_WINDOW_DAYS - 1)
    window = _daily_rows(db, user, window_first, today)
    weights = _weights(db, user, window_first, today)
    # Today is still being eaten: leave it out of the average intake.
    intake = [energy.DayIntake(date.fromisoformat(r["date"]), r["kcal"], r["meals"] > 0)
              for r in window if r["date"] != today.isoformat()]
    adaptive = energy.adaptive(intake, [(w.day, w.kg) for w in weights])

    profile = db.get(Profile, user.id)
    last_weight = db.scalar(select(Weight).where(Weight.user_id == user.id).order_by(Weight.day.desc()).limit(1))
    sex = profile.sex if profile and profile.sex else "f"
    rate = profile.rate_kg_week if profile else 0.0
    if adaptive["ready"] and last_weight:
        adaptive["suggested"] = energy.goals_for(adaptive["tdee"], sex, last_weight.kg, rate)
    with_activity = [r for r in rows if r["active_kcal"] is not None or r["steps"] is not None]
    return {
        "adaptive": adaptive,
        "formula": _formula(profile, last_weight, today),
        "activity": {
            "days": len(with_activity),
            "active_kcal": _avg(r["active_kcal"] for r in with_activity),
            "steps": _avg(r["steps"] for r in with_activity),
        } if with_activity else None,
    }


def _formula(profile: Profile | None, last_weight: Weight | None, today: date) -> dict | None:
    if not (profile and profile.sex and profile.birth_year and profile.height_cm and last_weight):
        return None
    f = energy.formula(profile.sex, last_weight.kg, profile.height_cm, today.year - profile.birth_year, profile.activity)
    return {**f, "suggested": energy.goals_for(f["tdee"], profile.sex, last_weight.kg, profile.rate_kg_week)}


@router.get("/stats")
def stats(days: int = Query(7, ge=1, le=366), user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    today = _today(user)
    first = today - timedelta(days=days - 1)
    rows = _daily_rows(db, user, first, today)
    logged = [r for r in rows if r["meals"]]
    avg = {key: round(sum(r[key] for r in logged) / len(logged), 1) if logged else 0.0
           for key in ("kcal", "protein", "fat", "carbs")}
    weights = _weights(db, user, first, today)
    last_weight = db.scalar(select(Weight).where(Weight.user_id == user.id).order_by(Weight.day.desc()).limit(1))
    return {
        "from": first.isoformat(),
        "to": today.isoformat(),
        "goals": _me(db, user)["goals"],
        "days": rows,
        "avg": avg,
        "logged_days": len(logged),
        "in_goal_days": sum(1 for r in logged if diary.in_goal(r["kcal"], user.kcal_goal)),
        "streak": diary.streak(db, user, today)[0],
        "weights": [{"date": w.day.isoformat(), "kg": w.kg, "trend": round(t, 2)}
                    for w, (_, t) in zip(weights, energy.weight_trend([(w.day, w.kg) for w in weights]))],
        "last_weight": {"date": last_weight.day.isoformat(), "kg": last_weight.kg} if last_weight else None,
        "energy": _energy(db, user, today, rows),
    }


class WeightIn(BaseModel):
    kg: float = Field(ge=20, le=400)
    day: date | None = Field(None, alias="date")


def _weight_day(user: User, d: date | None) -> date:
    today = _today(user)
    d = d or today
    if d > today:
        raise HTTPException(status_code=400, detail="Нельзя записать вес на будущую дату")
    return d


@router.put("/weight")
def put_weight(body: WeightIn, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    d = _weight_day(user, body.day)
    w = db.scalar(select(Weight).where(Weight.user_id == user.id, Weight.day == d))
    if w is None:
        w = Weight(user_id=user.id, day=d, kg=body.kg)
        db.add(w)
    else:
        w.kg = body.kg
    db.commit()
    return {"date": d.isoformat(), "kg": w.kg}


@router.delete("/weight")
def delete_weight(date_: date | None = Query(None, alias="date"), user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    d = _weight_day(user, date_)
    w = db.scalar(select(Weight).where(Weight.user_id == user.id, Weight.day == d))
    if w is not None:
        db.delete(w)
        db.commit()
    return {"ok": True}


# ---------- weekly review ----------

def _review_dict(r: Review | None) -> dict | None:
    return None if r is None else {"date": r.day.isoformat(), **json.loads(r.content)}


@router.get("/review")
def get_review(user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    latest = db.scalar(select(Review).where(Review.user_id == user.id).order_by(Review.id.desc()).limit(1))
    return {"review": _review_dict(latest), "fresh": latest is not None and latest.day == _today(user)}


@router.post("/review")
def make_review(user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    try:
        saved = review_mod.create_for(db, user)
    except review_mod.NotEnoughData as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except review_mod.LimitReached as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except review_mod.ReviewError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"review": _review_dict(saved), "fresh": True}


# ---------- profile & goal calculator ----------

def _profile(db: Session, user: User) -> Profile:
    profile = db.get(Profile, user.id)
    if profile is None:
        profile = Profile(user_id=user.id, activity=1.375, rate_kg_week=0.0)
        db.add(profile)
        db.flush()
    return profile


def _profile_dict(db: Session, user: User, profile: Profile) -> dict:
    today = _today(user)
    last_weight = db.scalar(select(Weight).where(Weight.user_id == user.id).order_by(Weight.day.desc()).limit(1))
    last_activity = db.scalar(select(Activity.day).where(Activity.user_id == user.id).order_by(Activity.day.desc()).limit(1))
    return {
        "sex": profile.sex,
        "birth_year": profile.birth_year,
        "height_cm": profile.height_cm,
        "activity": profile.activity,
        "rate_kg_week": profile.rate_kg_week,
        "goal_weight_kg": profile.goal_weight_kg,
        "weight_kg": last_weight.kg if last_weight else None,
        "activity_levels": [{"value": k, "label": v} for k, v in energy.ACTIVITY_LEVELS.items()],
        "rates": [{"value": k, "label": v} for k, v in energy.RATES.items()],
        "formula": _formula(profile, last_weight, today),
        "health": {"connected": profile.health_token_hash is not None,
                   "last_day": last_activity.isoformat() if last_activity else None},
        "notify": {
            "evening": profile.remind_evening, "hour": profile.remind_hour,
            "weekly": profile.weekly_review_push, "share_streak": profile.share_streak,
            "devices": db.scalar(select(func.count()).select_from(PushSubscription).where(PushSubscription.user_id == user.id)),
        },
    }


class ProfileIn(BaseModel):
    sex: str = Field(pattern="^[mf]$")
    birth_year: int = Field(ge=1920, le=2015)
    height_cm: float = Field(ge=120, le=230)
    activity: float
    rate_kg_week: float
    weight_kg: float | None = Field(None, ge=20, le=400)  # recorded as today's weigh-in when given
    goal_weight_kg: float | None = Field(None, ge=30, le=300)


@router.get("/profile")
def get_profile(user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    profile = _profile(db, user)
    db.commit()
    return _profile_dict(db, user, profile)


@router.put("/profile")
def put_profile(body: ProfileIn, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    if body.activity not in energy.ACTIVITY_LEVELS or body.rate_kg_week not in energy.RATES:
        raise HTTPException(status_code=400, detail="Выбери активность и цель из списка")
    profile = _profile(db, user)
    profile.sex, profile.birth_year, profile.height_cm = body.sex, body.birth_year, body.height_cm
    profile.activity, profile.rate_kg_week = body.activity, body.rate_kg_week
    profile.goal_weight_kg = body.goal_weight_kg
    if body.weight_kg is not None:
        _upsert_weight(db, user, _today(user), body.weight_kg)
    db.commit()
    return _profile_dict(db, user, profile)


def _upsert_weight(db: Session, user: User, d: date, kg: float) -> None:
    w = db.scalar(select(Weight).where(Weight.user_id == user.id, Weight.day == d))
    if w is None:
        db.add(Weight(user_id=user.id, day=d, kg=kg))
    else:
        w.kg = kg


# ---------- Apple Health via an iPhone Shortcut ----------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@router.post("/profile/onboarded")
def mark_onboarded(user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    _profile(db, user).onboarded = True
    db.commit()
    return _me(db, user)


@router.post("/profile/health-token")
def new_health_token(user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    """A new key for the Shortcut; the old one stops working. Shown once."""
    token = "ft_" + secrets.token_urlsafe(24)
    _profile(db, user).health_token_hash = _token_hash(token)
    db.commit()
    return {"token": token}


@router.delete("/profile/health-token")
def drop_health_token(user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    _profile(db, user).health_token_hash = None
    db.commit()
    return {"ok": True}


class HealthIn(BaseModel):
    day: date | None = Field(None, alias="date")
    active_kcal: float | None = Field(None, ge=0, le=10000)
    steps: int | None = Field(None, ge=0, le=200000)
    weight_kg: float | None = Field(None, ge=20, le=400)


@router.post("/health")
def receive_health(body: HealthIn, authorization: str = Header(""), db: Session = Depends(get_db)):
    """Called by the Shortcut, not the app: the key in the header stands in for the session."""
    token = authorization.removeprefix("Bearer ").strip()
    profile = db.scalar(select(Profile).where(Profile.health_token_hash == _token_hash(token))) if token else None
    if profile is None:
        raise HTTPException(status_code=401, detail="Неверный ключ")
    user = db.get(User, profile.user_id)
    today = _today(user)
    d = body.day or today
    if d > today or d < today - timedelta(days=30):
        raise HTTPException(status_code=400, detail="Дата должна быть за последние 30 дней")
    if body.active_kcal is not None or body.steps is not None:
        a = db.scalar(select(Activity).where(Activity.user_id == user.id, Activity.day == d))
        if a is None:
            a = Activity(user_id=user.id, day=d)
            db.add(a)
        # A field the Shortcut didn't send keeps what was there.
        if body.active_kcal is not None:
            a.active_kcal = round(body.active_kcal, 1)
        if body.steps is not None:
            a.steps = body.steps
    if body.weight_kg is not None:
        _upsert_weight(db, user, d, round(body.weight_kg, 1))
    db.commit()
    return {"ok": True, "date": d.isoformat()}


# ---------- push reminders ----------

class NotifyIn(BaseModel):
    evening: bool
    hour: int = Field(ge=17, le=23)
    weekly: bool
    share_streak: bool


@router.put("/profile/notify")
def put_notify(body: NotifyIn, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    profile = _profile(db, user)
    profile.remind_evening, profile.remind_hour = body.evening, body.hour
    profile.weekly_review_push, profile.share_streak = body.weekly, body.share_streak
    db.commit()
    return _profile_dict(db, user, profile)


@router.get("/push/key")
def push_key(_: User = Depends(auth.current_user)):
    return {"public_key": get_settings().vapid_public_key if push.enabled() else None}


class SubscriptionKeys(BaseModel):
    p256dh: str = Field(max_length=200)
    auth: str = Field(max_length=100)


class SubscriptionIn(BaseModel):
    endpoint: str = Field(pattern="^https://", max_length=1000)
    keys: SubscriptionKeys


@router.post("/push/subscribe")
def push_subscribe(body: SubscriptionIn, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    # The same device signing in as someone else moves to that account.
    sub = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint))
    if sub is None:
        sub = PushSubscription(endpoint=body.endpoint, user_id=user.id, p256dh=body.keys.p256dh, auth=body.keys.auth)
        db.add(sub)
    else:
        sub.user_id, sub.p256dh, sub.auth = user.id, body.keys.p256dh, body.keys.auth
    db.commit()
    return {"ok": True}


class EndpointIn(BaseModel):
    endpoint: str


@router.post("/push/unsubscribe")
def push_unsubscribe(body: EndpointIn, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    sub = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint,
                                                   PushSubscription.user_id == user.id))
    if sub is not None:
        db.delete(sub)
        db.commit()
    return {"ok": True}


@router.post("/push/test")
def push_test(user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    sent = push.send(db, user, "🥗 Уведомления работают", "Так будут выглядеть напоминания.")
    if not sent:
        raise HTTPException(status_code=400, detail="Не удалось отправить: включи уведомления на этом устройстве")
    return {"sent": sent}


# ---------- shared streaks ----------

@router.get("/streaks")
def streaks(user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    """Streaks of everyone who chose to share theirs; visible only to those who share too."""
    mine = db.get(Profile, user.id)
    if mine is None or not mine.share_streak:
        return {"joined": False, "people": []}
    people = []
    for u in db.scalars(select(User).join(Profile, Profile.user_id == User.id).where(Profile.share_streak.is_(True))):
        n, today_logged = diary.streak(db, u, diary.today(u))
        people.append({"name": u.username, "streak": n, "today": today_logged, "me": u.id == user.id})
    people.sort(key=lambda p: (-p["streak"], not p["me"], p["name"]))
    return {"joined": True, "people": people}
