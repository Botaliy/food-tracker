"""Load USDA FoodData Central foods (per 100 g) into the foods table.

Usage:
    python -m scripts.import_usda path/to/FoodData_Central_sr_legacy_food_csv_*.zip [more.zip ...]

Works with the "SR Legacy" (~7.8k generic foods) and "Foundation" CSV zips from
https://fdc.nal.usda.gov/download-datasets. Reads the zip in place, streaming
the nutrient table, so memory stays small. Re-running updates earlier USDA rows in
place by their FDC id, so food ids (and the meals pointing at them) survive.
"""

import csv
import io
import sys
import zipfile
from dataclasses import dataclass

from sqlalchemy import select, text

from app.database import SessionLocal, init_db
from app.models import Food

# FoodData Central nutrient ids (nutrient.csv: id, name, unit_name).
ENERGY_KCAL = ("1008", "2047", "2048")  # SR Legacy, then Foundation's Atwater variants
PROTEIN, FAT, CARBS = "1003", "1004", "1005"
FIBER = "1079"                  # Fiber, total dietary, G
SUGARS = ("2000", "1063")       # Sugars, total including NLEA (SR Legacy); Sugars, Total (Foundation), G
SODIUM = "1093"                 # Sodium, Na, MG
WANTED = {*ENERGY_KCAL, PROTEIN, FAT, CARBS, FIBER, *SUGARS, SODIUM}
SALT_PER_SODIUM = 2.5


@dataclass
class Row:
    fdc_id: str
    name: str
    kcal: float
    protein: float
    fat: float
    carbs: float
    fiber: float | None
    sugar: float | None
    salt: float | None


def _member(zf: zipfile.ZipFile, name: str) -> str:
    for n in zf.namelist():
        if n.endswith("/" + name) or n == name:
            return n
    raise SystemExit(f"{name} not found in {zf.filename}")


def _rows(zf: zipfile.ZipFile, name: str):
    with zf.open(_member(zf, name)) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8"))


def load(path: str) -> list[Row]:
    with zipfile.ZipFile(path) as zf:
        names = {r["fdc_id"]: r["description"] for r in _rows(zf, "food.csv")}
        values: dict[str, dict[str, float]] = {}
        for r in _rows(zf, "food_nutrient.csv"):
            nid = r["nutrient_id"]
            if nid in WANTED and r["fdc_id"] in names and r["amount"]:
                values.setdefault(r["fdc_id"], {}).setdefault(nid, float(r["amount"]))

    rows = []
    for fdc_id, v in values.items():
        kcal = next((v[k] for k in ENERGY_KCAL if k in v), None)
        if kcal is None or not all(k in v for k in (PROTEIN, FAT)):
            continue
        sugar = next((v[k] for k in SUGARS if k in v), None)
        rows.append(Row(
            fdc_id=fdc_id, name=names[fdc_id][:300],
            kcal=kcal, protein=v[PROTEIN], fat=v[FAT], carbs=v.get(CARBS, 0.0),
            fiber=v.get(FIBER), sugar=sugar,
            salt=round(v[SODIUM] * SALT_PER_SODIUM / 1000, 3) if SODIUM in v else None,
        ))
    return rows


# Meals logged before these values existed get them from their food; values already
# on an item (copied in when it was eaten) are never overwritten.
BACKFILL_SQL = [
    f"""UPDATE meal_items SET {col}_100 = (SELECT foods.{col} FROM foods WHERE foods.id = meal_items.food_id)
        WHERE food_id IS NOT NULL AND {col}_100 IS NULL"""
    for col in ("fiber", "sugar", "salt")
]


def save(db, rows: list[Row]) -> tuple[int, int]:
    """Update USDA foods in place by FDC id, insert new ones. (updated, added)."""
    existing = {f.source_id: f for f in db.scalars(select(Food).where(Food.source == "usda", Food.source_id.is_not(None)))}
    updated = added = 0
    for r in rows:
        food = existing.get(r.fdc_id)
        if food is None:
            food = Food(source="usda", source_id=r.fdc_id)
            db.add(food)
            existing[r.fdc_id] = food
            added += 1
        else:
            updated += 1
        # name_ru (a translation someone added) is left as it is.
        food.name, food.kcal, food.protein, food.fat, food.carbs = r.name, r.kcal, r.protein, r.fat, r.carbs
        food.fiber, food.sugar, food.salt = r.fiber, r.sugar, r.salt
    db.flush()
    for stmt in BACKFILL_SQL:
        db.execute(text(stmt))
    db.commit()
    return updated, added


def main(paths: list[str]) -> None:
    if not paths:
        raise SystemExit(__doc__)
    init_db()
    rows = [r for p in paths for r in load(p)]
    with SessionLocal() as db:
        updated, added = save(db, rows)
    print(f"USDA foods: {added} added, {updated} updated")


if __name__ == "__main__":
    main(sys.argv[1:])
