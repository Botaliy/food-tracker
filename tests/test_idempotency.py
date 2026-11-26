"""Idempotency-Key on the quick writes the offline queue retries."""

from datetime import timedelta

from fastapi.testclient import TestClient

from app import idempotency
from app.main import app
from app.models import IdempotencyKey, Meal, Water, Weight, utcnow
from tests.conftest import make_user
from tests.test_app import _local_today

KEY = "q-0123456789abcdef"
MEAL = {"title": "Каша", "eaten_at": "2026-01-05T08:30:00",
        "items": [{"name": "Овсянка", "grams": 250, "kcal_100": 90, "protein_100": 3, "fat_100": 2, "carbs_100": 15}]}


def other_client(db, name="bob"):
    make_user(db, name)
    c = TestClient(app)
    c.__enter__()
    assert c.post("/api/auth/login", json={"username": name, "password": "password1"}).status_code == 200
    return c


def test_meal_with_same_key_is_written_once(client, db):
    first = client.post("/api/meals", json=MEAL, headers={"Idempotency-Key": KEY})
    again = client.post("/api/meals", json={**MEAL, "title": "Другое"}, headers={"Idempotency-Key": KEY})
    assert first.status_code == again.status_code == 200
    assert again.json() == first.json()  # the meal created the first time, same shape
    assert db.query(Meal).count() == 1
    # The time it was entered, not the time it reached the server.
    assert first.json()["eaten_at"].startswith("2026-01-05T08:30")
    # Without a key it behaves as before.
    client.post("/api/meals", json=MEAL)
    client.post("/api/meals", json=MEAL)
    assert db.query(Meal).count() == 3


def test_water_and_weight_with_same_key(client, db):
    h = {"Idempotency-Key": KEY + "w"}
    assert client.post("/api/water", json={"delta_ml": 250}, headers=h).json()["ml"] == 250
    client.post("/api/water", json={"delta_ml": 250})  # another write in between
    replay = client.post("/api/water", json={"delta_ml": 250}, headers=h)
    assert replay.status_code == 200 and replay.json() == {"ml": 500, "goal": 2000}

    h = {"Idempotency-Key": KEY + "kg"}
    assert client.put("/api/weight", json={"kg": 70.5}, headers=h).json() == {"date": _local_today().isoformat(), "kg": 70.5}
    client.put("/api/weight", json={"kg": 71})
    replay = client.put("/api/weight", json={"kg": 70.5}, headers=h)
    assert replay.status_code == 200 and replay.json()["kg"] == 71  # the current state, not a second write
    assert db.query(Weight).count() == 1 and db.query(IdempotencyKey).count() == 2


def test_keys_are_per_user(client, db):
    client.post("/api/meals", json=MEAL, headers={"Idempotency-Key": KEY})
    bob = other_client(db)
    r = bob.post("/api/meals", json=MEAL, headers={"Idempotency-Key": KEY})
    assert r.status_code == 200
    assert bob.post("/api/water", json={"delta_ml": 300}, headers={"Idempotency-Key": KEY + "w"}).json()["ml"] == 300
    assert db.query(Meal).count() == 2 and db.query(Water).count() == 1
    bob.__exit__(None, None, None)


def test_malformed_key_is_rejected(client, db):
    for bad in ("short", "x" * 65, "has spaces in it!!", "dots.are.not.allowed"):
        assert client.post("/api/water", json={"delta_ml": 250}, headers={"Idempotency-Key": bad}).status_code == 400
    assert client.post("/api/meals", json=MEAL, headers={"Idempotency-Key": "a/b"}).status_code == 400
    assert client.put("/api/weight", json={"kg": 70}, headers={"Idempotency-Key": ""}).status_code == 400
    assert db.query(Water).count() == 0 and db.query(Meal).count() == 0


def test_replay_of_a_deleted_meal_does_not_recreate_it(client, db):
    meal = client.post("/api/meals", json=MEAL, headers={"Idempotency-Key": KEY}).json()
    client.delete(f"/api/meals/{meal['id']}")
    assert client.post("/api/meals", json=MEAL, headers={"Idempotency-Key": KEY}).status_code == 404
    assert db.query(Meal).count() == 0


def test_old_keys_are_swept_once_a_day(client, db, monkeypatch):
    client.post("/api/water", json={"delta_ml": 250}, headers={"Idempotency-Key": KEY})
    old = db.query(IdempotencyKey).one()
    old.created_at = utcnow() - timedelta(days=idempotency.KEEP_DAYS + 1)
    db.commit()
    monkeypatch.setattr(idempotency, "_last_sweep", 0.0)
    client.post("/api/water", json={"delta_ml": 250}, headers={"Idempotency-Key": KEY + "2"})
    db.expire_all()
    assert [k.key for k in db.query(IdempotencyKey)] == [KEY + "2"]
    # The old key is gone, so it would apply again: that's the 14-day contract.
    assert client.post("/api/water", json={"delta_ml": 250}, headers={"Idempotency-Key": KEY}).json()["ml"] == 750


def test_sweep_failure_never_breaks_the_write(client, db, monkeypatch):
    monkeypatch.setattr(idempotency, "_last_sweep", 0.0)

    def boom(*a):
        raise RuntimeError("disk")
    monkeypatch.setattr(idempotency, "delete", boom)
    r = client.post("/api/water", json={"delta_ml": 250}, headers={"Idempotency-Key": KEY})
    assert r.status_code == 200 and r.json()["ml"] == 250
