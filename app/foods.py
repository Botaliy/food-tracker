"""Looking foods up in the local database (USDA, Open Food Facts, custom).

The model names what's on the plate and guesses its nutrition; the numbers
we keep come from here whenever a match is plausible. The model's own guess
picks between close text matches and stands in when nothing fits.
"""

import re
from dataclasses import dataclass

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from app.models import Food

CANDIDATES = 6
# A match whose calories are this far off the model's guess is probably the wrong
# food (raw vs. cooked, "chicken skin" for "chicken"), so the guess wins.
MAX_KCAL_RATIO = 2.0

_WORD = re.compile(r"\w+", re.UNICODE)
# Words USDA names are full of that say little about the food itself.
_STOP = {"and", "or", "with", "without", "of", "the", "a", "in", "food", "dish", "prepared"}


@dataclass
class Per100:
    kcal: float
    protein: float
    fat: float
    carbs: float


def fts_query(phrase: str, op: str = "OR") -> str | None:
    """'grilled chicken breast' -> '"grilled"* OR "chicken"* OR "breast"*'."""
    words = [w.lower() for w in _WORD.findall(phrase) if w.lower() not in _STOP and len(w) > 1]
    if not words:
        return None
    return f" {op} ".join(f'"{w}"*' for w in dict.fromkeys(words))


def _match(db: Session, query: str, n: int) -> list[int]:
    return list(db.execute(
        text("SELECT rowid FROM foods_fts WHERE foods_fts MATCH :q ORDER BY bm25(foods_fts, 1.0, 1.0) LIMIT :n"),
        {"q": query, "n": n},
    ).scalars())


def search(db: Session, phrase: str, user_id: int | None, limit: int = CANDIDATES) -> list[Food]:
    """Foods best matching a phrase (English or Russian), the user's own first."""
    any_word = fts_query(phrase)
    if any_word is None:
        return []
    # Names holding every word first: bm25 alone favours short names that match
    # fewer words ("Chicken breast, roll" over "Chicken, ..., breast, meat only, roasted").
    rows = _match(db, fts_query(phrase, "AND"), limit * 5)
    if len(rows) < limit:
        rows += [r for r in _match(db, any_word, limit * 5) if r not in rows]
    if not rows:
        return []
    foods = {
        f.id: f
        for f in db.scalars(
            select(Food).where(Food.id.in_(rows), or_(Food.owner_id.is_(None), Food.owner_id == user_id))
        )
    }
    ranked = [foods[r] for r in rows if r in foods]
    # The user's own foods are what they actually eat: put them first.
    ranked.sort(key=lambda f: 0 if f.owner_id == user_id and user_id is not None else 1)
    return ranked[:limit]


def pick(candidates: list[Food], guess: Per100) -> Food | None:
    """The best candidate: high in the text ranking and close to the model's calorie guess."""
    best, best_score = None, None
    for rank, food in enumerate(candidates):
        if guess.kcal > 0 and food.kcal > 0:
            ratio = max(food.kcal, guess.kcal) / min(food.kcal, guess.kcal)
        elif guess.kcal == food.kcal:
            ratio = 1.0
        else:
            ratio = float("inf")
        if ratio > MAX_KCAL_RATIO:
            continue
        # 10% off the guess weighs as much as one place down the text ranking.
        score = rank + 10 * (ratio - 1)
        if best_score is None or score < best_score:
            best, best_score = food, score
    return best


def per100(food: Food) -> Per100:
    return Per100(food.kcal, food.protein, food.fat, food.carbs)


def food_dict(food: Food) -> dict:
    return {
        "food_id": food.id,
        "source": food.source,
        "name": food.name_ru or food.name,
        "name_en": food.name,
        "kcal_100": round(food.kcal, 1),
        "protein_100": round(food.protein, 1),
        "fat_100": round(food.fat, 1),
        "carbs_100": round(food.carbs, 1),
    }
