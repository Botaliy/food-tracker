"""Packaged foods: the pack's weight from the label, the barcode database or the user."""

import json

import pytest

from app import barcodes
from tests.conftest import add_food, fake_response
from tests.test_app import RECOGNITION, FakeClient, fake_model  # noqa: F401  (the fixture)


@pytest.mark.parametrize("product, grams", [
    ({"quantity": "0,5 л"}, 500),
    ({"quantity": "330 ml"}, 330),
    ({"quantity": "6 x 330 ml", "product_quantity": 1980}, 330),  # one can, not the multipack
    ({"quantity": "1.5 kg"}, 1500),
    ({"quantity": "45 г"}, 45),
    ({"quantity": "", "product_quantity": "450"}, 450),
    ({"quantity": "12 шт"}, None),
    ({"quantity": "25 kg"}, None),  # a sack is not a helping
    ({}, None),
])
def test_package_grams_from_off(product, grams):
    assert barcodes.package_grams(product) == grams


def test_scanned_pack_is_kept_and_last_helping_returned(client, monkeypatch):
    beer = {"product_name": "Пиво светлое", "quantity": "0,45 л",
            "nutriments": {"energy-kcal_100g": 42, "proteins_100g": 0.5, "fat_100g": 0, "carbohydrates_100g": 3.4}}
    monkeypatch.setattr(barcodes, "_fetch", lambda code: beer)
    food = client.get("/api/foods/barcode/4600000000017").json()
    assert food["package_grams"] == 450 and food["last_grams"] is None
    assert client.get("/api/foods/search?q=пиво").json()[0]["package_grams"] == 450

    item = {"name": food["name"], "grams": 225, "food_id": food["food_id"], "source": "off",
            "kcal_100": 42, "protein_100": 0.5, "fat_100": 0, "carbs_100": 3.4}
    assert client.post("/api/meals", json={"title": "", "items": [item]}).status_code == 200
    assert client.get("/api/foods/barcode/4600000000017").json()["last_grams"] == 225


def test_pack_told_by_hand(client, db):
    r = client.post("/api/foods", json={"name": "Батончик", "kcal_100": 400, "protein_100": 20, "fat_100": 15,
                                        "carbs_100": 40, "package_grams": 50})
    bar = r.json()
    assert bar["package_grams"] == 50
    assert client.put(f"/api/foods/{bar['food_id']}/package", json={"grams": 60}).json()["package_grams"] == 60
    assert client.put(f"/api/foods/{bar['food_id']}/package", json={"grams": None}).json()["package_grams"] is None
    assert client.put(f"/api/foods/{bar['food_id']}/package", json={"grams": 0}).status_code == 422

    generic = add_food(db, "Beer, regular", 43)  # USDA rows are generic: no pack
    assert client.put(f"/api/foods/{generic.id}/package", json={"grams": 500}).status_code == 404
    assert client.put("/api/foods/999999/package", json={"grams": 500}).status_code == 404


def test_recognized_pack_reaches_the_draft(client, db, fake_model):
    beer = {"name_ru": "Пиво светлое", "name_en": "beer regular", "grams": 1000, "package_grams": 500,
            "kcal_100": 43, "protein_100": 0.5, "fat_100": 0, "carbs_100": 3.6,
            "fiber_100": 0, "sugar_100": 0, "salt_100": 0, "confidence": "high"}
    plate = {**RECOGNITION["items"][0], "package_grams": 0}
    fake_model["client"] = FakeClient(fake_response(json.dumps({"title": "Пиво", "items": [beer, plate], "comment": ""})))
    add_food(db, "Beer, regular, all", 43, 0.5, 0, 3.6)

    r = client.post("/api/recognize", data={"note": "две банки"})
    assert r.status_code == 200, r.text
    cans, other = r.json()["items"]
    assert cans["source"] == "usda" and cans["grams"] == 1000 and cans["package_grams"] == 500
    assert other["package_grams"] is None

    schema = fake_model["client"].kwargs["output_config"]["format"]["schema"]
    assert "package_grams" in schema["properties"]["items"]["items"]["required"]


def test_recipe_ingredients_have_no_pack():
    from app.recognize import _recipe_schema
    item = _recipe_schema()["properties"]["items"]["items"]
    assert "package_grams" not in item["properties"] and "package_grams" not in item["required"]
