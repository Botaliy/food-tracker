"""«Угадай калории»: a guess made before the recognition answers, scored against what was logged."""

from tests.conftest import make_user


def _meal(client, kcal, guess=None, title="Обед"):
    body = {"title": title, "items": [{"name": "Плов", "grams": 100, "kcal_100": kcal, "protein_100": 5, "fat_100": 5, "carbs_100": 20}]}
    if guess is not None:
        body["guess_kcal"] = guess
    r = client.post("/api/meals", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_guess_is_kept_and_scored_by_the_median_miss(client):
    assert client.get("/api/day").json()["streaks"]["guess"] is None
    assert _meal(client, 500)["guess_kcal"] is None  # a meal without a guess doesn't count
    meal = _meal(client, 500, guess=450)             # 10% off
    assert meal["guess_kcal"] == 450
    assert client.get("/api/day").json()["streaks"]["guess"] == {"n": 1, "off_percent": 10}
    _meal(client, 400, guess=600)                    # 50%
    _meal(client, 800, guess=640)                    # 20%
    assert client.get("/api/day").json()["streaks"]["guess"] == {"n": 3, "off_percent": 20}

    # Fixing the portion afterwards moves the truth, not the guess; an edit can't add or change a guess.
    item = {"name": "Плов", "grams": 90, "kcal_100": 500, "protein_100": 5, "fat_100": 5, "carbs_100": 20}
    r = client.put(f"/api/meals/{meal['id']}", json={"title": "Обед", "items": [item], "guess_kcal": 9999})
    assert r.json()["guess_kcal"] == 450 and r.json()["totals"]["kcal"] == 450
    assert client.get("/api/day").json()["streaks"]["guess"] == {"n": 3, "off_percent": 20}  # misses 0, 50, 20

    for bad in (0, -5, 20000):
        body = {"title": "", "items": [item], "guess_kcal": bad}
        assert client.post("/api/meals", json=body).status_code == 422


def test_guess_score_is_shared_only_after_a_few_plates(client, db):
    notify = {"evening": True, "hour": 21, "weekly": True, "share_streak": True}
    assert client.put("/api/profile/notify", json=notify).status_code == 200
    _meal(client, 500, guess=500)
    _meal(client, 500, guess=550)
    me = client.get("/api/streaks").json()["people"][0]
    assert me["me"] and me["guess"] is None  # two lucky plates are not a score
    _meal(client, 500, guess=600)
    assert client.get("/api/streaks").json()["people"][0]["guess"] == {"n": 3, "off_percent": 10}
