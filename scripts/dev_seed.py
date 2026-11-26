"""Local development only: a dev admin and a handful of foods to search.

    python -m scripts.dev_seed          # user and foods
    python -m scripts.dev_seed --demo   # plus 60 days of meals and weigh-ins for the reports screen

Login: dev / devpassword (see README). Never run against the production database.
"""

import random
import sys
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from app.auth import hash_password
from app.database import SessionLocal, init_db
from app.models import Food, Meal, MealItem, User, Weight
from scripts.import_usda import BACKFILL_SQL

# name, name_ru, kcal, protein, fat, carbs, fibre, sugar, salt (per 100 g, roughly SR Legacy)
FOODS = [
    ("Chicken, broilers or fryers, breast, meat only, cooked, roasted", "Куриная грудка запечённая", 165, 31.0, 3.6, 0.0, 0.0, 0.0, 0.19),
    ("Rice, white, long-grain, regular, enriched, cooked", "Рис белый варёный", 130, 2.7, 0.3, 28.2, 0.4, 0.05, 0.0),
    ("Buckwheat groats, roasted, cooked", "Гречка варёная", 92, 3.4, 0.6, 19.9, 2.7, 0.9, 0.01),
    ("Egg, whole, cooked, hard-boiled", "Яйцо варёное", 155, 12.6, 10.6, 1.1, 0.0, 1.1, 0.31),
    ("Bread, rye", "Хлеб ржаной", 259, 8.5, 3.3, 48.3, 5.8, 3.9, 1.65),
    ("Oil, olive, salad or cooking", "Масло оливковое", 884, 0.0, 100.0, 0.0, 0.0, 0.0, 0.0),
    ("Tomatoes, red, ripe, raw", "Помидор", 18, 0.9, 0.2, 3.9, 1.2, 2.6, 0.01),
    ("Cucumber, with peel, raw", "Огурец", 15, 0.7, 0.1, 3.6, 0.5, 1.7, 0.01),
]


def main() -> None:
    init_db()
    with SessionLocal() as db:
        if not db.scalar(select(User).where(User.username == "dev")):
            db.add(User(username="dev", password_hash=hash_password("devpassword"), is_admin=True, timezone="Europe/Berlin"))
        have = {f.name: f for f in db.scalars(select(Food).where(Food.source == "usda", Food.name.in_([r[0] for r in FOODS])))}
        for n, ru, k, p, f, c, fib, sug, salt in FOODS:
            food = have.get(n)
            if food is None:
                db.add(Food(source="usda", name=n, name_ru=ru, kcal=k, protein=p, fat=f, carbs=c, fiber=fib, sugar=sug, salt=salt))
            else:  # dev rows seeded before fibre/sugar/salt existed
                food.fiber, food.sugar, food.salt = fib, sug, salt
        db.flush()
        for stmt in BACKFILL_SQL:  # meals already logged from these foods
            db.execute(text(stmt))
        db.commit()
        if "--demo" in sys.argv:
            _demo(db, db.scalar(select(User).where(User.username == "dev")))
    print("dev seed ok")


def _demo(db, user: User, days: int = 60) -> None:
    """Meals titled «Демо» and a slowly falling weight. Re-running replaces them."""
    for meal in db.scalars(select(Meal).where(Meal.user_id == user.id, Meal.title == "Демо")):
        db.delete(meal)
    db.query(Weight).filter(Weight.user_id == user.id).delete()
    rnd = random.Random(42)
    tz = ZoneInfo(user.timezone)
    today = datetime.now(tz).date()
    kg = 84.0
    for k in range(days, 0, -1):
        day = today - timedelta(days=k)
        if rnd.random() < 0.12:
            continue  # a forgotten day now and then
        for hour, kcal in ((9, 450), (14, 750), (20, 800)):
            local = datetime(day.year, day.month, day.day, hour, tzinfo=tz)
            grams = kcal * rnd.uniform(0.7, 1.35)
            db.add(Meal(user_id=user.id, title="Демо", eaten_at=local.astimezone(UTC).replace(tzinfo=None), items=[
                MealItem(name="Демо-блюдо", grams=round(grams), source="estimate",
                         kcal_100=100, protein_100=6, fat_100=3.5, carbs_100=11,
                         fiber_100=1.2, sugar_100=2.5, salt_100=0.35)]))
        kg += rnd.uniform(-0.25, 0.15)
        if rnd.random() < 0.6:
            db.add(Weight(user_id=user.id, day=day, kg=round(kg, 1)))
    db.commit()


if __name__ == "__main__":
    main()
