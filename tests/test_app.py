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
    monkeypatch.setattr("app.routers.api.recognize", lambda img, note, hints=(): real(img, note, hints, client=holder["client"]))
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


def test_added_foods_are_shared_with_own_first(db):
    a, b = make_user(db, "anna"), make_user(db, "boris")
    generic = add_food(db, "Pie, apple", 240)
    anna = add_food(db, "Бабушкин пирог pie", 300, source="custom", owner_id=a.id)
    boris = add_food(db, "Пирог с капустой pie", 200, source="custom", owner_id=b.id)
    assert foods.search(db, "pie", user_id=b.id) == [boris, anna, generic]
    assert foods.search(db, "pie", user_id=a.id)[0] == anna


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
    assert s["in_goal_days"] == 0  # 2200 is over the 2000 goal, 1500 is under the 85% floor
    assert s["streak"] == 2  # today is still empty; the gap 2 days ago is bridged by the weekly freeze


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
    assert s["weights"] == [{"date": today.isoformat(), "kg": 80.1, "trend": 80.1}]
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


# ---------- barcodes ----------

OFF_PRODUCT = {"product_name": "Йогурт греческий 2%", "brands": "Fage, Total",
               "nutriments": {"energy-kcal_100g": 73, "proteins_100g": "9.9", "fat_100g": 2, "carbohydrates_100g": 3.9}}


def test_barcode_found_in_off_is_saved(client, db, monkeypatch):
    from app import barcodes
    from app.models import Food
    calls = []
    monkeypatch.setattr(barcodes, "_fetch", lambda code: calls.append(code) or OFF_PRODUCT)
    r = client.get("/api/foods/barcode/5201054017401")
    assert r.status_code == 200, r.text
    food = r.json()
    assert food["name"] == "Йогурт греческий 2% (Fage)" and food["source"] == "off" and food["protein_100"] == 9.9
    assert client.get("/api/foods/barcode/5201054017401").json()["food_id"] == food["food_id"]
    assert calls == ["5201054017401"]  # the second scan didn't ask again
    assert [f["food_id"] for f in client.get("/api/foods/search?q=йогурт").json()] == [food["food_id"]]
    assert db.query(Food).filter_by(barcode="5201054017401").count() == 1


def test_unknown_barcode_then_label_by_hand(client, db, monkeypatch):
    from app import barcodes
    monkeypatch.setattr(barcodes, "_fetch", lambda code: None)
    assert client.get("/api/foods/barcode/4600000000000").status_code == 404
    assert client.get("/api/foods/barcode/12ab").status_code == 400
    client.post("/api/foods", json={"name": "Сырок глазированный", "kcal_100": 410, "protein_100": 8,
                                    "fat_100": 25, "carbs_100": 38, "barcode": "4600000000000"})
    make_user(db, "boris")  # someone else scans the same pack
    client.post("/api/auth/logout")
    client.post("/api/auth/login", json={"username": "boris", "password": "password1"})
    assert client.get("/api/foods/barcode/4600000000000").json()["name"] == "Сырок глазированный"

    def down(code):
        raise barcodes.LookupError_("timeout")
    monkeypatch.setattr(barcodes, "_fetch", down)
    assert client.get("/api/foods/barcode/4600000000001").status_code == 503


def test_off_product_without_numbers_is_skipped():
    from app import barcodes
    from app.models import User
    u = User(id=1)
    assert barcodes.from_off({"product_name": "x", "nutriments": {}}, "1", u) is None
    assert barcodes.from_off({"product_name": "", "nutriments": {"energy-kcal_100g": 50}}, "1", u) is None
    kj = barcodes.from_off({"product_name": "Сок", "nutriments": {"energy_100g": 184.1}}, "1", u)
    assert kj.kcal == 44.0


# ---------- learning from corrections ----------

def _ai_meal(client, *items):
    """A saved recognized meal: (name, grams kept, grams the model suggested or None if added by hand)."""
    r = client.post("/api/meals", json={"items": [
        {"name": n, "grams": g, "ai_grams": ai, "kcal_100": 100, "protein_100": 0, "fat_100": 0, "carbs_100": 0}
        for n, g, ai in items]})
    assert r.status_code == 200, r.text
    return r.json()


def test_corrections_become_hints(client, db):
    from app import corrections
    from app.models import User
    user = db.query(User).filter_by(username="admin").one()
    for _ in range(3):
        _ai_meal(client, ("Рис варёный", 200, 150), ("Курица", 120, 120), ("Масло оливковое", 10, None))
    _ai_meal(client, ("Рис варёный", 160, 160))
    # Not recognized at all: an added item here is no correction.
    client.post("/api/meals", json={"items": [{"name": "Сыр", "grams": 30, "kcal_100": 350, "protein_100": 25, "fat_100": 27, "carbs_100": 0}]})

    h = corrections.hints(db, user)
    assert len(h) == 2
    assert "Рис варёный" in h[0] and "×1.33" in h[0] and "4 раз" in h[0]
    assert "Масло оливковое" in h[1] and "~10 г" in h[1]
    assert client.get("/api/day").json()["meals"][0]["items"][0]["ai_grams"] == 150


def test_recognize_sends_hints_and_marks_ai_grams(client, db, fake_model):
    for _ in range(3):
        _ai_meal(client, ("Рис", 220, 150))
    r = client.post("/api/recognize", data={"note": "обед"})
    draft = r.json()
    assert draft["personal_hints"] == 1
    assert all(i["ai_grams"] == i["grams"] for i in draft["items"])
    text = fake_model["client"].kwargs["messages"][0]["content"][-1]["text"]
    assert "Прошлые правки" in text and "«Рис»" in text


# ---------- reminders & shared streaks ----------

SUB = {"endpoint": "https://web.push.apple.com/abc", "keys": {"p256dh": "BPk", "auth": "xyz"}, "expirationTime": None}


def _at_local(day, hour):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime(day.year, day.month, day.day, hour, 5, tzinfo=ZoneInfo("Europe/Berlin"))


def test_push_subscription_moves_between_accounts(client, db):
    from app.models import PushSubscription
    assert client.post("/api/push/subscribe", json=SUB).status_code == 200
    assert client.post("/api/push/subscribe", json=SUB).status_code == 200  # same device twice: one row
    assert db.query(PushSubscription).count() == 1
    assert client.post("/api/push/subscribe", json={**SUB, "endpoint": "http://evil"}).status_code == 422
    assert client.get("/api/profile").json()["notify"]["devices"] == 1
    client.post("/api/push/unsubscribe", json={"endpoint": SUB["endpoint"]})
    assert db.query(PushSubscription).count() == 0


def test_evening_reminder_once_and_only_when_empty(client, db):
    from datetime import timedelta
    from app import reminders
    from app.models import Profile
    today = _local_today()
    for k in (1, 2, 3):
        _eat(client, f"{today - timedelta(days=k)}T12:00:00", 100)
    client.post("/api/push/subscribe", json=SUB)
    sent = []
    fake_send = lambda db_, user, title, body, url="/": sent.append((user.username, title, body)) or 1

    assert reminders.run_due(db, _at_local(today, 20), fake_send) == []  # before 21:00
    done = reminders.run_due(db, _at_local(today, 21), fake_send)
    assert [k for _, k in done] == ["evening"]
    assert "Серия 3" in sent[0][1]
    assert reminders.run_due(db, _at_local(today, 22), fake_send) == []  # not twice a day

    # Once the day is logged there's nothing to remind about.
    _eat(client, f"{today}T12:00:00", 100)
    db.expire_all()
    db.get(Profile, client.get("/api/me").json()["id"]).last_evening_push = None
    db.commit()
    assert reminders.run_due(db, _at_local(today, 21), fake_send) == []


def test_weekly_review_push_on_monday(client, db, fake_reviewer, monkeypatch):
    from datetime import timedelta
    from app import reminders
    today = _local_today()
    monday = today - timedelta(days=today.weekday())
    for k in range(1, 5):
        _eat(client, f"{monday - timedelta(days=k)}T12:00:00", 100)
    client.post("/api/push/subscribe", json=SUB)
    client.put("/api/profile/notify", json={"evening": False, "hour": 21, "weekly": True, "share_streak": False})
    sent = []
    fake_send = lambda db_, user, title, body, url="/": sent.append((title, body, url)) or 1

    from app import diary
    monkeypatch.setattr(diary, "today", lambda user: monday)  # the review is made "on Monday"
    assert reminders.run_due(db, _at_local(monday, 9), fake_send) == []
    assert [k for _, k in reminders.run_due(db, _at_local(monday, 10), fake_send)] == ["weekly"]
    assert reminders.run_due(db, _at_local(monday, 11), fake_send) == []
    assert sent == [("✨ Обзор недели готов", REVIEW["summary"], "/#reports")]
    assert fake_reviewer.calls == 1


def test_shared_streaks_are_opt_in(client, db):
    from datetime import timedelta
    from fastapi.testclient import TestClient
    from app import main as app_main
    today = _local_today()
    _eat(client, f"{today - timedelta(days=1)}T12:00:00", 100)
    assert client.get("/api/streaks").json() == {"joined": False, "people": []}
    notify = {"evening": True, "hour": 21, "weekly": True, "share_streak": True}
    client.put("/api/profile/notify", json=notify)
    make_user(db, "alina")
    make_user(db, "boris")  # doesn't share: not listed
    with TestClient(app_main.app) as other:
        other.post("/api/auth/login", json={"username": "alina", "password": "password1"})
        other.put("/api/profile/notify", json=notify)
        other.post("/api/meals", json={"items": [{"name": "x", "grams": 10, "kcal_100": 1, "protein_100": 0, "fat_100": 0, "carbs_100": 0}]})
        people = other.get("/api/streaks").json()["people"]
    assert people == [  # longest first; on a tie, you first
        {"name": "alina", "streak": 1, "today": True, "me": True},
        {"name": "admin", "streak": 1, "today": False, "me": False},
    ]


def test_push_send_drops_gone_devices(client, db, monkeypatch):
    from types import SimpleNamespace
    from app import push
    from app.config import get_settings
    from app.models import PushSubscription, User
    from pywebpush import WebPushException
    private, public = push.generate_keys()
    s = get_settings()
    monkeypatch.setattr(s, "vapid_private_key", private)
    monkeypatch.setattr(s, "vapid_public_key", public)
    monkeypatch.setattr(s, "vapid_subject", "mailto:test@example.com")
    client.post("/api/push/subscribe", json=SUB)
    client.post("/api/push/subscribe", json={**SUB, "endpoint": "https://fcm.googleapis.com/gone"})
    calls = []

    def fake_webpush(subscription_info, data, **kw):
        calls.append((subscription_info["endpoint"], json.loads(data), kw["vapid_claims"]))
        if "gone" in subscription_info["endpoint"]:
            raise WebPushException("gone", response=SimpleNamespace(status_code=410))

    monkeypatch.setattr(push, "webpush", fake_webpush)
    user = db.query(User).filter_by(username="admin").one()
    assert push.send(db, user, "Заголовок", "Текст") == 1
    assert calls[0][1] == {"title": "Заголовок", "body": "Текст", "url": "/"}
    assert calls[0][2] == {"sub": "mailto:test@example.com"}
    assert [p.endpoint for p in db.query(PushSubscription)] == [SUB["endpoint"]]
    assert client.get("/api/push/key").json() == {"public_key": public}


def test_new_model_columns_are_added_to_old_tables(tmp_path):
    from sqlalchemy import create_engine, text
    from app import database
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE profiles (user_id INTEGER PRIMARY KEY, activity FLOAT)"))
        conn.execute(text("INSERT INTO profiles (user_id, activity) VALUES (1, 1.2)"))
        orig = database.engine
        database.engine = engine
        try:
            database._add_missing_columns(conn)
        finally:
            database.engine = orig
        row = conn.execute(text("SELECT remind_evening, remind_hour, share_streak, sex FROM profiles")).one()
    assert tuple(row) == (1, 21, 0, None)


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


def test_onboarding_flag(client):
    assert client.get("/api/me").json()["onboarded"] is False
    assert client.post("/api/profile/onboarded").json()["onboarded"] is True
    assert client.get("/api/me").json()["onboarded"] is True


def test_streak_runs_freeze_and_quality():
    from datetime import date, timedelta
    from app import diary
    on = date(2026, 11, 26)
    d = lambda k: on - timedelta(days=k)  # noqa: E731

    # One gap is frozen; a second gap within the week ends the run.
    logged = {d(k) for k in (0, 1, 2, 4, 5, 6, 8, 9)}
    n, frozen = diary.run(lambda x: x in logged, on, diary.FREEZE_EVERY_DAYS)
    assert (n, frozen) == (6, [d(3)])
    # Two empty days in a row: no freeze.
    logged = {d(k) for k in (0, 3, 4)}
    assert diary.run(lambda x: x in logged, on, diary.FREEZE_EVERY_DAYS) == (1, [])
    # Today still empty: the run counts up to yesterday, and an empty yesterday can be frozen.
    logged = {d(k) for k in (2, 3)}
    assert diary.run(lambda x: x in logged, on, diary.FREEZE_EVERY_DAYS) == (2, [d(1)])
    # Freezes a week apart are both allowed.
    logged = {d(k) for k in range(20)} - {d(2), d(9)}
    assert diary.run(lambda x: x in logged, on, diary.FREEZE_EVERY_DAYS)[1] == [d(2), d(9)]

    assert diary.in_goal(1800, 2000) and not diary.in_goal(1500, 2000) and not diary.in_goal(2200, 2000)


def test_day_shows_streaks(client):
    from datetime import timedelta
    today = _local_today()
    for k in (1, 2, 4):
        _eat(client, f"{today - timedelta(days=k)}T12:00:00", 475, grams=400)  # 1900 kcal, 40 g protein
    st = client.get("/api/day").json()["streaks"]
    assert st["days"] == 3 and st["today"] is False and st["frozen"] == (today - timedelta(days=3)).isoformat()
    assert st["freeze_ready"] is False and st["next_milestone"] == 7
    assert st["in_goal"] == 2 and st["protein"] == 0  # 40 g of 120 isn't enough


def test_evening_text_names_partners():
    from app import reminders
    title, body = reminders.evening_text(12, False, ["alina"])
    assert "под угрозой" in title and "alina уже записал" in body
    assert "заморозка" in reminders.evening_text(12, True, [])[1]


def test_weight_trend_and_goal_eta():
    from datetime import date, timedelta
    from app import energy
    t0 = date(2026, 11, 1)
    # Losing 0.5 kg a week, with a 1 kg water spike on one day.
    weights = [(t0 + timedelta(days=k), 80 - 0.5 * k / 7 + (1.0 if k == 20 else 0)) for k in range(0, 26, 2)]
    trend = energy.weight_trend(weights)
    by_day = dict(trend)
    raw_jump = weights[10][1] - weights[9][1]  # day 20 vs day 18: about +0.86 kg
    trend_jump = by_day[t0 + timedelta(days=20)] - by_day[t0 + timedelta(days=18)]
    assert raw_jump > 0.8 and trend_jump < 0.25  # the spike barely moves the trend
    today = t0 + timedelta(days=25)
    out = energy.weight_outlook(weights, 75, today)
    assert -0.6 < out["kg_per_week"] < -0.4
    eta = date.fromisoformat(out["eta"])
    left = out["trend"] - 75
    assert 3 < left < 5 and 6 * 7 < (eta - today).days < 12 * 7  # ~4 kg to go at ~0.5 kg a week
    assert energy.weight_outlook(weights, 85, today)["eta"] is None  # moving away from it
    assert energy.weight_outlook([(today, 75.1)], 75, today)["eta"] == "reached"


def test_day_has_weight_outlook(client):
    from datetime import timedelta
    today = _local_today()
    assert client.get("/api/day").json()["weight"] is None
    client.put("/api/profile", json={"sex": "f", "birth_year": 1995, "height_cm": 168, "activity": 1.375,
                                     "rate_kg_week": -0.5, "goal_weight_kg": 60})
    for k in (14, 7, 0):
        client.put("/api/weight", json={"kg": 65 - (14 - k) / 14, "date": (today - timedelta(days=k)).isoformat()})
    w = client.get("/api/day").json()["weight"]
    assert w["today"] == 64.0 and w["goal"] == 60 and w["kg_per_week"] == -0.5 and w["eta"]
    assert client.get("/api/profile").json()["goal_weight_kg"] == 60


def test_observations_from_two_weeks(client):
    from datetime import timedelta
    today = _local_today()
    for k in range(1, 8):
        d = today - timedelta(days=k)
        client.post("/api/meals", json={"eaten_at": f"{d}T08:00:00", "items": [
            {"name": "Тост", "grams": 100, "kcal_100": 300, "protein_100": 8, "fat_100": 5, "carbs_100": 50}]})
        client.post("/api/meals", json={"eaten_at": f"{d}T20:00:00", "items": [
            {"name": "Пицца", "grams": 400, "kcal_100": 260, "protein_100": 11, "fat_100": 10, "carbs_100": 30}]})
    found = client.get("/api/insights").json()
    icons = [f["icon"] for f in found]
    assert "🌙" in icons and "🍳" in icons and "🔎" in icons
    assert "«Пицца»" in found[icons.index("🔎")]["text"]


def test_suggestions_fit_what_is_left():
    from app import insights
    meal = lambda t, k, p: {"title": t, "totals": {"kcal": k, "protein": p}}  # noqa: E731
    usual = [meal("Пицца", 900, 30), meal("Творог", 300, 35), meal("Яблоко", 80, 0), meal("Омлет", 350, 20)]
    picks = [m["title"] for m in insights.suggestions(400, 40, usual)]
    assert picks[0] == "Творог" and "Пицца" not in picks
    assert insights.suggestions(100, 10, usual) == []


def test_water_glasses_and_switch(client):
    assert client.get("/api/day").json()["water"] == {"ml": 0, "goal": 2000}
    client.post("/api/water", json={"delta_ml": 250})
    assert client.post("/api/water", json={"delta_ml": 250}).json()["ml"] == 500
    assert client.post("/api/water", json={"delta_ml": -1000}).json()["ml"] == 0
    client.put("/api/profile/water", json={"track_water": False, "water_goal_ml": 2500})
    assert client.get("/api/day").json()["water"] is None
