from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app import auth, foods, usage
from app.config import get_settings
from app.database import get_db
from app.drafts import build_draft
from app.images import release_memory, shrink
from app.models import Food, Invite, Meal, MealItem, User, Weight, utcnow
from app.recognize import RecognizeError, RecognizeErrorWithResponse, recognize

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
    try:
        result = recognize(image, note[:500])
    except RecognizeErrorWithResponse as exc:
        usage.record_call(db, user, exc.response, model=model, error=str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except RecognizeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    cost = usage.record_call(db, user, result.response, model=model)
    draft = build_draft(db, result.recognition, user.id)
    draft["cost_usd"] = round(cost, 4)
    draft["spent_today"] = round(usage.spent_today(db, user), 4)
    return draft


# ---------- foods ----------

@router.get("/foods/search")
def search_foods(q: str, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    return [foods.food_dict(f) for f in foods.search(db, q, user.id, limit=15)]


class FoodIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kcal_100: float = Field(ge=0, le=900)
    protein_100: float = Field(ge=0, le=100)
    fat_100: float = Field(ge=0, le=100)
    carbs_100: float = Field(ge=0, le=100)


@router.post("/foods")
def add_food(body: FoodIn, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    food = Food(
        source="custom", name=body.name.strip(), name_ru=body.name.strip(), owner_id=user.id,
        kcal=body.kcal_100, protein=body.protein_100, fat=body.fat_100, carbs=body.carbs_100,
    )
    db.add(food)
    db.commit()
    return foods.food_dict(food)


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
    )
    # A database food's numbers are taken from the database, not from the client.
    food = db.get(Food, body.food_id) if body.food_id is not None else None
    if food is not None and food.owner_id in (None, user.id):
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
    return {
        "date": (date_ or datetime.now(ZoneInfo(user.timezone)).date()).isoformat(),
        "meals": out,
        "totals": totals,
        "goals": _me(db, user)["goals"],
    }


# ---------- reports ----------

STREAK_LOOKBACK_DAYS = 400
IN_GOAL_RATIO = 1.05  # same 5% slack as the red "over" bar on the day screen


def _today(user: User) -> date:
    return datetime.now(ZoneInfo(user.timezone)).date()


def _local_day(dt: datetime, tz: str) -> date:
    return dt.replace(tzinfo=UTC).astimezone(ZoneInfo(tz)).date()


def _streak(db: Session, user: User, today: date) -> int:
    """Days in a row with at least one meal, ending today, or yesterday if today is still empty."""
    since, _ = usage.day_bounds_utc(user.timezone, today - timedelta(days=STREAK_LOOKBACK_DAYS))
    logged = {
        _local_day(dt, user.timezone)
        for dt in db.scalars(select(Meal.eaten_at).where(Meal.user_id == user.id, Meal.eaten_at >= since))
    }
    day = today if today in logged else today - timedelta(days=1)
    n = 0
    while day in logged:
        n += 1
        day -= timedelta(days=1)
    return n


@router.get("/stats")
def stats(days: int = Query(7, ge=1, le=366), user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    today = _today(user)
    first = today - timedelta(days=days - 1)
    start, _ = usage.day_bounds_utc(user.timezone, first)
    _, end = usage.day_bounds_utc(user.timezone, today)
    meals = db.scalars(
        select(Meal)
        .where(Meal.user_id == user.id, Meal.eaten_at >= start, Meal.eaten_at < end)
        .options(selectinload(Meal.items))
    ).all()

    by_day: dict[date, list[Meal]] = {}
    for m in meals:
        by_day.setdefault(_local_day(m.eaten_at, user.timezone), []).append(m)

    rows = []
    for k in range(days):
        d = first + timedelta(days=k)
        day_meals = by_day.get(d, [])
        rows.append({"date": d.isoformat(), "meals": len(day_meals), **_totals([i for m in day_meals for i in m.items])})

    logged = [r for r in rows if r["meals"]]
    avg = {key: round(sum(r[key] for r in logged) / len(logged), 1) if logged else 0.0
           for key in ("kcal", "protein", "fat", "carbs")}
    weights = db.scalars(
        select(Weight).where(Weight.user_id == user.id, Weight.day >= first, Weight.day <= today).order_by(Weight.day)
    ).all()
    last_weight = db.scalar(select(Weight).where(Weight.user_id == user.id).order_by(Weight.day.desc()).limit(1))
    return {
        "from": first.isoformat(),
        "to": today.isoformat(),
        "goals": _me(db, user)["goals"],
        "days": rows,
        "avg": avg,
        "logged_days": len(logged),
        "in_goal_days": sum(1 for r in logged if r["kcal"] <= user.kcal_goal * IN_GOAL_RATIO),
        "streak": _streak(db, user, today),
        "weights": [{"date": w.day.isoformat(), "kg": w.kg} for w in weights],
        "last_weight": {"date": last_weight.day.isoformat(), "kg": last_weight.kg} if last_weight else None,
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
