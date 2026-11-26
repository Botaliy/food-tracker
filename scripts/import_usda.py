"""Load USDA FoodData Central foods (per 100 g) into the foods table.

Usage:
    python -m scripts.import_usda path/to/FoodData_Central_sr_legacy_food_csv_*.zip [more.zip ...]

Works with the "SR Legacy" (~7.8k generic foods) and "Foundation" CSV zips from
https://fdc.nal.usda.gov/download-datasets. Reads the zip in place, streaming
the nutrient table, so memory stays small. Re-running replaces earlier USDA rows.
"""

import csv
import io
import sys
import zipfile

from sqlalchemy import delete

from app.database import SessionLocal, init_db
from app.models import Food

# FoodData Central nutrient ids.
ENERGY_KCAL = ("1008", "2047", "2048")  # SR Legacy, then Foundation's Atwater variants
PROTEIN, FAT, CARBS = "1003", "1004", "1005"
WANTED = {*ENERGY_KCAL, PROTEIN, FAT, CARBS}


def _member(zf: zipfile.ZipFile, name: str) -> str:
    for n in zf.namelist():
        if n.endswith("/" + name) or n == name:
            return n
    raise SystemExit(f"{name} not found in {zf.filename}")


def _rows(zf: zipfile.ZipFile, name: str):
    with zf.open(_member(zf, name)) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8"))


def load(path: str) -> list[Food]:
    with zipfile.ZipFile(path) as zf:
        names = {r["fdc_id"]: r["description"] for r in _rows(zf, "food.csv")}
        values: dict[str, dict[str, float]] = {}
        for r in _rows(zf, "food_nutrient.csv"):
            nid = r["nutrient_id"]
            if nid in WANTED and r["fdc_id"] in names and r["amount"]:
                values.setdefault(r["fdc_id"], {}).setdefault(nid, float(r["amount"]))

    foods = []
    for fdc_id, v in values.items():
        kcal = next((v[k] for k in ENERGY_KCAL if k in v), None)
        if kcal is None or not all(k in v for k in (PROTEIN, FAT)):
            continue
        foods.append(Food(
            source="usda", source_id=fdc_id, name=names[fdc_id][:300],
            kcal=kcal, protein=v[PROTEIN], fat=v[FAT], carbs=v.get(CARBS, 0.0),
        ))
    return foods


def main(paths: list[str]) -> None:
    if not paths:
        raise SystemExit(__doc__)
    init_db()
    foods = [f for p in paths for f in load(p)]
    with SessionLocal() as db:
        db.execute(delete(Food).where(Food.source == "usda"))
        db.add_all(foods)
        db.commit()
    print(f"Imported {len(foods)} USDA foods")


if __name__ == "__main__":
    main(sys.argv[1:])
