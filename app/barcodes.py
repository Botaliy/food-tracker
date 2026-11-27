"""Packaged food by barcode: our own foods first, then Open Food Facts.

Open Food Facts (https://world.openfoodfacts.org) is an open database of products with
their label values per 100 g. A product found there is saved as a food (source "off"),
so the next scan, and the text search, find it without asking again.
"""

import json
import logging
import re
import urllib.error
import urllib.request

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Food, User

logger = logging.getLogger(__name__)

OFF_URL = "https://world.openfoodfacts.org/api/v2/product/{code}.json?fields=product_name,product_name_ru,brands,nutriments,quantity,product_quantity"
# Open Food Facts asks API clients to name themselves.
USER_AGENT = "food-tracker/1.0 (personal calorie diary)"
TIMEOUT = 8


class LookupError_(Exception):
    """Open Food Facts didn't answer; the user can still enter the label by hand."""


def normalize(code: str) -> str | None:
    digits = re.sub(r"\D", "", code)
    return digits if 6 <= len(digits) <= 14 else None


def _fetch(code: str) -> dict | None:
    req = urllib.request.Request(OFF_URL.format(code=code), headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise LookupError_(f"Open Food Facts: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise LookupError_(f"Open Food Facts: {exc}") from exc
    return data.get("product") if data.get("status") == 1 else None


def _num(n: dict, key: str) -> float | None:
    v = n.get(key)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


_UNITS = {"г": 1, "гр": 1, "g": 1, "gr": 1, "мл": 1, "ml": 1, "кг": 1000, "kg": 1000, "л": 1000, "l": 1000, "cl": 10, "сл": 10}
_AMOUNT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(кг|гр|г|мл|сл|л|kg|gr|g|ml|cl|l)\b", re.IGNORECASE)


def package_grams(product: dict) -> float | None:
    """The net weight of one pack, in g (1 ml counts as 1 g), or None when the label data has none.
    The printed quantity goes first: in "6 x 330 ml" the last amount is one can, while
    product_quantity is the whole multipack."""
    grams = None
    if found := _AMOUNT.findall(str(product.get("quantity") or "")):
        value, unit = found[-1]
        grams = float(value.replace(",", ".")) * _UNITS[unit.lower()]
    else:
        grams = _num(product, "product_quantity")
    return round(grams, 1) if grams is not None and 1 <= grams <= 5000 else None


def from_off(product: dict, code: str, user: User) -> Food | None:
    """A Food from an Open Food Facts product, or None without usable values per 100 g."""
    n = product.get("nutriments") or {}
    kcal = _num(n, "energy-kcal_100g")
    if kcal is None and (kj := _num(n, "energy_100g")) is not None:
        kcal = kj / 4.184
    if kcal is None or not 0 <= kcal <= 900:
        return None
    name = (product.get("product_name_ru") or product.get("product_name") or "").strip()
    brand = (product.get("brands") or "").split(",")[0].strip()
    if not name:
        return None
    full = f"{name} ({brand})" if brand and brand.lower() not in name.lower() else name
    clamp = lambda v: min(100.0, max(0.0, v or 0.0))  # noqa: E731
    # Often missing from the label data: a missing value stays unknown, not zero.
    maybe = lambda key: None if (v := _num(n, key)) is None else clamp(v)  # noqa: E731
    return Food(
        source="off", source_id=code, barcode=code, name=full[:300], name_ru=full[:300], owner_id=user.id,
        kcal=round(kcal, 1),
        protein=clamp(_num(n, "proteins_100g")), fat=clamp(_num(n, "fat_100g")), carbs=clamp(_num(n, "carbohydrates_100g")),
        fiber=maybe("fiber_100g"), sugar=maybe("sugars_100g"), salt=maybe("salt_100g"),
        package_grams=package_grams(product),
    )


def lookup(db: Session, code: str, user: User) -> Food | None:
    # Recipes never carry a barcode; the filter keeps it so if one ever does.
    known = db.scalar(select(Food).where(Food.barcode == code, Food.source != "recipe").order_by(Food.id.desc()).limit(1))
    if known is not None:
        return known
    product = _fetch(code)
    food = from_off(product, code, user) if product else None
    if food is not None:
        db.add(food)
        db.commit()
    return food
