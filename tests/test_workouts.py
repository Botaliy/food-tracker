"""Workouts and day activity from the coach chat: typed, or read off a watch screenshot."""

import base64
import csv
import io
import json
import zipfile

from fastapi.testclient import TestClient
from PIL import Image

from app import coach
from app.main import app
from app.models import Activity, ChatMessage, Workout
from tests.conftest import fake_response, make_user
from tests.test_app import _local_today
from tests.test_coach_actions import ask, coach_says, decide, tool_response  # noqa: F401  (fixture)

RUN = ("log_workout", {"kind": "Бег", "minutes": 30, "kcal": 0, "distance_km": 5, "avg_hr": 0, "time": "07:10"})
STEPS = ("log_activity", {"steps": 9500, "active_kcal": 0})


def png_b64() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def test_workout_accept_writes_once_with_unknowns_as_none(client, db, coach_says):
    coach_says(tool_response("Карточка ниже.", RUN))
    msg = ask(client, "пробежал 5 км за 30 минут")
    [action] = msg["actions"]
    assert action["type"] == "log_workout"
    assert action["summary"] == {"kind": "Бег", "minutes": 30, "kcal": None, "distance_km": 5, "avg_hr": None,
                                 "time": "07:10", "date": _local_today().isoformat()}
    assert db.query(Workout).count() == 0
    for _ in range(2):
        assert decide(client, msg, action).json()["actions"][0]["status"] == "done"
    [w] = db.query(Workout).all()
    assert (w.kind, w.minutes, w.kcal, w.distance_km, w.avg_hr, w.started, w.day) == (
        "Бег", 30, None, 5, None, "07:10", _local_today())

    day = client.get("/api/day").json()
    assert day["workouts"] == [{"id": w.id, "time": "07:10", "kind": "Бег", "minutes": 30, "kcal": None,
                                "distance_km": 5, "avg_hr": None}]

    coach_says(fake_response("Ок"))
    fake = coach._client()
    ask(client, "как тебе")
    sent = fake.kwargs["messages"][1]["content"]
    assert sent.endswith("[предложено: записать тренировку: Бег, 30 мин, 5 км — записано]")
    data = json.loads(fake.kwargs["system"][1]["text"].split("\n", 1)[1])
    assert data["workouts_last_7_days"] == [{"date": _local_today().isoformat(), "time": "07:10", "kind": "Бег",
                                             "minutes": 30, "kcal": None, "distance_km": 5.0, "avg_hr": None}]


def test_workout_decline_writes_nothing_and_invalid_is_dropped(client, db, coach_says):
    coach_says(tool_response("Ниже.", RUN))
    msg = ask(client)
    decide(client, msg, msg["actions"][0], accept=False)
    assert db.query(Workout).count() == 0

    zero = ("log_workout", {**RUN[1], "minutes": 0})
    no_kind = ("log_workout", {**RUN[1], "kind": "  "})
    hr = ("log_workout", {**RUN[1], "avg_hr": 400})
    nothing = ("log_activity", {"steps": 0, "active_kcal": 0})
    coach_says(tool_response("Вот.", zero, no_kind, hr))
    assert ask(client)["actions"] == []
    coach_says(tool_response("Вот.", nothing, ("log_activity", {"steps": 999999, "active_kcal": 0})))
    assert ask(client)["actions"] == []


def test_activity_upserts_and_keeps_the_other_field(client, db, coach_says):
    admin_id = client.get("/api/me").json()["id"]
    db.add(Activity(user_id=admin_id, day=_local_today(), active_kcal=410))
    db.commit()
    coach_says(tool_response("", STEPS))
    msg = ask(client)
    assert msg["actions"][0]["summary"] == {"steps": 9500, "active_kcal": None, "date": _local_today().isoformat()}
    decide(client, msg, msg["actions"][0])
    assert client.get("/api/day").json()["activity"] == {"active_kcal": 410, "steps": 9500}

    coach_says(tool_response("", ("log_activity", {"steps": 0, "active_kcal": 520.4})))
    msg = ask(client)
    decide(client, msg, msg["actions"][0])
    assert client.get("/api/day").json()["activity"] == {"active_kcal": 520, "steps": 9500}
    assert db.query(Activity).count() == 1


def test_screenshot_goes_to_the_model_once(client, db, coach_says):
    fake = coach_says(tool_response("Вижу пробежку.", RUN))
    r = client.post("/api/coach", json={"text": "", "image": png_b64()})
    assert r.status_code == 200, r.text
    mine = r.json()["messages"][0]
    assert mine["has_image"] is True and mine["text"] == "Посмотри, пожалуйста, скриншот"
    image, text = fake.kwargs["messages"][-1]["content"]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/jpeg"
    assert base64.b64decode(image["source"]["data"]).startswith(b"\xff\xd8")  # re-encoded as JPEG
    assert text == {"type": "text", "text": "Посмотри, пожалуйста, скриншот"}
    assert db.query(ChatMessage).filter(ChatMessage.has_image.is_(True)).count() == 1

    # A data: URL works too.
    coach_says(fake_response("Ок"))
    fake = coach._client()
    r = client.post("/api/coach", json={"text": "а это?", "image": f"data:image/png;base64,{png_b64()}"})
    assert r.status_code == 200, r.text
    first = fake.kwargs["messages"][0]["content"]
    assert first == f"Посмотри, пожалуйста, скриншот\n{coach.IMAGE_NOTE}"
    assert client.get("/api/coach").json()["messages"][0]["has_image"] is True


def test_bad_image_and_empty_message_are_400(client, db, coach_says):
    coach_says(fake_response("Ок"))
    for bad in ("not base64!!", base64.b64encode(b"just text").decode(), "data:text/plain;base64,aGk="):
        r = client.post("/api/coach", json={"text": "вот", "image": bad})
        assert r.status_code == 400 and r.json()["detail"] == "Не получилось прочитать изображение"
    assert client.post("/api/coach", json={"text": "  "}).status_code == 400
    assert client.post("/api/coach", json={}).status_code == 400
    assert db.query(ChatMessage).count() == 0


def test_delete_workout_only_own(client, db):
    admin_id = client.get("/api/me").json()["id"]
    bob = make_user(db, "bob")
    mine = Workout(user_id=admin_id, day=_local_today(), kind="Силовая", minutes=45)
    theirs = Workout(user_id=bob.id, day=_local_today(), kind="Бег", minutes=20)
    db.add_all([mine, theirs])
    db.commit()
    assert client.delete(f"/api/workouts/{theirs.id}").status_code == 404
    assert client.delete("/api/workouts/999999").status_code == 404
    assert client.delete(f"/api/workouts/{mine.id}").status_code == 200
    assert client.get("/api/day").json()["workouts"] == []
    with TestClient(app) as other:
        other.post("/api/auth/login", json={"username": "bob", "password": "password1"})
        assert [w["kind"] for w in other.get("/api/day").json()["workouts"]] == ["Бег"]


def test_export_has_workouts(client, db):
    admin_id = client.get("/api/me").json()["id"]
    db.add(Workout(user_id=admin_id, day=_local_today(), started="18:00", kind="Велосипед", minutes=60, kcal=480,
                   distance_km=22.5, avg_hr=135))
    db.commit()
    zf = zipfile.ZipFile(io.BytesIO(client.get("/api/export").content))
    rows = list(csv.reader(io.StringIO(zf.read("workouts.csv").decode("utf-8-sig"))))
    assert rows == [["date", "time", "kind", "minutes", "kcal", "distance_km", "avg_hr"],
                    [_local_today().isoformat(), "18:00", "Велосипед", "60", "480.0", "22.5", "135"]]


def test_menu_photo_keeps_its_small_print(client, db, coach_says):
    """A picture in the chat is read (a menu, a watch screen), so it goes in larger than a plate."""
    from app.images import MAX_IMAGE_EDGE, TEXT_IMAGE_EDGE
    buf = io.BytesIO()
    Image.new("RGB", (3000, 2000), "white").save(buf, format="JPEG")
    fake = coach_says(fake_response("Возьми лосося на гриле."))
    r = client.post("/api/coach", json={"text": "Что взять из этого меню?", "image": base64.b64encode(buf.getvalue()).decode()})
    assert r.status_code == 200, r.text
    assert r.json()["messages"][1]["actions"] == []
    sent = fake.kwargs["messages"][-1]["content"][0]["source"]["data"]
    assert max(Image.open(io.BytesIO(base64.b64decode(sent))).size) == TEXT_IMAGE_EDGE > MAX_IMAGE_EDGE
    assert "меню" in fake.kwargs["system"][0]["text"]
