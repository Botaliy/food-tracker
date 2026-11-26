"""Local development only: a dev admin and a handful of foods to search.

    python -m scripts.dev_seed

Login: dev / devpassword (see README). Never run against the production database.
"""

from sqlalchemy import select

from app.auth import hash_password
from app.database import SessionLocal, init_db
from app.models import Food, User

FOODS = [
    ("Chicken, broilers or fryers, breast, meat only, cooked, roasted", "Куриная грудка запечённая", 165, 31.0, 3.6, 0.0),
    ("Rice, white, long-grain, regular, enriched, cooked", "Рис белый варёный", 130, 2.7, 0.3, 28.2),
    ("Buckwheat groats, roasted, cooked", "Гречка варёная", 92, 3.4, 0.6, 19.9),
    ("Egg, whole, cooked, hard-boiled", "Яйцо варёное", 155, 12.6, 10.6, 1.1),
    ("Bread, rye", "Хлеб ржаной", 259, 8.5, 3.3, 48.3),
    ("Oil, olive, salad or cooking", "Масло оливковое", 884, 0.0, 100.0, 0.0),
    ("Tomatoes, red, ripe, raw", "Помидор", 18, 0.9, 0.2, 3.9),
    ("Cucumber, with peel, raw", "Огурец", 15, 0.7, 0.1, 3.6),
]


def main() -> None:
    init_db()
    with SessionLocal() as db:
        if not db.scalar(select(User).where(User.username == "dev")):
            db.add(User(username="dev", password_hash=hash_password("devpassword"), is_admin=True, timezone="Europe/Berlin"))
        if not db.scalar(select(Food).where(Food.source == "usda").limit(1)):
            db.add_all(Food(source="usda", name=n, name_ru=ru, kcal=k, protein=p, fat=f, carbs=c) for n, ru, k, p, f, c in FOODS)
        db.commit()
    print("dev seed ok")


if __name__ == "__main__":
    main()
