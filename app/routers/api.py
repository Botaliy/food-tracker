import base64
import binascii
import csv
import hashlib
import io
import logging
import json
import re
import secrets
import zipfile
from types import SimpleNamespace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import (auth, barcodes, coach, corrections, diary, energy, foods, idempotency, insights, photos, push, recipes,
                 review as review_mod, usage)
from app.config import get_settings
from app.database import get_db
from app.drafts import build_draft, build_recipe_draft, draft_item
from app.images import TEXT_IMAGE_EDGE, release_memory, shrink
from app.models import (Activity, ChatMessage, Food, Invite, Meal, MealItem, Profile, PushSubscription, RecipeItem, Review, User,
                        Water, Weight, Workout, utcnow)
from app.recognize import RecognizedItem, RecognizeError, RecognizeErrorWithResponse, recognize, recognize_recipe

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


def _set_goals(user: User, body: GoalsIn) -> None:
    user.kcal_goal, user.protein_goal, user.fat_goal, user.carbs_goal = body.kcal, body.protein, body.fat, body.carbs
    if tz := _valid_tz(body.timezone):
        user.timezone = tz


@router.put("/me")
def update_me(body: GoalsIn, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    _set_goals(user, body)
    db.commit()
    return _me(db, user)


# ---------- recognition ----------

def _read_photo(photo: UploadFile | None) -> bytes | None:
    """The upload shrunk to a JPEG for the model, or None without one."""
    if photo is None or not photo.filename:
        return None
    raw = photo.file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Фото слишком большое")
    image = shrink(raw)
    del raw
    release_memory()
    if image is None:
        raise HTTPException(status_code=400, detail="Не получилось открыть фото")
    return image


@router.post("/recognize")
def recognize_meal(
    photo: UploadFile | None = File(None),
    note: str = Form(""),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
):
    if problem := usage.limit_problem(db, user):
        raise HTTPException(status_code=429, detail=problem)

    image = _read_photo(photo)
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
    # A thumbnail only for a plate that has food on it; saving the meal claims it.
    thumb = photos.save_thumbnail(image) if image is not None and result.recognition.items else None
    del image
    draft = build_draft(db, result.recognition, user.id, photo=thumb)
    draft["personal_hints"] = len(hints)
    draft["cost_usd"] = round(cost, 4)
    draft["spent_today"] = round(usage.spent_today(db, user), 4)
    photos.maybe_cleanup(db)
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
    fiber_100: float | None = Field(None, ge=0, le=100)
    sugar_100: float | None = Field(None, ge=0, le=100)
    salt_100: float | None = Field(None, ge=0, le=100)
    barcode: str | None = Field(None, max_length=32)
    package_grams: float | None = Field(None, ge=1, le=5000)


@router.post("/foods")
def add_food(body: FoodIn, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    food = Food(
        source="custom", name=body.name.strip(), name_ru=body.name.strip(), owner_id=user.id,
        barcode=barcodes.normalize(body.barcode) if body.barcode else None,
        kcal=body.kcal_100, protein=body.protein_100, fat=body.fat_100, carbs=body.carbs_100,
        fiber=body.fiber_100, sugar=body.sugar_100, salt=body.salt_100, package_grams=body.package_grams,
    )
    db.add(food)
    db.commit()
    return foods.food_dict(food)


class PackageIn(BaseModel):
    grams: float | None = Field(None, ge=1, le=5000)  # None: forget the pack


@router.put("/foods/{food_id}/package")
def set_food_package(food_id: int, body: PackageIn, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    """The weight of one pack of a shared food, told once by whoever logs it first. Scanned and
    hand-added foods only: USDA rows are generic, and a dish has its usual portion."""
    food = foods.usable(db.get(Food, food_id), user.id)
    if food is None or food.source not in ("off", "custom"):
        raise HTTPException(status_code=404, detail="Продукт не найден")
    food.package_grams = body.grams
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
    return {**foods.food_dict(food), "barcode": norm, "last_grams": _last_grams(db, user, [food.id]).get(food.id)}


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
    fiber_100: float | None = Field(None, ge=0, le=100)  # None = unknown
    sugar_100: float | None = Field(None, ge=0, le=100)
    salt_100: float | None = Field(None, ge=0, le=100)
    ai_grams: float | None = Field(None, gt=0, le=5000)


class MealIn(BaseModel):
    title: str = Field(default="", max_length=200)
    eaten_at: datetime | None = None
    items: list[ItemIn] = Field(min_length=1, max_length=30)
    photo: str | None = Field(None, max_length=64)  # a name from /api/recognize
    guess_kcal: float | None = Field(None, ge=1, le=10000)  # new meals only: a guess can't be made afterwards


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
        fiber_100=body.fiber_100, sugar_100=body.sugar_100, salt_100=body.salt_100,
        ai_grams=body.ai_grams,
    )
    # A database food's numbers are taken from the database, not from the client.
    food = foods.usable(db.get(Food, body.food_id), user.id) if body.food_id is not None else None
    if food is not None:
        item.food_id = food.id
        _copy_food(item, food)
    return item


def _copy_food(item: MealItem | RecipeItem, food: Food) -> None:
    item.source = food.source
    item.kcal_100, item.protein_100, item.fat_100, item.carbs_100 = food.kcal, food.protein, food.fat, food.carbs
    # Where the database knows fibre/sugar/salt it wins; where it doesn't, the client's
    # value (the model's estimate from the draft) is kept.
    for key in foods.EXTRA:
        if (v := getattr(food, key)) is not None:
            setattr(item, f"{key}_100", v)


def _totals(items) -> dict:
    t = {"kcal": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0}
    for i in items:
        k = i.grams / 100
        t["kcal"] += i.kcal_100 * k
        t["protein"] += i.protein_100 * k
        t["fat"] += i.fat_100 * k
        t["carbs"] += i.carbs_100 * k
    out = {key: round(v, 1) for key, v in t.items()}
    # Fibre, sugar, salt: known values only; None when no item has the value.
    out.update({k: None if v is None else round(v, 1) for k, v in foods.extra_totals(items).items()})
    return out


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
                "fiber_100": i.fiber_100, "sugar_100": i.sugar_100, "salt_100": i.salt_100,
                "ai_grams": i.ai_grams,
            }
            for i in meal.items
        ],
        "totals": _totals(meal.items),
        "photo": meal.photo,
        "guess_kcal": meal.guess_kcal,
    }


def _own_meal(db: Session, meal_id: int, user: User) -> Meal:
    meal = db.get(Meal, meal_id)
    if meal is None or meal.user_id != user.id:
        raise HTTPException(status_code=404, detail="Приём пищи не найден")
    return meal


def _new_meal(db: Session, user: User, body: MealIn) -> Meal:
    """A new meal, flushed but not committed: the caller owns the transaction."""
    meal = Meal(user_id=user.id, title=body.title.strip(), eaten_at=_to_utc(body.eaten_at, user.timezone),
                guess_kcal=body.guess_kcal)
    meal.items = [_item(db, i, user) for i in body.items]
    # A photo that is gone (cleaned up overnight) or taken just doesn't attach: the meal still saves.
    if photos.claimable(db, body.photo):
        meal.photo = body.photo
    db.add(meal)
    db.flush()
    return meal


# The quick writes take an optional Idempotency-Key (app.idempotency): a repeat of a key this
# user already used answers with the current state instead of writing again.
IdemKey = Header(None, alias="Idempotency-Key")


def _commit_keyed(db: Session, key: str | None) -> bool:
    """Commit the write and its key; False when a parallel request with the same key won."""
    try:
        db.commit()
    except IntegrityError:
        if key is None:
            raise
        db.rollback()
        return False
    return True


def _meal_replay(db: Session, user: User, key: str) -> dict:
    done = idempotency.used(db, user, key)
    meal = db.get(Meal, done.ref_id) if done and done.ref_id else None
    if meal is None or meal.user_id != user.id:
        raise HTTPException(status_code=404, detail="Этот приём пищи уже удалён")
    return _meal_dict(meal, user.timezone)


@router.post("/meals")
def add_meal(body: MealIn, user: User = Depends(auth.current_user), db: Session = Depends(get_db),
             idempotency_key: str | None = IdemKey):
    key = idempotency.checked(idempotency_key)
    if key:
        idempotency.sweep(db)
        if idempotency.used(db, user, key):
            return _meal_replay(db, user, key)
    meal = _new_meal(db, user, body)
    idempotency.remember(db, user, key, meal.id)
    if not _commit_keyed(db, key):
        return _meal_replay(db, user, key)
    return _meal_dict(meal, user.timezone)


@router.put("/meals/{meal_id}")
def update_meal(meal_id: int, body: MealIn, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    meal = _own_meal(db, meal_id, user)
    meal.title = body.title.strip()
    if body.eaten_at is not None:
        meal.eaten_at = _to_utc(body.eaten_at, user.timezone)
    meal.items = [_item(db, i, user) for i in body.items]
    # No "photo" in the body keeps the current one; an explicit null removes it.
    dropped = None
    if "photo" in body.model_fields_set and body.photo != meal.photo:
        if body.photo is None:
            dropped, meal.photo = meal.photo, None
        elif photos.claimable(db, body.photo, meal.id):
            dropped, meal.photo = meal.photo, body.photo
    db.commit()
    photos.delete(dropped)
    return _meal_dict(meal, user.timezone)


@router.delete("/meals/{meal_id}")
def delete_meal(meal_id: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    meal = _own_meal(db, meal_id, user)
    photo = meal.photo
    db.delete(meal)
    db.commit()
    photos.delete(photo)
    return {"ok": True}


@router.get("/photos/{name}")
def get_photo(name: str, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    """Your own meals' photos, and drafts' photos nobody has claimed yet."""
    path = photos.path_of(name)  # validates the name before any filesystem access
    if path is None:
        raise HTTPException(status_code=404, detail="Фото не найдено")
    owner = db.scalar(select(Meal.user_id).where(Meal.photo == name).limit(1))
    if (owner is not None and owner != user.id) or not path.is_file():
        raise HTTPException(status_code=404, detail="Фото не найдено")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=31536000, immutable"})


RECENT_DAYS = 30


@router.get("/meals/recent")
def recent_meals(limit: int = Query(8, ge=1, le=20), user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    return _recent(db, user, limit)


def _recent(db: Session, user: User, limit: int) -> list[dict]:
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


# ---------- own dishes (recipes) ----------
# A dish is a Food (source "recipe") only its owner sees, so search, the meal editor
# and logged meals treat it like any other food.

class IngredientIn(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    grams: float = Field(gt=0, le=10000)  # a whole pot, not one helping
    food_id: int | None = None
    source: str = "estimate"
    kcal_100: float = Field(ge=0, le=900)
    protein_100: float = Field(ge=0, le=100)
    fat_100: float = Field(ge=0, le=100)
    carbs_100: float = Field(ge=0, le=100)
    fiber_100: float | None = Field(None, ge=0, le=100)  # None = unknown
    sugar_100: float | None = Field(None, ge=0, le=100)
    salt_100: float | None = Field(None, ge=0, le=100)


class RecipeIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    items: list[IngredientIn] = Field(min_length=1, max_length=40)
    cooked_grams: float | None = Field(None, gt=0, le=50000)  # None = the sum of the ingredients
    portion_grams: float | None = Field(None, gt=0, le=5000)


def _own_recipe(db: Session, recipe_id: int, user: User) -> Food:
    food = db.get(Food, recipe_id)
    if food is None or food.source != "recipe" or food.owner_id != user.id:
        raise HTTPException(status_code=404, detail="Блюдо не найдено")
    return food


def _recipe_dict(food: Food, ingredients: bool = False) -> dict:
    out = {
        **foods.food_dict(food),
        "id": food.id,
        "name": food.name,
        "cooked_grams": food.cooked_grams,
        "portion_grams": food.portion_grams,
        "raw_grams": round(sum(i.grams for i in food.recipe_items), 1),
        "ingredient_count": len(food.recipe_items),
        "portion_kcal": round(food.kcal * food.portion_grams / 100) if food.portion_grams else None,
    }
    if ingredients:
        out["items"] = [
            {
                "id": i.id, "name": i.name, "grams": i.grams, "food_id": i.ingredient_food_id, "source": i.source,
                "kcal_100": i.kcal_100, "protein_100": i.protein_100, "fat_100": i.fat_100, "carbs_100": i.carbs_100,
                "fiber_100": i.fiber_100, "sugar_100": i.sugar_100, "salt_100": i.salt_100,
            }
            for i in food.recipe_items
        ]
    return out


def _ingredient(db: Session, body: IngredientIn, user: User, dish_id: int | None) -> RecipeItem:
    item = RecipeItem(
        name=body.name.strip(), grams=body.grams, source=body.source if body.source in {"estimate", "manual"} else "estimate",
        kcal_100=body.kcal_100, protein_100=body.protein_100, fat_100=body.fat_100, carbs_100=body.carbs_100,
        fiber_100=body.fiber_100, sugar_100=body.sugar_100, salt_100=body.salt_100,
    )
    food = foods.usable(db.get(Food, body.food_id), user.id) if body.food_id is not None else None
    if food is not None:
        if dish_id is not None and food.id == dish_id:
            raise HTTPException(status_code=400, detail="Блюдо не может входить само в себя")
        item.ingredient_food_id = food.id
        _copy_food(item, food)
    return item


def _fill_recipe(db: Session, food: Food, body: RecipeIn, user: User) -> None:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Назови блюдо")
    items = [_ingredient(db, i, user, food.id) for i in body.items]
    try:
        values = recipes.per100(items, body.cooked_grams)
        recipes.check_plausible(values)
    except recipes.RecipeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    food.name = food.name_ru = name
    food.kcal, food.protein, food.fat, food.carbs = (values[k] for k in recipes.MACROS)
    food.fiber, food.sugar, food.salt = (values[k] for k in foods.EXTRA)
    food.cooked_grams, food.portion_grams = body.cooked_grams, body.portion_grams
    # Meals already logged from the dish keep their copied numbers: only the food row changes.
    food.recipe_items = items


@router.get("/recipes")
def list_recipes(user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    dishes = list(db.scalars(
        select(Food).where(Food.source == "recipe", Food.owner_id == user.id).options(selectinload(Food.recipe_items))
    ))
    # The ones logged most recently first, then the newest.
    last = dict(db.execute(
        select(MealItem.food_id, func.max(MealItem.id))
        .join(Meal, Meal.id == MealItem.meal_id)
        .where(Meal.user_id == user.id, MealItem.food_id.in_([f.id for f in dishes]))
        .group_by(MealItem.food_id)
    ).all()) if dishes else {}
    dishes.sort(key=lambda f: (last.get(f.id, 0), f.id), reverse=True)
    return [_recipe_dict(f) for f in dishes]


@router.get("/recipes/{recipe_id}")
def get_recipe(recipe_id: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    return _recipe_dict(_own_recipe(db, recipe_id, user), ingredients=True)


@router.post("/recipes")
def add_recipe(body: RecipeIn, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    food = Food(source="recipe", owner_id=user.id)
    _fill_recipe(db, food, body, user)
    db.add(food)
    db.commit()
    return _recipe_dict(food, ingredients=True)


@router.put("/recipes/{recipe_id}")
def update_recipe(recipe_id: int, body: RecipeIn, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    food = _own_recipe(db, recipe_id, user)
    _fill_recipe(db, food, body, user)
    db.commit()
    return _recipe_dict(food, ingredients=True)


@router.delete("/recipes/{recipe_id}")
def delete_recipe(recipe_id: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    """Meals logged from the dish stay: their items lose the link (food_id SET NULL), not their numbers."""
    db.delete(_own_recipe(db, recipe_id, user))
    db.commit()
    return {"ok": True}


@router.post("/recipes/draft")
def recipe_draft(
    photo: UploadFile | None = File(None),
    note: str = Form(""),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
):
    """A dish from a description or a photo (ingredients, a written recipe, a screenshot), for the user
    to check before saving. Same model, limits and accounting as /api/recognize; no thumbnail is kept."""
    if problem := usage.limit_problem(db, user):
        raise HTTPException(status_code=429, detail=problem)
    image = _read_photo(photo)
    model = get_settings().recognize_model
    try:
        result = recognize_recipe(image, note[:2000])
    except RecognizeErrorWithResponse as exc:
        usage.record_call(db, user, exc.response, model=model, error=str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except RecognizeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    del image
    cost = usage.record_call(db, user, result.response, model=model)
    draft = build_recipe_draft(db, result.recipe, user.id)
    draft["cost_usd"] = round(cost, 4)
    draft["spent_today"] = round(usage.spent_today(db, user), 4)
    return draft


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
        "refs": diary.nutrient_refs(user.kcal_goal),
        "activity": _activity_dict(act),
        "workouts": [_workout_dict(w) for w in db.scalars(
            select(Workout).where(Workout.user_id == user.id, Workout.day == the_day)
            .order_by(Workout.started.is_(None), Workout.started, Workout.id))],
        "streaks": diary.streaks(db, user, _today(user)),
        "weight": _weight_outlook(db, user),
        "water": _water(db, user, the_day),
        "suggest": insights.suggestions(
            user.kcal_goal - totals["kcal"], user.protein_goal - totals["protein"], _recent(db, user, 20)
        ) if the_day == _today(user) and meals else [],
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


def _water(db: Session, user: User, d: date) -> dict | None:
    profile = db.get(Profile, user.id)
    if profile is not None and not profile.track_water:
        return None
    w = db.scalar(select(Water).where(Water.user_id == user.id, Water.day == d))
    return {"ml": w.ml if w else 0, "goal": profile.water_goal_ml if profile else 2000}


class WaterIn(BaseModel):
    delta_ml: int = Field(ge=-1000, le=1000)


def _add_water(db: Session, user: User, d: date, delta_ml: int) -> Water:
    w = db.scalar(select(Water).where(Water.user_id == user.id, Water.day == d))
    if w is None:
        w = Water(user_id=user.id, day=d, ml=0)
        db.add(w)
    w.ml = max(0, min(10000, (w.ml or 0) + delta_ml))
    return w


def _water_now(db: Session, user: User, d: date) -> dict:
    w = db.scalar(select(Water).where(Water.user_id == user.id, Water.day == d))
    return _water(db, user, d) or {"ml": w.ml if w else 0}


@router.post("/water")
def add_water(body: WaterIn, user: User = Depends(auth.current_user), db: Session = Depends(get_db),
              idempotency_key: str | None = IdemKey):
    key = idempotency.checked(idempotency_key)
    d = _today(user)
    if key:
        idempotency.sweep(db)
        if idempotency.used(db, user, key):
            return _water_now(db, user, d)
    _add_water(db, user, d, body.delta_ml)
    idempotency.remember(db, user, key)
    _commit_keyed(db, key)
    return _water_now(db, user, d)


@router.get("/insights")
def get_insights(user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    return insights.observations(db, user, _today(user))


def _activity_dict(a: Activity | None) -> dict | None:
    return None if a is None else {"active_kcal": a.active_kcal, "steps": a.steps}


# Activity totals: the same bounds whether the Shortcut or a confirmed coach card sends them.
MAX_STEPS = 200000
MAX_ACTIVE_KCAL = 10000


def _upsert_activity(db: Session, user: User, d: date, kcal: float | None, steps: int | None) -> None:
    """A field that wasn't sent (None) keeps what was there."""
    a = db.scalar(select(Activity).where(Activity.user_id == user.id, Activity.day == d))
    if a is None:
        a = Activity(user_id=user.id, day=d)
        db.add(a)
    if kcal is not None:
        a.active_kcal = round(kcal, 1)
    if steps is not None:
        a.steps = steps


class WorkoutIn(BaseModel):
    kind: str = Field(min_length=1, max_length=40)
    minutes: int = Field(ge=1, le=1440)
    kcal: float | None = Field(None, gt=0, le=5000)
    distance_km: float | None = Field(None, gt=0, le=500)
    avg_hr: int | None = Field(None, ge=30, le=230)
    time: str | None = None  # local "HH:MM"

    @field_validator("kind", mode="before")
    @classmethod
    def _strip(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("time")
    @classmethod
    def _hhmm(cls, v):
        if v is not None and not _HHMM.fullmatch(v):
            raise ValueError(f"time {v!r}")
        return v


def _workout_dict(w: Workout) -> dict:
    return {"id": w.id, "time": w.started, "kind": w.kind, "minutes": w.minutes, "kcal": w.kcal,
            "distance_km": w.distance_km, "avg_hr": w.avg_hr}


@router.delete("/workouts/{workout_id}")
def delete_workout(workout_id: int, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    w = db.get(Workout, workout_id)
    if w is None or w.user_id != user.id:
        raise HTTPException(status_code=404, detail="Тренировка не найдена")
    db.delete(w)
    db.commit()
    return {"ok": True}


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
    # Over the days that have the value at all: an unknown day isn't a zero day.
    for key in foods.EXTRA:
        known = [r[key] for r in logged if r[key] is not None]
        avg[key] = round(sum(known) / len(known), 1) if known else None
    weights = _weights(db, user, first, today)
    last_weight = db.scalar(select(Weight).where(Weight.user_id == user.id).order_by(Weight.day.desc()).limit(1))
    return {
        "from": first.isoformat(),
        "to": today.isoformat(),
        "goals": _me(db, user)["goals"],
        "refs": diary.nutrient_refs(user.kcal_goal),
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
def put_weight(body: WeightIn, user: User = Depends(auth.current_user), db: Session = Depends(get_db),
               idempotency_key: str | None = IdemKey):
    key = idempotency.checked(idempotency_key)
    d = _weight_day(user, body.day)
    if key:
        idempotency.sweep(db)
    if key is None or not idempotency.used(db, user, key):
        _upsert_weight(db, user, d, body.kg)
        idempotency.remember(db, user, key)
        _commit_keyed(db, key)
    w = db.scalar(select(Weight).where(Weight.user_id == user.id, Weight.day == d))
    # A replay answers with the day's weight as it is now (gone if deleted since).
    return {"date": d.isoformat(), "kg": w.kg if w else body.kg}


@router.delete("/weight")
def delete_weight(date_: date | None = Query(None, alias="date"), user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    d = _weight_day(user, date_)
    w = db.scalar(select(Weight).where(Weight.user_id == user.id, Weight.day == d))
    if w is not None:
        db.delete(w)
        db.commit()
    return {"ok": True}


# ---------- export ----------

def _csv(header: list[str], rows) -> bytes:
    """UTF-8 with a BOM, so Excel opens Cyrillic right; None becomes an empty cell."""
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf)
    w.writerow(header)
    for row in rows:
        w.writerow(["" if v is None else v for v in row])
    return buf.getvalue().encode("utf-8")


def _portion(per100: float | None, grams: float, digits: int = 1) -> float | None:
    return None if per100 is None else round(per100 * grams / 100, digits)


@router.get("/export")
def export(
    first: date | None = Query(None, alias="from"),
    last: date | None = Query(None, alias="to"),
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
):
    """Everything the user logged, as CSV files in a zip. Dates are inclusive, local."""
    if first and last and first > last:
        raise HTTPException(status_code=400, detail="Начальная дата позже конечной")
    zone = ZoneInfo(user.timezone)
    meal_q = select(Meal).where(Meal.user_id == user.id).options(selectinload(Meal.items)).order_by(Meal.eaten_at)
    if first:
        meal_q = meal_q.where(Meal.eaten_at >= usage.day_bounds_utc(user.timezone, first)[0])
    if last:
        meal_q = meal_q.where(Meal.eaten_at < usage.day_bounds_utc(user.timezone, last)[1])

    def meal_rows():
        for m in db.scalars(meal_q):
            local = m.eaten_at.replace(tzinfo=UTC).astimezone(zone)
            for i in m.items:
                yield [local.date().isoformat(), local.strftime("%H:%M"), m.title, i.name, round(i.grams, 1),
                       _portion(i.kcal_100, i.grams), _portion(i.protein_100, i.grams), _portion(i.fat_100, i.grams),
                       _portion(i.carbs_100, i.grams), _portion(i.fiber_100, i.grams), _portion(i.sugar_100, i.grams),
                       _portion(i.salt_100, i.grams, 2), i.source]

    def by_day(model, *cols):
        q = select(model).where(model.user_id == user.id).order_by(model.day)
        if first:
            q = q.where(model.day >= first)
        if last:
            q = q.where(model.day <= last)
        return ([r.day.isoformat(), *(getattr(r, c) for c in cols)] for r in db.scalars(q))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("meals.csv", _csv(["date", "time", "meal_title", "item_name", "grams", "kcal", "protein", "fat",
                                       "carbs", "fiber", "sugar", "salt", "source"], meal_rows()))
        zf.writestr("weights.csv", _csv(["date", "kg"], by_day(Weight, "kg")))
        zf.writestr("water.csv", _csv(["date", "ml"], by_day(Water, "ml")))
        zf.writestr("activity.csv", _csv(["date", "active_kcal", "steps"], by_day(Activity, "active_kcal", "steps")))
        workout_cols = ("kind", "minutes", "kcal", "distance_km", "avg_hr")
        zf.writestr("workouts.csv", _csv(["date", "time", *workout_cols], by_day(Workout, "started", *workout_cols)))
    name = f"food-tracker-{_today(user).isoformat()}.zip"
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


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
        "track_water": profile.track_water,
        "water_goal_ml": profile.water_goal_ml,
        "weight_kg": last_weight.kg if last_weight else None,
        "activity_levels": [{"value": k, "label": v} for k, v in energy.ACTIVITY_LEVELS.items()],
        "rates": [{"value": k, "label": v} for k, v in energy.RATES.items()],
        "formula": _formula(profile, last_weight, today),
        "android_apk": get_settings().android_apk.is_file(),
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


_NUMBER = re.compile(r"-?\d[\d\s\u00a0\u202f]*(?:[.,]\d+)?")


def _number(value, combine=sum) -> float | None:
    """A number the way Shortcuts may send it: 512.3, "512,3 kcal", "8 400", or a list of
    samples (summed, or for weight the last one), or {"value": ...}."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        return _number(value.get("value") or value.get("quantity"), combine)
    if isinstance(value, list):
        nums = [n for n in (_number(v, combine) for v in value) if n is not None]
        return combine(nums) if nums else None
    m = _NUMBER.search(str(value))
    if not m:
        return None
    return float(re.sub(r"[\s\u00a0\u202f]", "", m.group()).replace(",", "."))


def _health_day(value) -> date | None:
    """"2026-11-26", or a full date-time; anything else means today."""
    m = re.search(r"\d{4}-\d{2}-\d{2}", str(value or ""))
    try:
        return date.fromisoformat(m.group()) if m else None
    except ValueError:
        return None


async def _health_payload(request: Request) -> dict:
    """JSON or form fields: the Shortcut's "Request Body" can be either."""
    raw = await request.body()
    try:
        data = json.loads(raw) if raw.strip().startswith(b"{") else dict(await request.form())
    except (ValueError, UnicodeDecodeError):
        data = {}
    return data if isinstance(data, dict) else {}


@router.post("/health")
async def receive_health(request: Request, authorization: str = Header(""), db: Session = Depends(get_db)):
    """Called by the Shortcut, not the app: the key in the header stands in for the session."""
    token = authorization.removeprefix("Bearer ").strip()
    profile = db.scalar(select(Profile).where(Profile.health_token_hash == _token_hash(token))) if token else None
    if profile is None:
        raise HTTPException(status_code=401, detail="Неверный ключ")
    data = await _health_payload(request)
    kcal = _number(data.get("active_kcal"))
    steps = _number(data.get("steps"))
    weight = _number(data.get("weight_kg"), combine=lambda xs: xs[-1])
    kcal = kcal if kcal is not None and 0 <= kcal <= MAX_ACTIVE_KCAL else None
    steps = int(steps) if steps is not None and 0 <= steps <= MAX_STEPS else None
    weight = weight if weight is not None and 20 <= weight <= 400 else None
    if kcal is None and steps is None and weight is None:
        logger.warning("Health payload not understood: %s", (await request.body())[:500])
        raise HTTPException(status_code=400, detail=f"Не нашёл чисел active_kcal, steps или weight_kg в запросе: {str(data)[:200]}")
    user = db.get(User, profile.user_id)
    today = _today(user)
    d = _health_day(data.get("date")) or today
    if d > today or d < today - timedelta(days=30):
        raise HTTPException(status_code=400, detail="Дата должна быть за последние 30 дней")
    if kcal is not None or steps is not None:
        _upsert_activity(db, user, d, kcal, steps)
    if weight is not None:
        _upsert_weight(db, user, d, round(weight, 1))
    db.commit()
    return {"ok": True, "date": d.isoformat()}


# ---------- push reminders ----------

class WaterSettingsIn(BaseModel):
    track_water: bool
    water_goal_ml: int = Field(ge=500, le=5000)


@router.put("/profile/water")
def put_water_settings(body: WaterSettingsIn, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    profile = _profile(db, user)
    profile.track_water, profile.water_goal_ml = body.track_water, body.water_goal_ml
    db.commit()
    return _profile_dict(db, user, profile)


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
        guess = diary.guess_score(db, u, diary.today(u))
        people.append({"name": u.username, "streak": n, "today": today_logged, "me": u.id == user.id,
                       # A couple of lucky plates is not a score yet.
                       "guess": guess if guess and guess["n"] >= diary.GUESS_MIN else None})
    people.sort(key=lambda p: (-p["streak"], not p["me"], p["name"]))
    return {"joined": True, "people": people}


# ---------- coach chat ----------

def _chat_state(db: Session, user: User) -> dict:
    return {"used_today": coach.used_today(db, user), "per_day": get_settings().coach_daily_messages}


# What the coach proposes is checked with the same models and bounds as the regular endpoints,
# stored on its message, and applied only by POST /coach/actions/... through the same helpers.

_HHMM = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
_ITEM_KEYS = ("name", "grams", "food_id", "source", "kcal_100", "protein_100", "fat_100", "carbs_100",
              "fiber_100", "sugar_100", "salt_100")
COACH_MIN_KCAL = 1200
NO_ACTION_REPLY = "Не получилось подготовить запись — напиши подробнее, что именно записать."


def _proposal_payload(db: Session, user: User, call: coach.ToolCall) -> dict:
    """The payload to store, or ValueError/TypeError/KeyError for an unusable call."""
    raw, today = call.input, _today(user)
    if call.name == "log_meal":
        when = str(raw.get("time") or "").strip()
        if when and not _HHMM.fullmatch(when):
            raise ValueError(f"time {when!r}")
        if not isinstance(raw.get("items"), list):
            raise ValueError("no items")
        items = []
        for r in raw["items"][:31]:  # MealIn rejects more than 30
            # The same matching as a recognized photo: database numbers win over the model's.
            d = draft_item(db, RecognizedItem.model_validate({**r, "confidence": "medium"}), user.id)
            items.append(ItemIn.model_validate({k: d[k] for k in _ITEM_KEYS}).model_dump(exclude={"ai_grams"}))
        meal = MealIn(title=str(raw.get("title") or ""), items=items)
        return {"title": meal.title.strip(), "time": when or None, "date": today.isoformat(), "items": items}
    if call.name == "set_goals":
        goals = GoalsIn.model_validate({k: raw[k] for k in ("kcal", "protein", "fat", "carbs")})
        if goals.kcal < COACH_MIN_KCAL:
            raise ValueError(f"kcal {goals.kcal}")
        return {**goals.model_dump(exclude={"timezone"}), "reason": str(raw.get("reason") or "").strip()[:300],
                "old": _me(db, user)["goals"]}
    if call.name == "add_water":
        water = WaterIn(delta_ml=raw["ml"])
        if water.delta_ml <= 0:
            raise ValueError(f"ml {water.delta_ml}")
        return {"ml": water.delta_ml, "date": today.isoformat()}
    if call.name == "log_weight":
        return {"kg": round(WeightIn(kg=raw["kg"]).kg, 1), "date": today.isoformat()}
    if call.name == "log_workout":
        # The tool is strict, so "unknown" comes as 0 or "".
        w = WorkoutIn(kind=str(raw["kind"]), minutes=raw["minutes"], kcal=raw["kcal"] or None,
                      distance_km=raw["distance_km"] or None, avg_hr=raw["avg_hr"] or None,
                      time=str(raw["time"] or "").strip() or None)
        return {**w.model_dump(), "kcal": w.kcal and (round(w.kcal) or None),
                "distance_km": w.distance_km and (round(w.distance_km, 2) or None), "date": today.isoformat()}
    if call.name == "log_activity":
        steps, kcal = raw["steps"] or None, raw["active_kcal"] or None
        if steps is not None and not (isinstance(steps, (int, float)) and 0 < steps <= MAX_STEPS):
            raise ValueError(f"steps {steps!r}")
        if kcal is not None and not (isinstance(kcal, (int, float)) and 0 < kcal <= MAX_ACTIVE_KCAL):
            raise ValueError(f"active_kcal {kcal!r}")
        if steps is None and kcal is None:
            raise ValueError("no activity numbers")
        return {"steps": None if steps is None else int(steps), "active_kcal": None if kcal is None else round(kcal),
                "date": today.isoformat()}
    raise ValueError(f"unknown tool {call.name!r}")


def _proposals(db: Session, user: User, calls: list[coach.ToolCall]) -> list[dict]:
    out = []
    for call in calls:
        try:
            payload = _proposal_payload(db, user, call)
        except (ValueError, TypeError, KeyError) as exc:  # pydantic's ValidationError is a ValueError
            logger.warning("Coach proposal %s dropped: %s", call.name, str(exc)[:300])
            continue
        out.append({"id": secrets.token_urlsafe(6), "type": call.name, "payload": payload, "status": "pending"})
    return out


def _action_summary(a: dict) -> dict:
    p = a["payload"]
    if a["type"] == "log_meal":
        t = _totals([SimpleNamespace(**i) for i in p["items"]])
        return {"title": p["title"], "time": p["time"], "date": p["date"],
                "items": [{"name": i["name"], "grams": i["grams"]} for i in p["items"]],
                "totals": {k: round(t[k]) for k in ("kcal", "protein", "fat", "carbs")}}
    if a["type"] == "set_goals":
        return {"old": p["old"], "new": {k: p[k] for k in ("kcal", "protein", "fat", "carbs")}, "reason": p["reason"]}
    if a["type"] == "add_water":
        return {"ml": p["ml"], "date": p["date"]}
    if a["type"] == "log_weight":
        return {"kg": p["kg"], "date": p["date"]}
    if a["type"] == "log_workout":
        return {k: p[k] for k in ("kind", "minutes", "kcal", "distance_km", "avg_hr", "time", "date")}
    if a["type"] == "log_activity":
        return {k: p[k] for k in ("steps", "active_kcal", "date")}
    return {}


def _msg_dict(m: ChatMessage) -> dict:
    actions = json.loads(m.actions) if m.actions else []
    return {"id": m.id, "role": m.role, "text": m.content, "has_image": bool(m.has_image),
            "actions": [{"id": a["id"], "type": a["type"], "status": a["status"], "summary": _action_summary(a)}
                        for a in actions]}


def _apply_action(db: Session, user: User, a: dict) -> None:
    """The proposal written through the regular endpoints' helpers; no commit."""
    p = a["payload"]
    if a["type"] == "log_meal":
        # The proposed time on the day it was proposed; without one, the moment of acceptance.
        eaten = datetime.combine(date.fromisoformat(p["date"]), time.fromisoformat(p["time"])) if p["time"] else None
        _new_meal(db, user, MealIn(title=p["title"], eaten_at=eaten, items=[ItemIn(**i) for i in p["items"]]))
    elif a["type"] == "set_goals":
        p["old"] = _me(db, user)["goals"]  # what it really replaced, for the card afterwards
        _set_goals(user, GoalsIn(**{k: p[k] for k in ("kcal", "protein", "fat", "carbs")}))
    elif a["type"] == "add_water":
        _add_water(db, user, date.fromisoformat(p["date"]), p["ml"])
    elif a["type"] == "log_weight":
        _upsert_weight(db, user, _weight_day(user, date.fromisoformat(p["date"])), p["kg"])
    elif a["type"] == "log_workout":
        db.add(Workout(user_id=user.id, day=date.fromisoformat(p["date"]), started=p["time"], kind=p["kind"],
                       minutes=p["minutes"], kcal=p["kcal"], distance_km=p["distance_km"], avg_hr=p["avg_hr"]))
    elif a["type"] == "log_activity":
        _upsert_activity(db, user, date.fromisoformat(p["date"]), p["active_kcal"], p["steps"])
    else:
        raise ValueError(f"unknown action {a['type']!r}")


@router.get("/coach")
def coach_chat(user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    msgs = coach.history(db, user, 100)
    return {"messages": [_msg_dict(m) for m in msgs], **_chat_state(db, user)}


MAX_COACH_IMAGE_CHARS = 2_500_000  # base64; the browser sends ~200 KB, this is the safety net
IMAGE_ONLY_TEXT = "Посмотри, пожалуйста, скриншот"
BAD_IMAGE = "Не получилось прочитать изображение"


class CoachIn(BaseModel):
    text: str = Field("", max_length=coach.MAX_MESSAGE_CHARS)
    image: str | None = Field(None, max_length=MAX_COACH_IMAGE_CHARS)  # base64 or a data: URL


def _coach_image(raw: str) -> bytes:
    """The attached image as a small JPEG; 400 if it isn't one."""
    if raw.startswith("data:"):
        head, _, raw = raw.partition(",")
        if not head.startswith("data:image/") or not head.endswith(";base64"):
            raise HTTPException(status_code=400, detail=BAD_IMAGE)
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail=BAD_IMAGE) from exc
    jpeg = shrink(data, TEXT_IMAGE_EDGE)
    del data
    release_memory()  # as after a meal photo: the container has 250 MB
    if jpeg is None:
        raise HTTPException(status_code=400, detail=BAD_IMAGE)
    return jpeg


@router.post("/coach")
def coach_say(body: CoachIn, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    text = body.text.strip()
    if not text and not body.image:
        raise HTTPException(status_code=400, detail="Пустое сообщение")
    image = _coach_image(body.image) if body.image else None
    text = text or IMAGE_ONLY_TEXT
    if coach.used_today(db, user) >= get_settings().coach_daily_messages:
        raise HTTPException(status_code=429, detail="На сегодня сообщения коучу закончились — продолжим завтра")
    if problem := usage.limit_problem(db, user):
        raise HTTPException(status_code=429, detail=problem)
    model = get_settings().coach_model
    today = _today(user)
    expenditure = _energy(db, user, today, _daily_rows(db, user, today - timedelta(days=6), today))
    try:
        reply = coach.ask(db, user, text, extra={"expenditure": expenditure}, image=image)
    except coach.CoachErrorWithResponse as exc:
        usage.record_call(db, user, exc.response, model=model, error=str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except coach.CoachError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    usage.record_call(db, user, reply.response, model=model)
    actions = _proposals(db, user, reply.calls)
    answer_text = reply.text or (coach.NO_TEXT_REPLY if actions else NO_ACTION_REPLY)
    mine = ChatMessage(user_id=user.id, role="user", content=text, has_image=image is not None)
    answer = ChatMessage(user_id=user.id, role="assistant", content=answer_text,
                         actions=json.dumps(actions, ensure_ascii=False) if actions else None)
    db.add_all([mine, answer])
    db.commit()
    return {"messages": [_msg_dict(m) for m in (mine, answer)],
            **_chat_state(db, user), "spent_today": round(usage.spent_today(db, user), 4)}


class ActionIn(BaseModel):
    accept: bool


@router.post("/coach/actions/{message_id}/{action_id}")
def coach_action(message_id: int, action_id: str, body: ActionIn,
                 user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    """Accept or decline one proposal. Only a pending one changes, so a repeat never writes twice."""
    msg = db.get(ChatMessage, message_id)
    actions = json.loads(msg.actions) if msg is not None and msg.user_id == user.id and msg.actions else []
    action = next((a for a in actions if a["id"] == action_id), None)
    if action is None:
        raise HTTPException(status_code=404, detail="Предложение не найдено")
    if action["status"] != "pending":
        return _msg_dict(msg)
    before = msg.actions
    if body.accept:
        _apply_action(db, user, action)
    action["status"] = "done" if body.accept else "declined"
    # Compare-and-set in the same transaction as the write: a parallel accept that got
    # here first leaves no row to update, and this one's write is rolled back.
    swapped = db.execute(update(ChatMessage).where(ChatMessage.id == msg.id, ChatMessage.actions == before)
                         .values(actions=json.dumps(actions, ensure_ascii=False))).rowcount
    if swapped != 1:
        db.rollback()
        return _msg_dict(db.get(ChatMessage, message_id))
    db.commit()
    return _msg_dict(db.get(ChatMessage, message_id))


@router.delete("/coach")
def coach_clear(user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    db.query(ChatMessage).filter(ChatMessage.user_id == user.id).delete()
    db.commit()
    return {"ok": True}
