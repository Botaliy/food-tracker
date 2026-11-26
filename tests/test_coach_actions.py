"""The coach proposes diary writes as tool calls; only the user's tap applies them."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import coach
from app.main import app
from app.models import ApiCall, ChatMessage, Meal, Water, Weight
from tests.conftest import add_food, fake_response, make_user
from tests.test_app import FakeClient, _local_today


def tool_response(text, *calls, stop_reason="tool_use"):
    r = fake_response(text, stop_reason=stop_reason)
    blocks = [SimpleNamespace(type="text", text=text)] if text else []
    blocks += [SimpleNamespace(type="tool_use", id=f"toolu_{j}", name=name, input=inp) for j, (name, inp) in enumerate(calls)]
    r.content = blocks
    return r


def item(name_ru, name_en, grams, kcal, protein=0.0, fat=0.0, carbs=0.0):
    return {"name_ru": name_ru, "name_en": name_en, "grams": grams, "kcal_100": kcal, "protein_100": protein,
            "fat_100": fat, "carbs_100": carbs, "fiber_100": 0, "sugar_100": 0, "salt_100": 0}


OMELETTE = ("log_meal", {"title": "Омлет и кофе", "time": "08:15", "items": [
    item("Омлет", "egg whole cooked omelet", 165, 150, 10, 11, 1),
    item("Кофе с молоком", "coffee brewed with milk", 200, 30, 1.5, 1.5, 2.5),
]})
GOALS = ("set_goals", {"kcal": 1840, "protein": 162, "fat": 65, "carbs": 152, "reason": "Расход ~2300 ккал, худеешь мягко."})
WATER = ("add_water", {"ml": 250})
WEIGHT = ("log_weight", {"kg": 72.44})


@pytest.fixture
def coach_says(monkeypatch):
    holder = {}

    def set_reply(response):
        holder["client"] = FakeClient(response)
        return holder["client"]

    monkeypatch.setattr(coach, "_client", lambda: holder["client"])
    return set_reply


def ask(client, text="запиши"):
    r = client.post("/api/coach", json={"text": text})
    assert r.status_code == 200, r.text
    return r.json()["messages"][1]


def decide(client, msg, action, accept=True):
    return client.post(f"/api/coach/actions/{msg['id']}/{action['id']}", json={"accept": accept})


def test_tools_are_strict_and_sent(client, coach_says):
    fake = coach_says(tool_response("Карточка ниже.", OMELETTE))
    ask(client)
    tools = {t["name"]: t for t in fake.kwargs["tools"]}
    assert set(tools) == {"log_meal", "set_goals", "add_water", "log_weight"}
    for t in tools.values():
        assert t["strict"] is True and t["input_schema"]["additionalProperties"] is False
    assert "expenditure" in json.loads(fake.kwargs["system"][1]["text"].split("\n", 1)[1])


def test_meal_proposal_uses_database_numbers_and_accepts_once(client, db, coach_says):
    add_food(db, "Egg, whole, cooked, omelet", 154, 10.6, 11.7, 0.6)
    coach_says(tool_response("Записать омлет и кофе? Карточка ниже.", OMELETTE))
    msg = ask(client)
    assert msg["text"] == "Записать омлет и кофе? Карточка ниже."
    [action] = msg["actions"]
    assert action["type"] == "log_meal" and action["status"] == "pending"
    s = action["summary"]
    assert s["title"] == "Омлет и кофе" and s["time"] == "08:15"
    assert s["items"] == [{"name": "Омлет", "grams": 165}, {"name": "Кофе с молоком", "grams": 200}]
    # The omelette is the database's 154 kcal/100 g, not the model's 150.
    assert s["totals"]["kcal"] == round(154 * 1.65 + 30 * 2)
    assert db.query(Meal).count() == 0  # nothing written yet

    r = decide(client, msg, action)
    assert r.status_code == 200 and r.json()["actions"][0]["status"] == "done"
    again = decide(client, msg, action)
    assert again.status_code == 200 and again.json()["actions"][0]["status"] == "done"
    assert decide(client, msg, action, accept=False).json()["actions"][0]["status"] == "done"

    day = client.get("/api/day").json()
    assert len(day["meals"]) == 1
    meal = day["meals"][0]
    assert meal["eaten_at"].startswith(f"{_local_today().isoformat()}T08:15")
    assert round(meal["totals"]["kcal"]) == s["totals"]["kcal"]
    assert meal["items"][0]["food_id"] is not None and meal["items"][0]["ai_grams"] is None
    assert day["streaks"]["today"] is True  # the regular meal path: streaks see it
    # The history the model gets says what happened; the bubble text doesn't.
    assert client.get("/api/coach").json()["messages"][1]["text"] == msg["text"]
    coach_says(fake_response("Ок"))
    fake = coach._client()
    ask(client, "спасибо")
    sent = fake.kwargs["messages"][1]["content"]
    assert sent.startswith(msg["text"]) and sent.endswith(f"[предложено: записать «Омлет и кофе», {s['totals']['kcal']} ккал — записано]")


def test_goals_water_weight_accept_and_decline(client, db, coach_says):
    coach_says(tool_response("", GOALS, WATER, WEIGHT))
    msg = ask(client)
    assert msg["text"] == coach.NO_TEXT_REPLY
    goals, water, weight = msg["actions"]
    assert goals["summary"] == {"old": {"kcal": 2000, "protein": 120, "fat": 70, "carbs": 220},
                                "new": {"kcal": 1840, "protein": 162, "fat": 65, "carbs": 152},
                                "reason": "Расход ~2300 ккал, худеешь мягко."}
    assert water["summary"]["ml"] == 250 and weight["summary"]["kg"] == 72.4

    assert decide(client, msg, goals).status_code == 200
    assert decide(client, msg, goals).status_code == 200
    assert client.get("/api/me").json()["goals"] == {"kcal": 1840, "protein": 162, "fat": 65, "carbs": 152}

    for _ in range(2):
        assert decide(client, msg, water).status_code == 200
    assert client.get("/api/day").json()["water"]["ml"] == 250

    r = decide(client, msg, weight, accept=False)
    assert [a["status"] for a in r.json()["actions"]] == ["done", "done", "declined"]
    assert decide(client, msg, weight).json()["actions"][2]["status"] == "declined"
    assert db.query(Weight).count() == 0

    coach_says(fake_response("Ок"))
    fake = coach._client()
    ask(client, "ну как")
    sent = fake.kwargs["messages"][1]["content"]
    assert "изменить норму на 1840 ккал (Б 162 · Ж 65 · У 152) — записано]" in sent
    assert "добавить воду 250 мл — записано]" in sent and "записать вес 72,4 кг — отклонено]" in sent


def test_decline_writes_nothing_and_pending_shows_in_history(client, db, coach_says):
    coach_says(tool_response("Карточка ниже.", OMELETTE, WATER))
    msg = ask(client)
    meal_action, water_action = msg["actions"]
    decide(client, msg, meal_action, accept=False)
    assert db.query(Meal).count() == 0 and db.query(Water).count() == 0
    coach_says(fake_response("Ок"))
    fake = coach._client()
    ask(client, "дальше")
    sent = fake.kwargs["messages"][1]["content"]
    assert "— отклонено]" in sent and "добавить воду 250 мл — ждёт подтверждения]" in sent


def test_someone_elses_proposal_is_404(client, db, coach_says):
    coach_says(tool_response("Карточка ниже.", WATER))
    msg = ask(client)
    make_user(db, "bob")
    with TestClient(app) as other:
        other.post("/api/auth/login", json={"username": "bob", "password": "password1"})
        r = decide(other, msg, msg["actions"][0])
        assert r.status_code == 404
    assert client.post(f"/api/coach/actions/{msg['id']}/nope", json={"accept": True}).status_code == 404
    assert client.post(f"/api/coach/actions/999999/x", json={"accept": True}).status_code == 404
    assert db.query(Water).count() == 0


def test_invalid_proposals_are_dropped_and_extra_ones_ignored(client, db, coach_says):
    low = ("set_goals", {**GOALS[1], "kcal": 800})
    zero = ("log_meal", {"title": "Ничего", "time": "", "items": [item("Воздух", "air", 0, 0)]})
    bad_time = ("log_meal", {**OMELETTE[1], "time": "25:99"})
    coach_says(tool_response("Вот.", low, zero, bad_time, WATER, WEIGHT))
    msg = ask(client)
    assert msg["actions"] == []  # the first three were all invalid; the 4th and 5th are past the cap
    assert msg["text"] == "Вот."

    coach_says(tool_response("", WATER, WATER, WATER, WATER, WATER))
    assert len(ask(client)["actions"]) == 3

    coach_says(tool_response("", ("add_water", {"ml": -200}), ("log_weight", {"kg": 5}), ("delete_meal", {})))
    msg = ask(client)
    assert msg["actions"] == [] and msg["text"] == "Не получилось подготовить запись — напиши подробнее, что именно записать."
    assert db.query(ApiCall).count() == 3  # billed once per message, nothing extra


def test_meal_without_time_is_logged_at_acceptance(client, coach_says):
    coach_says(tool_response("Ниже.", ("log_meal", {**OMELETTE[1], "time": ""})))
    msg = ask(client)
    assert msg["actions"][0]["summary"]["time"] is None
    decide(client, msg, msg["actions"][0])
    [meal] = client.get("/api/day").json()["meals"]
    assert meal["eaten_at"].startswith(_local_today().isoformat())


def test_text_only_reply_and_refusal_unchanged(client, db, coach_says):
    coach_says(fake_response("Просто совет."))
    msg = ask(client)
    assert msg["actions"] == [] and msg["text"] == "Просто совет."
    assert db.query(ChatMessage).filter(ChatMessage.actions.is_not(None)).count() == 0

    coach_says(tool_response("", WATER, stop_reason="refusal"))
    r = client.post("/api/coach", json={"text": "ещё"})
    assert r.status_code == 502 and "не может" in r.json()["detail"]
    assert db.query(ChatMessage).count() == 2 and db.query(ApiCall).count() == 2

    # A cut-off reply keeps its text and proposes nothing.
    coach_says(tool_response("Длинный отв", WATER, stop_reason="max_tokens"))
    msg = ask(client)
    assert msg["text"] == "Длинный отв…" and msg["actions"] == []
