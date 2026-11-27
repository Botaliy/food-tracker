"""A recognition turned into an editable draft: database numbers where they fit."""

from sqlalchemy.orm import Session

from app import foods
from app.recognize import RecipeRecognition, Recognition, RecognizedItem


def _estimate(item: RecognizedItem) -> dict:
    return {
        "food_id": None,
        "source": "estimate",
        "name": item.name_ru,
        "name_en": item.name_en,
        "kcal_100": round(item.kcal_100, 1),
        "protein_100": round(item.protein_100, 1),
        "fat_100": round(item.fat_100, 1),
        "carbs_100": round(item.carbs_100, 1),
        "fiber_100": None if item.fiber_100 is None else round(item.fiber_100, 1),
        "sugar_100": None if item.sugar_100 is None else round(item.sugar_100, 1),
        "salt_100": None if item.salt_100 is None else round(item.salt_100, 2),
        "package_grams": None,
    }


def draft_item(db: Session, item: RecognizedItem, user_id: int) -> dict:
    seen: dict[int, object] = {}
    for phrase in (item.name_en, item.name_ru):
        for food in foods.search(db, phrase, user_id):
            seen.setdefault(food.id, food)
    candidates = list(seen.values())
    guess = foods.Per100(item.kcal_100, item.protein_100, item.fat_100, item.carbs_100)
    chosen = foods.pick(candidates, guess)

    estimate = _estimate(item)
    options = [estimate] + [foods.food_dict(f) for f in candidates]
    base = foods.food_dict(chosen) if chosen is not None else estimate
    # Like the macros, a database value wins; unlike them, the database often has none,
    # and then the model's estimate is better than nothing.
    base = {**base, **{f"{k}_100": estimate[f"{k}_100"] for k in foods.EXTRA if base[f"{k}_100"] is None}}
    return {
        **base,
        # What's on the plate, in the user's words, even when the database row is named in English.
        "name": item.name_ru,
        "grams": round(item.grams),
        "ai_grams": round(item.grams),
        # The pack in the photo, read off its label, over the database's: the same drink comes in several sizes.
        "package_grams": round(item.package_grams) if item.package_grams else base["package_grams"],
        "confidence": item.confidence,
        "options": options,
    }


def build_recipe_draft(db: Session, recipe: RecipeRecognition, user_id: int) -> dict:
    """The same matching as a meal's, for a dish's raw ingredients."""
    return {
        "title": recipe.title,
        "comment": recipe.comment,
        "cooked_grams": round(recipe.cooked_grams) if recipe.cooked_grams else None,
        "items": [draft_item(db, item, user_id) for item in recipe.items],
    }


def build_draft(db: Session, recognition: Recognition, user_id: int, photo: str | None = None) -> dict:
    return {
        "title": recognition.title,
        "photo": photo,
        "comment": recognition.comment,
        "items": [draft_item(db, item, user_id) for item in recognition.items],
    }
