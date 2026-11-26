import os
import tempfile
from types import SimpleNamespace

# Point the app at a throwaway database before anything imports it.
_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ["ANTHROPIC_API_KEY"] = "test"
os.environ["DEFAULT_DAILY_LIMIT_USD"] = "0.05"
os.environ["SCHEDULER_ENABLED"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete  # noqa: E402

from app.auth import hash_password  # noqa: E402
from app.database import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Activity, ApiCall, ChatMessage, Food, Invite, Meal, Profile, PushSubscription, Review, User, Water, Weight  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    init_db()
    with SessionLocal() as db:
        for model in (ApiCall, Review, ChatMessage, Activity, PushSubscription, Water, Profile, Weight, Meal, Invite, Food, User):
            db.execute(delete(model))
        db.commit()
    yield


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session


def make_user(db, username="admin", password="password1", is_admin=False) -> User:
    user = User(username=username, password_hash=hash_password(password), is_admin=is_admin, timezone="Europe/Berlin")
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def admin(db):
    return make_user(db, "admin", is_admin=True)


@pytest.fixture
def client(admin):
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": "admin", "password": "password1"})
        assert r.status_code == 200
        yield c


def fake_response(text: str, stop_reason="end_turn", input_tokens=2000, output_tokens=500):
    return SimpleNamespace(
        model="claude-sonnet-5-5",
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                              cache_creation_input_tokens=0, cache_read_input_tokens=0),
    )


def add_food(db, name, kcal, protein=0.0, fat=0.0, carbs=0.0, source="usda", owner_id=None) -> Food:
    food = Food(source=source, name=name, kcal=kcal, protein=protein, fat=fat, carbs=carbs, owner_id=owner_id)
    db.add(food)
    db.commit()
    return food
