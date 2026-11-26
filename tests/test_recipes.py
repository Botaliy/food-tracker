import json
import shutil
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import foods, photos, recipes, recognize as recognize_mod
from app.main import app
from app.models import ApiCall, Food, MealItem, RecipeItem
from tests.conftest import add_food, fake_response, make_user
from tests.test_app import FakeClient, _jpeg


def ing(grams, kcal, protein=0.0, fat=0.0, carbs=0.0, fiber=None, sugar=None, salt=None):
    return SimpleNamespace(grams=grams, kcal_100=kcal, protein_100=protein, fat_100=fat, carbs_100=carbs,
                           fiber_100=fiber, sugar_100=sugar, salt_100=salt)


# ---------- arithmetic ----------

def test_per100_spreads_ingredients_over_cooked_weight():
    rice = ing(200, 360, 7, 1, 79, fiber=1.3)
    oil = ing(10, 900, 0, 100, 0)
    # Rice triples: 210 g raw becomes 600 g cooked, so 100 g of it is lighter than raw.
    v = recipes.per100([rice, oil], cooked_grams=600)
    assert v["kcal"] == pytest.approx((720 + 90) / 600 * 100)
    assert v["fat"] == pytest.approx((2 + 10) / 600 * 100)
    assert v["fiber"] == pytest.approx(2.6 / 600 * 100)
    assert v["sugar"] is None and v["salt"] is None  # no ingredient knows them: unknown, not zero

    # Without a cooked weight the dish weighs what its ingredients do.
    assert recipes.per100([rice, oil])["kcal"] == pytest.approx(810 / 210 * 100)


def test_per100_cooked_smaller_than_raw():
    beef = ing(1000, 200, 20, 13, 0, salt=0.2)
    v = recipes.per100([beef], cooked_grams=700)  # roasting loses water
    assert v["kcal"] == pytest.approx(2000 / 700 * 100)
    assert v["salt"] == pytest.approx(2 / 700 * 100)
    recipes.check_plausible(v)
    with pytest.raises(recipes.RecipeError):
        recipes.check_plausible(recipes.per100([ing(100, 900, 0, 100, 0)], cooked_grams=50))


# ---------- API ----------

def _dish(name="Борщ", items=None, cooked=3000, portion=350):
    return {"name": name, "cooked_grams": cooked, "portion_grams": portion, "items": items or [
        {"name": "Свёкла", "grams": 1000, "kcal_100": 43, "protein_100": 1.6, "fat_100": 0.2, "carbs_100": 9.6},
        {"name": "Говядина", "grams": 500, "kcal_100": 250, "protein_100": 26, "fat_100": 15, "carbs_100": 0},
    ]}


def _other_client(db):
    make_user(db, "boris")
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": "boris", "password": "password1"}).status_code == 200
    return c


def test_recipe_crud_and_privacy(client, db):
    r = client.post("/api/recipes", json=_dish())
    assert r.status_code == 200, r.text
    dish = r.json()
    kcal = (430 + 1250) / 3000 * 100
    assert dish["kcal_100"] == round(kcal, 1) and dish["source"] == "recipe"
    assert dish["ingredient_count"] == 2 and dish["portion_kcal"] == round(kcal * 3.5)
    assert dish["fiber_100"] is None and len(dish["items"]) == 2

    listed = client.get("/api/recipes").json()
    assert [d["id"] for d in listed] == [dish["id"]] and "items" not in listed[0]
    assert client.get(f"/api/recipes/{dish['id']}").json()["items"][0]["name"] == "Свёкла"
    # The owner finds it in the normal search, with its usual portion.
    found = client.get("/api/foods/search", params={"q": "борщ"}).json()
    assert found[0]["food_id"] == dish["id"] and found[0]["portion_grams"] == 350

    r = client.put(f"/api/recipes/{dish['id']}", json=_dish("Борщ зелёный", cooked=None))
    assert r.status_code == 200
    assert r.json()["kcal_100"] == round((430 + 1250) / 1500 * 100, 1)
    assert db.query(RecipeItem).count() == 2  # replaced, not appended

    other = _other_client(db)
    with other:
        for method in ("get", "delete"):
            assert getattr(other, method)(f"/api/recipes/{dish['id']}").status_code == 404
        assert other.put(f"/api/recipes/{dish['id']}", json=_dish()).status_code == 404
        assert other.get("/api/recipes").json() == []
        assert other.get("/api/foods/search", params={"q": "борщ"}).json() == []
        # Nor can they log it by id: the link is dropped and their own numbers stay.
        meal = other.post("/api/meals", json={"items": [{"name": "Борщ", "grams": 100, "food_id": dish["id"],
                                                         "kcal_100": 10, "protein_100": 1, "fat_100": 1, "carbs_100": 1}]}).json()
        assert meal["items"][0]["food_id"] is None and meal["items"][0]["kcal_100"] == 10
    assert foods.search(db, "борщ", user_id=None) == []

    # Not a recipe: the same id space, still 404.
    rice = add_food(db, "Rice", 130)
    assert client.get(f"/api/recipes/{rice.id}").status_code == 404


def test_meal_from_dish_keeps_numbers_after_edit_and_delete(client, db):
    dish = client.post("/api/recipes", json=_dish()).json()
    item = {"name": dish["name"], "grams": 350, "food_id": dish["id"], "source": "recipe",
            "kcal_100": 0, "protein_100": 0, "fat_100": 0, "carbs_100": 0}
    meal = client.post("/api/meals", json={"title": "Обед", "items": [item]}).json()
    logged = meal["items"][0]
    assert logged["food_id"] == dish["id"] and logged["source"] == "recipe"
    assert logged["kcal_100"] == pytest.approx(dish["kcal_100"], abs=0.05)  # from the database, not the client
    before = meal["totals"]["kcal"]

    # Twice the beef: the dish changes, the meal already eaten does not.
    heavier = _dish()
    heavier["items"][1]["grams"] = 1000
    assert client.put(f"/api/recipes/{dish['id']}", json=heavier).json()["kcal_100"] > dish["kcal_100"]
    day = client.get("/api/day").json()
    assert day["meals"][0]["totals"]["kcal"] == before
    # The dish logged last comes first in the list.
    second = client.post("/api/recipes", json=_dish("Плов")).json()
    assert [d["id"] for d in client.get("/api/recipes").json()] == [dish["id"], second["id"]]

    assert client.delete(f"/api/recipes/{dish['id']}").json() == {"ok": True}
    db.expire_all()
    assert db.get(Food, dish["id"]) is None
    assert db.query(RecipeItem).filter(RecipeItem.food_id == dish["id"]).count() == 0
    day = client.get("/api/day").json()
    assert day["meals"][0]["totals"]["kcal"] == before and day["meals"][0]["items"][0]["food_id"] is None
    assert db.query(MealItem).count() == 1


def test_recipe_ingredient_uses_database_numbers(client, db):
    beet = add_food(db, "Beets, raw", 43, 1.6, 0.2, 9.6)
    beet.fiber = 2.8
    db.commit()
    body = _dish(items=[{"name": "Свёкла", "grams": 1000, "food_id": beet.id,
                         "kcal_100": 499, "protein_100": 0, "fat_100": 0, "carbs_100": 0}], cooked=None)
    dish = client.post("/api/recipes", json=body).json()
    assert dish["kcal_100"] == 43 and dish["fiber_100"] == 2.8
    assert dish["items"][0]["food_id"] == beet.id and dish["items"][0]["source"] == "usda"
    # A dish can't be its own ingredient.
    body["items"][0]["food_id"] = dish["id"]
    assert client.put(f"/api/recipes/{dish['id']}", json=body).status_code == 400


def test_recipe_validation(client):
    one = _dish()["items"][0]
    assert client.post("/api/recipes", json={**_dish(), "items": []}).status_code == 422
    assert client.post("/api/recipes", json={**_dish(), "items": [one] * 41}).status_code == 422
    assert client.post("/api/recipes", json={**_dish(cooked=None), "items": [one] * 40}).status_code == 200
    assert client.post("/api/recipes", json={**_dish(), "items": [{**one, "grams": 0}]}).status_code == 422
    assert client.post("/api/recipes", json={**_dish(), "items": [{**one, "grams": 20000}]}).status_code == 422
    assert client.post("/api/recipes", json={**_dish(), "cooked_grams": 0}).status_code == 422
    assert client.post("/api/recipes", json={**_dish(), "portion_grams": 6000}).status_code == 422
    assert client.post("/api/recipes", json={**_dish(), "name": "  "}).status_code == 400
    # 1.5 kg of food can't weigh 100 g cooked.
    r = client.post("/api/recipes", json={**_dish(), "cooked_grams": 100})
    assert r.status_code == 400 and "Вес готового блюда" in r.json()["detail"]


# ---------- AI draft ----------

RECIPE = {
    "title": "Плов",
    "comment": "Масло добавил: плов без него не готовят",
    "cooked_grams": 1400,
    "items": [
        {"name_ru": "Рис", "name_en": "rice white long-grain raw", "grams": 400, "kcal_100": 365, "protein_100": 7,
         "fat_100": 0.7, "carbs_100": 80, "fiber_100": 1.3, "sugar_100": 0.1, "salt_100": 0, "confidence": "high"},
        {"name_ru": "Масло подсолнечное", "name_en": "oil sunflower", "grams": 30, "kcal_100": 884, "protein_100": 0,
         "fat_100": 100, "carbs_100": 0, "fiber_100": 0, "sugar_100": 0, "salt_100": 0, "confidence": "medium"},
    ],
}


@pytest.fixture
def fake_recipe_model(monkeypatch):
    fake = FakeClient(fake_response(json.dumps(RECIPE)))
    monkeypatch.setattr(recognize_mod, "_client", lambda: fake)
    return fake


def test_recipe_draft_matches_and_logs_usage(client, db, fake_recipe_model):
    rice = add_food(db, "Rice, white, long-grain, regular, raw, enriched", 365, 7.1, 0.7, 80)
    shutil.rmtree(photos.folder(), ignore_errors=True)
    r = client.post("/api/recipes/draft", data={"note": "плов: 2 стакана риса"},
                    files={"photo": ("r.jpg", _jpeg(), "image/jpeg")})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["title"] == "Плов" and d["cooked_grams"] == 1400 and "Масло" in d["comment"]
    assert d["items"][0]["food_id"] == rice.id and d["items"][0]["grams"] == 400
    assert d["items"][1]["source"] == "estimate"
    assert d["cost_usd"] > 0 and "photo" not in d

    sent = fake_recipe_model.kwargs
    assert sent["system"] == recognize_mod.RECIPE_SYSTEM
    assert sent["output_config"]["format"]["schema"] == recognize_mod.RECIPE_SCHEMA
    assert sent["messages"][0]["content"][0]["type"] == "image"
    assert "2 стакана риса" in sent["messages"][0]["content"][-1]["text"]
    assert db.query(ApiCall).count() == 1
    assert not photos.folder().exists() or not any(photos.folder().iterdir())  # no thumbnail for a recipe


def test_recipe_draft_limits_and_errors(client, db, fake_recipe_model):
    assert client.post("/api/recipes/draft", data={"note": " "}).status_code == 502
    assert db.query(ApiCall).count() == 0  # nothing was sent

    fake_recipe_model.response = fake_response("", stop_reason="refusal")
    assert client.post("/api/recipes/draft", data={"note": "борщ"}).status_code == 502
    assert db.query(ApiCall).one().error

    # $0.05 a day in tests: the next calls cross it, then the endpoint refuses before calling.
    fake_recipe_model.response = fake_response(json.dumps(RECIPE))
    while client.post("/api/recipes/draft", data={"note": "борщ"}).status_code == 200:
        assert db.query(ApiCall).count() < 20
    calls = db.query(ApiCall).count()
    r = client.post("/api/recipes/draft", data={"note": "борщ"})
    assert r.status_code == 429 and "лимит" in r.json()["detail"]
    assert db.query(ApiCall).count() == calls
