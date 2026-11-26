import json
import zipfile

import pytest

from app import foods, recognize as recognize_mod
from app.models import ApiCall
from scripts import import_usda
from tests.conftest import add_food, fake_response, make_user

RECOGNITION = {
    "title": "Курица с рисом",
    "comment": "Масло не видно",
    "items": [
        {"name_ru": "Куриная грудка", "name_en": "chicken breast roasted", "grams": 150,
         "kcal_100": 170, "protein_100": 30, "fat_100": 4, "carbs_100": 0, "confidence": "high"},
        {"name_ru": "Рис", "name_en": "rice white cooked", "grams": 180,
         "kcal_100": 130, "protein_100": 2.7, "fat_100": 0.3, "carbs_100": 28, "confidence": "medium"},
        {"name_ru": "Соус терияки", "name_en": "teriyaki sauce", "grams": 20,
         "kcal_100": 90, "protein_100": 6, "fat_100": 0, "carbs_100": 16, "confidence": "low"},
    ],
}


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.kwargs = None
        outer = self

        class Messages:
            def create(self, **kwargs):
                outer.kwargs = kwargs
                return outer.response

        self.beta = type("Beta", (), {"messages": Messages()})()


@pytest.fixture
def fake_model(monkeypatch):
    holder = {"client": FakeClient(fake_response(json.dumps(RECOGNITION)))}
    real = recognize_mod.recognize
    monkeypatch.setattr("app.routers.api.recognize", lambda img, note: real(img, note, client=holder["client"]))
    return holder


# ---------- food matching ----------

def test_search_and_pick_prefers_plausible_calories(db):
    skin = add_food(db, "Chicken, broilers or fryers, skin only, roasted", 454)
    breast = add_food(db, "Chicken, broilers or fryers, breast, meat only, roasted", 165, 31, 3.6)
    found = foods.search(db, "chicken breast roasted", user_id=None)
    assert breast in found and skin in found
    assert foods.pick(found, foods.Per100(170, 30, 4, 0)) == breast


def test_pick_rejects_far_off_match(db):
    add_food(db, "Butter, salted", 717)
    found = foods.search(db, "butter", user_id=None)
    assert foods.pick(found, foods.Per100(80, 1, 2, 10)) is None


def test_custom_foods_are_private(db):
    a, b = make_user(db, "anna"), make_user(db, "boris")
    add_food(db, "Бабушкин пирог", 300, source="custom", owner_id=a.id)
    assert foods.search(db, "пирог", user_id=a.id)
    assert not foods.search(db, "пирог", user_id=b.id)


# ---------- recognition ----------

def test_recognize_builds_draft_from_database(client, db, fake_model):
    breast = add_food(db, "Chicken, broilers or fryers, breast, meat only, roasted", 165, 31, 3.6, 0)
    add_food(db, "Rice, white, long-grain, regular, enriched, cooked", 130, 2.7, 0.3, 28)

    r = client.post("/api/recognize", data={"note": "обед"}, files={"photo": ("m.jpg", _jpeg(), "image/jpeg")})
    assert r.status_code == 200, r.text
    draft = r.json()
    chicken, rice, sauce = draft["items"]
    assert chicken["food_id"] == breast.id and chicken["source"] == "usda"
    assert chicken["name"] == "Куриная грудка" and chicken["grams"] == 150
    assert rice["source"] == "usda"
    assert sauce["source"] == "estimate" and sauce["kcal_100"] == 90
    assert draft["cost_usd"] == pytest.approx((2000 * 2 + 500 * 10) / 1e6)

    sent = fake_model["client"].kwargs
    assert sent["model"] == "claude-sonnet-5-5"
    assert sent["fallbacks"] == "default"
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert sent["messages"][0]["content"][0]["type"] == "image"


def test_daily_limit_blocks_further_calls(client, db, fake_model):
    # Limit is $0.05 in tests; a call above costs ~$0.009, so six calls cross it.
    for _ in range(6):
        assert client.post("/api/recognize", data={"note": "яблоко"}).status_code == 200
    r = client.post("/api/recognize", data={"note": "яблоко"})
    assert r.status_code == 429
    assert db.query(ApiCall).count() == 6


def test_refusal_is_billed_and_reported(client, db, fake_model):
    fake_model["client"].response = fake_response("", stop_reason="refusal")
    r = client.post("/api/recognize", data={"note": "что-то"})
    assert r.status_code == 502
    call = db.query(ApiCall).one()
    assert call.error and call.cost_usd > 0


def test_recognize_needs_photo_or_note(client, fake_model):
    assert client.post("/api/recognize", data={"note": "  "}).status_code == 502


# ---------- diary ----------

def test_meal_uses_database_numbers_and_sums_the_day(client, db):
    rice = add_food(db, "Rice, white, cooked", 130, 2.7, 0.3, 28)
    body = {"title": "Обед", "items": [
        # A tampered client can't change a database food's numbers.
        {"name": "Рис", "grams": 200, "food_id": rice.id, "source": "usda",
         "kcal_100": 800, "protein_100": 0, "fat_100": 0, "carbs_100": 0},
        {"name": "Соус", "grams": 50, "source": "estimate",
         "kcal_100": 100, "protein_100": 0, "fat_100": 10, "carbs_100": 2},
    ]}
    meal = client.post("/api/meals", json=body).json()
    assert meal["totals"]["kcal"] == pytest.approx(260 + 50)

    day = client.get("/api/day").json()
    assert len(day["meals"]) == 1
    assert day["totals"]["fat"] == pytest.approx(0.6 + 5)

    meal_id = meal["id"]
    body["items"] = body["items"][:1]
    assert client.put(f"/api/meals/{meal_id}", json=body).json()["totals"]["kcal"] == 260
    assert client.delete(f"/api/meals/{meal_id}").status_code == 200
    assert client.get("/api/day").json()["meals"] == []


def test_cannot_touch_someone_elses_meal(client, db):
    meal = client.post("/api/meals", json={"items": [
        {"name": "x", "grams": 10, "kcal_100": 1, "protein_100": 0, "fat_100": 0, "carbs_100": 0}]}).json()
    make_user(db, "boris")
    client.post("/api/auth/logout")
    client.post("/api/auth/login", json={"username": "boris", "password": "password1"})
    assert client.delete(f"/api/meals/{meal['id']}").status_code == 404


# ---------- reports ----------

def _eat(client, when: str, kcal_100: float, grams: float = 100):
    r = client.post("/api/meals", json={"eaten_at": when, "items": [
        {"name": "x", "grams": grams, "kcal_100": kcal_100, "protein_100": 10, "fat_100": 0, "carbs_100": 0}]})
    assert r.status_code == 200, r.text


def _local_today():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Europe/Berlin")).date()


def test_stats_group_days_in_user_timezone(client):
    from datetime import timedelta
    today = _local_today()
    d1, d2 = today - timedelta(days=1), today - timedelta(days=3)
    _eat(client, f"{d1}T08:00:00", 500)
    _eat(client, f"{d1}T23:30:00", 850, grams=200)  # late evening is still that local day
    _eat(client, f"{d2}T12:00:00", 750, grams=200)

    s = client.get("/api/stats?days=7").json()
    assert len(s["days"]) == 7 and s["to"] == today.isoformat()
    by_date = {d["date"]: d for d in s["days"]}
    assert by_date[d1.isoformat()]["kcal"] == 2200 and by_date[d1.isoformat()]["meals"] == 2
    assert by_date[today.isoformat()]["kcal"] == 0
    assert s["logged_days"] == 2
    assert s["avg"]["kcal"] == pytest.approx((2200 + 1500) / 2)
    assert s["in_goal_days"] == 1  # 2200 is over the 2000 goal, 1500 is within
    assert s["streak"] == 1  # today is empty, so the run ends yesterday


def test_streak_counts_today_once_logged(client):
    from datetime import timedelta
    today = _local_today()
    for k in range(3):
        _eat(client, f"{today - timedelta(days=k)}T12:00:00", 100)
    _eat(client, f"{today - timedelta(days=5)}T12:00:00", 100)
    assert client.get("/api/stats?days=7").json()["streak"] == 3


def test_weight_is_one_per_day_and_private(client, db):
    from datetime import timedelta
    today = _local_today()
    assert client.put("/api/weight", json={"kg": 80.5}).status_code == 200
    assert client.put("/api/weight", json={"kg": 80.1}).json() == {"date": today.isoformat(), "kg": 80.1}
    client.put("/api/weight", json={"kg": 81.0, "date": (today - timedelta(days=10)).isoformat()})
    assert client.put("/api/weight", json={"kg": 80, "date": (today + timedelta(days=1)).isoformat()}).status_code == 400

    s = client.get("/api/stats?days=7").json()
    assert s["weights"] == [{"date": today.isoformat(), "kg": 80.1}]
    assert s["last_weight"]["kg"] == 80.1
    assert len(client.get("/api/stats?days=30").json()["weights"]) == 2

    assert client.delete("/api/weight").status_code == 200
    assert client.get("/api/stats?days=7").json()["last_weight"]["kg"] == 81.0

    make_user(db, "boris")
    client.post("/api/auth/logout")
    client.post("/api/auth/login", json={"username": "boris", "password": "password1"})
    assert client.get("/api/stats?days=30").json()["last_weight"] is None


def test_recent_meals_groups_repeats_by_title_and_products(client, db):
    def meal(title, *names, grams=100):
        return client.post("/api/meals", json={"title": title, "items": [
            {"name": n, "grams": grams, "kcal_100": 100, "protein_100": 0, "fat_100": 0, "carbs_100": 0} for n in names]}).json()

    meal("Завтрак", "Овсянка", "Банан")
    meal("завтрак ", "Банан", "Овсянка", grams=150)  # same meal: case, spaces and order don't matter
    meal("Завтрак", "Яйцо")
    meal("Обед", "Суп")

    recent = client.get("/api/meals/recent").json()
    assert [(m["title"], m["count"]) for m in recent][0] == ("завтрак", 2)
    assert recent[0]["items"][0]["grams"] == 150  # the newest copy is the template
    assert len(recent) == 3

    make_user(db, "boris")
    client.post("/api/auth/logout")
    client.post("/api/auth/login", json={"username": "boris", "password": "password1"})
    assert client.get("/api/meals/recent").json() == []


# ---------- weekly review ----------

REVIEW = {"summary": "Неделя ровная.", "good": ["Белок в норме"], "improve": ["Вечером 50% калорий"], "tips": ["Перенеси часть ужина на обед"]}


@pytest.fixture
def fake_reviewer(monkeypatch):
    from app import review as review_mod
    fake = FakeClient(fake_response(json.dumps(REVIEW)))
    fake.calls = 0
    real = review_mod.review

    def counted(digest):
        fake.calls += 1
        return real(digest, client=fake)

    monkeypatch.setattr(review_mod, "review", counted)
    return fake


def test_review_needs_three_logged_days(client, db, fake_reviewer):
    from datetime import timedelta
    today = _local_today()
    for k in range(2):
        _eat(client, f"{today - timedelta(days=k)}T12:00:00", 100)
    r = client.post("/api/review")
    assert r.status_code == 400
    assert fake_reviewer.calls == 0 and db.query(ApiCall).count() == 0


def test_review_is_made_once_a_day_from_aggregates(client, db, fake_reviewer):
    from datetime import timedelta
    today = _local_today()
    assert client.get("/api/review").json() == {"review": None, "fresh": False}
    for k in (0, 1, 3):
        _eat(client, f"{today - timedelta(days=k)}T20:00:00", 500)
    client.put("/api/weight", json={"kg": 80})

    r = client.post("/api/review")
    assert r.status_code == 200, r.text
    assert r.json() == {"review": {"date": today.isoformat(), **REVIEW}, "fresh": True}
    assert db.query(ApiCall).count() == 1

    sent = fake_reviewer.kwargs["messages"][0]["content"]
    digest = json.loads(sent.split("\n", 1)[1])
    assert "admin" not in sent  # no username goes to the model
    assert len(digest["days"]) == 7 and digest["days"][2] == {"date": (today - timedelta(days=4)).isoformat(),
                                                             "weekday": digest["days"][2]["weekday"], "logged": False}
    assert digest["days"][-1]["kcal"] == 500
    assert digest["kcal_share_by_part_of_day_percent"] == {"вечер": 100}
    assert digest["top_foods_by_kcal"] == [{"name": "x", "kcal_week": 1500, "times": 3}]
    assert digest["weigh_ins_kg"] == [{"date": today.isoformat(), "kg": 80.0}]

    # Same day again: the stored review, no second call.
    assert client.post("/api/review").json()["review"]["summary"] == REVIEW["summary"]
    assert fake_reviewer.calls == 1
    assert client.get("/api/review").json()["fresh"] is True


def test_review_respects_daily_limit(client, db, fake_reviewer):
    from datetime import timedelta
    from app.models import ApiCall as Call
    today = _local_today()
    for k in range(3):
        _eat(client, f"{today - timedelta(days=k)}T12:00:00", 100)
    db.add(Call(user_id=client.get("/api/me").json()["id"], model="claude-opus-5-5", cost_usd=1.0))
    db.commit()
    assert client.post("/api/review").status_code == 429
    assert fake_reviewer.calls == 0


# ---------- goals, Apple Health, energy ----------

def test_formula_and_goals():
    from app import energy
    # 30-year-old woman, 165 cm, 65 kg: 10*65 + 6.25*165 - 5*30 - 161 = 1370.25
    assert energy.bmr("f", 65, 165, 30) == pytest.approx(1370.25)
    g = energy.goals_for(2000, "f", 65, -0.5)
    assert g["kcal"] == 1450 and g["protein"] == 130 and not g["capped"]
    assert g["protein"] * 4 + g["fat"] * 9 + g["carbs"] * 4 == pytest.approx(g["kcal"], abs=10)
    # Too steep a deficit is held at the safe floor.
    assert energy.goals_for(1500, "f", 55, -0.75)["kcal"] == 1200
    assert energy.goals_for(1500, "f", 55, -0.75)["capped"]


def test_adaptive_expenditure():
    from datetime import date, timedelta
    from app import energy
    d0 = date(2026, 11, 1)
    days = [energy.DayIntake(d0 + timedelta(days=k), 2000, k % 7 != 6) for k in range(28)]
    # Losing exactly 0.5 kg a week on 2000 kcal: spends 2000 + 550 = 2550.
    weights = [(d0 + timedelta(days=k), 80 - 0.5 * k / 7) for k in range(0, 28, 3)]
    out = energy.adaptive(days, weights)
    assert out["ready"] and out["tdee"] == 2550 and out["kg_per_week"] == -0.5
    assert not energy.adaptive(days[:5], weights)["ready"]
    assert not energy.adaptive(days, weights[:1])["ready"]


def test_profile_gives_formula_goals(client):
    p = client.get("/api/profile").json()
    assert p["formula"] is None and p["health"] == {"connected": False, "last_day": None}
    r = client.put("/api/profile", json={"sex": "m", "birth_year": 1990, "height_cm": 180, "weight_kg": 85,
                                         "activity": 1.55, "rate_kg_week": -0.5})
    assert r.status_code == 200, r.text
    f = r.json()["formula"]
    assert f["tdee"] > f["bmr"] > 1500 and f["suggested"]["kcal"] < f["tdee"]
    assert client.get("/api/stats").json()["last_weight"]["kg"] == 85  # the form's weight is a weigh-in
    bad = client.put("/api/profile", json={"sex": "m", "birth_year": 1990, "height_cm": 180, "activity": 3, "rate_kg_week": 0})
    assert bad.status_code == 400


def test_health_shortcut_sends_activity(client, db):
    from datetime import timedelta
    today = _local_today()
    token = client.post("/api/profile/health-token").json()["token"]
    anon = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(client.app)

    assert anon.post("/api/health", json={"steps": 100}).status_code == 401
    assert anon.post("/api/health", json={"steps": 100}, headers={"Authorization": "Bearer nope"}).status_code == 401
    h = {"Authorization": f"Bearer {token}"}
    assert anon.post("/api/health", json={"active_kcal": 512.37, "steps": 8400}, headers=h).json() == {"ok": True, "date": today.isoformat()}
    anon.post("/api/health", json={"steps": 9100}, headers=h)  # a later resend keeps the kcal
    assert client.get("/api/day").json()["activity"] == {"active_kcal": 512.4, "steps": 9100}
    old = (today - timedelta(days=40)).isoformat()
    assert anon.post("/api/health", json={"date": old, "steps": 1}, headers=h).status_code == 400

    s = client.get("/api/stats").json()
    assert s["days"][-1]["steps"] == 9100 and s["energy"]["activity"]["days"] == 1
    assert client.get("/api/profile").json()["health"] == {"connected": True, "last_day": today.isoformat()}

    # A new key replaces the old one.
    client.post("/api/profile/health-token")
    assert anon.post("/api/health", json={"steps": 1}, headers=h).status_code == 401


def test_stats_adaptive_needs_data_then_suggests(client):
    from datetime import timedelta
    today = _local_today()
    s = client.get("/api/stats").json()["energy"]["adaptive"]
    assert not s["ready"] and s["logged_days"] == 0
    for k in range(1, 15):
        _eat(client, f"{today - timedelta(days=k)}T12:00:00", 500, grams=400)  # 2000 kcal a day
    for k in (14, 7, 1):
        client.put("/api/weight", json={"kg": 80 - (14 - k) / 14, "date": (today - timedelta(days=k)).isoformat()})
    ad = client.get("/api/stats").json()["energy"]["adaptive"]
    assert ad["ready"] and ad["avg_intake"] == 2000
    assert ad["tdee"] == 2550 and ad["suggested"]["kcal"] == 2550  # no profile: keep weight


# ---------- invites ----------

def test_invite_flow(client):
    code = client.post("/api/admin/invites").json()["code"]
    client.post("/api/auth/logout")
    r = client.post("/api/auth/register", json={"invite": code, "username": "Vera", "password": "longpass1", "timezone": "Asia/Nicosia"})
    assert r.status_code == 200
    assert r.json()["username"] == "vera" and r.json()["timezone"] == "Asia/Nicosia"
    assert not r.json()["is_admin"]
    assert client.get("/api/admin/users").status_code == 403
    client.post("/api/auth/logout")
    again = client.post("/api/auth/register", json={"invite": code, "username": "other", "password": "longpass1"})
    assert again.status_code == 400


def test_api_requires_login():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        assert c.get("/api/day").status_code == 401
        assert c.get("/").status_code == 200


# ---------- USDA import ----------

def test_import_usda(tmp_path, db):
    path = tmp_path / "sr.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("sr/food.csv", 'fdc_id,data_type,description\n1,sr_legacy_food,"Egg, whole, raw"\n2,sr_legacy_food,"Water"\n')
        zf.writestr("sr/food_nutrient.csv",
                    "id,fdc_id,nutrient_id,amount\n"
                    "1,1,1008,143\n2,1,1003,12.6\n3,1,1004,9.5\n4,1,1005,0.7\n5,2,1008,0\n")
    import_usda.main([str(path)])
    found = foods.search(db, "egg raw", user_id=None)
    assert len(found) == 1 and found[0].kcal == 143 and found[0].protein == 12.6


def _jpeg() -> bytes:
    import io
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (2400, 1800), "orange").save(buf, format="JPEG")
    return buf.getvalue()
