"""A recognition turned into an editable draft: database numbers where they fit."""

from sqlalchemy.orm import Session

from app import foods
from app.recognize import Recognition, RecognizedItem


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
    return {
        **base,
        # What's on the plate, in the user's words, even when the database row is named in English.
        "name": item.name_ru,
        "grams": round(item.grams),
        "ai_grams": round(item.grams),
        "confidence": item.confidence,
        "options": options,
    }


def build_draft(db: Session, recognition: Recognition, user_id: int) -> dict:
    return {
        "title": recognition.title,
        "comment": recognition.comment,
        "items": [draft_item(db, item, user_id) for item in recognition.items],
    }
