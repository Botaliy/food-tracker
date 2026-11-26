"""Energy maths: a starting goal from a formula, and the real expenditure from the diary.

The formula (Mifflin-St Jeor) is a population average and can be off by a few hundred
kcal for a given person. The adaptive estimate is what the diary says instead: if you
ate X a day on average and your weight moved by Y, you spent X - Y * 7700 / days.
"""

from dataclasses import dataclass
from datetime import date

KCAL_PER_KG = 7700  # energy in a kilogram of body weight change, the usual estimate

# Multiplier on BMR, with what it means in words for the settings form.
ACTIVITY_LEVELS = {
    1.2: "Почти не двигаюсь",
    1.375: "Лёгкая активность: прогулки, 1–3 тренировки в неделю",
    1.55: "Средняя: 3–5 тренировок в неделю",
    1.725: "Высокая: тренировки почти каждый день",
}
# kg per week; negative = losing.
RATES = {-0.75: "Худеть быстро, −0,75 кг/нед", -0.5: "Худеть, −0,5 кг/нед", -0.25: "Худеть мягко, −0,25 кг/нед",
         0.0: "Держать вес", 0.25: "Набирать, +0,25 кг/нед"}

# Below this the diet is too strict to recommend without a doctor.
MIN_KCAL = {"f": 1200, "m": 1500}

# The adaptive estimate needs enough data to beat day-to-day water swings.
ADAPTIVE_WINDOW_DAYS = 28
MIN_LOGGED_DAYS = 10
MIN_WEIGH_IN_SPAN_DAYS = 10


def bmr(sex: str, weight_kg: float, height_cm: float, age: int) -> float:
    return 10 * weight_kg + 6.25 * height_cm - 5 * age + (5 if sex == "m" else -161)


def round10(x: float) -> int:
    return int(round(x / 10) * 10)


def goals_for(tdee: float, sex: str, weight_kg: float, rate_kg_week: float) -> dict:
    """Daily kcal and macros for a given expenditure and pace."""
    kcal = tdee + rate_kg_week * KCAL_PER_KG / 7
    floor = MIN_KCAL.get(sex, 1200)
    capped = kcal < floor
    kcal = round10(max(kcal, floor))
    # More protein while losing keeps muscle; fat at least a quarter of the energy.
    protein = round((2.0 if rate_kg_week < 0 else 1.6) * weight_kg)
    fat = round(max(0.8 * weight_kg, 0.25 * kcal / 9))
    carbs = max(0, round((kcal - 4 * protein - 9 * fat) / 4))
    return {"kcal": kcal, "protein": protein, "fat": fat, "carbs": carbs, "capped": capped}


def formula(sex: str, weight_kg: float, height_cm: float, age: int, activity: float) -> dict:
    b = bmr(sex, weight_kg, height_cm, age)
    return {"bmr": round10(b), "tdee": round10(b * activity)}


@dataclass
class DayIntake:
    day: date
    kcal: float
    logged: bool


def weight_slope(weights: list[tuple[date, float]]) -> float:
    """Least-squares trend in kg per day: one noisy weigh-in doesn't swing it."""
    t0 = weights[0][0]
    xs = [(d - t0).days for d, _ in weights]
    ys = [kg for _, kg in weights]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    var = sum((x - mx) ** 2 for x in xs)
    return 0.0 if var == 0 else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var


def adaptive(days: list[DayIntake], weights: list[tuple[date, float]]) -> dict:
    """Expenditure from what was eaten and how weight moved. `ready` is False until there's enough data."""
    logged = [d for d in days if d.logged]
    span = (weights[-1][0] - weights[0][0]).days if len(weights) >= 2 else 0
    out = {
        "logged_days": len(logged),
        "weigh_ins": len(weights),
        "weigh_in_span_days": span,
        "need_logged_days": MIN_LOGGED_DAYS,
        "need_span_days": MIN_WEIGH_IN_SPAN_DAYS,
        "ready": len(logged) >= MIN_LOGGED_DAYS and span >= MIN_WEIGH_IN_SPAN_DAYS,
    }
    if not out["ready"]:
        return out
    intake = sum(d.kcal for d in logged) / len(logged)
    slope = weight_slope(weights)
    out.update({
        "avg_intake": round(intake),
        "kg_per_week": round(slope * 7, 2),
        "tdee": round10(intake - slope * KCAL_PER_KG),
    })
    return out
