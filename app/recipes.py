"""Own dishes: a food whose per-100 g values come from its raw ingredients.

A pot of borscht is weighed once it is cooked; what the ingredients add up to is
spread over that weight, so 350 g of it carry 350/cooked of the whole pot.
"""

from app.foods import EXTRA

MACROS = ("kcal", "protein", "fat", "carbs")


class RecipeError(ValueError):
    """A message for the user, in Russian."""


def per100(items, cooked_grams: float | None = None) -> dict[str, float | None]:
    """Per-100 g of the finished dish from items with .grams and .<key>_100.

    Without a cooked weight the dish weighs what its ingredients do. Fibre, sugar and
    salt count known values only and are None when no ingredient has the value."""
    raw = sum(i.grams for i in items)
    weight = cooked_grams or raw
    if weight <= 0:
        raise RecipeError("В блюде нет продуктов")
    out: dict[str, float | None] = {}
    for key in MACROS:
        out[key] = sum(getattr(i, f"{key}_100") * i.grams / 100 for i in items) / weight * 100
    for key in EXTRA:
        known = [getattr(i, f"{key}_100") * i.grams / 100 for i in items if getattr(i, f"{key}_100") is not None]
        out[key] = sum(known) / weight * 100 if known else None
    return out


def check_plausible(values: dict[str, float | None]) -> None:
    """A cooked weight far below the ingredients' gives more than 100 g of fat in 100 g:
    a typo, not a dish."""
    if values["kcal"] > 900 or any((values[k] or 0) > 100 for k in ("protein", "fat", "carbs", *EXTRA)):
        raise RecipeError("Вес готового блюда слишком мал для этих продуктов — проверь его")
