"""Badges, the evening reserve, the weekly duel, the weight outlook on Reports and breakfasts for breakfast."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app import badges, coach, diary, main as app_main, reminders
from app.models import User
from tests.conftest import make_user
from tests.test_app import SUB, _local_today

BERLIN = ZoneInfo("Europe/Berlin")
NOTIFY = {"evening": False, "hour": 21, "weekly": False, "monthly": False, "share_streak": True}


def _eat(client, when, name="Плов", kcal=500, protein=10.0, guess=None, title=""):
    body = {"title": title, "eaten_at": when, "items": [
        {"name": name, "grams": 100, "kcal_100": kcal, "protein_100": protein, "fat_100": 0, "carbs_100": 0}]}
    if guess is not None:
        body["guess_kcal"] = guess
    r = client.post("/api/meals", json=body)
    assert r.status_code == 200, r.text


def _badges(client):
    return {b["key"]: b for b in client.get("/api/badges").json()["badges"]}


def test_badges_come_from_the_diary(client):
    b = _badges(client)
    assert not any(x["earned"] for x in b.values())
    assert (b["plates_100"]["value"], b["plates_100"]["target"]) == (0, 100)

    goals = client.get("/api/me").json()["goals"]
    client.put("/api/me", json={**goals, "kcal": 500, "protein": 40, "timezone": "Europe/Berlin"})
    today = _local_today()
    for k in range(7):                       # a week in a row, in goal, with protein, guessed right
        _eat(client, f"{today - timedelta(days=k)}T12:00:00", protein=40, guess=520 if k < 3 else None)
    b = _badges(client)
    assert {k for k, x in b.items() if x["earned"]} == {"first", "run_7", "goal_7", "protein_7", "sniper"}
    assert (b["run_30"]["value"], b["plates_100"]["value"], b["photo_50"]["value"]) == (7, 7, 0)

    _eat(client, f"{today}T13:00:00", guess=900)  # a wild guess doesn't take the badge back
    assert _badges(client)["sniper"]["earned"] and not _badges(client)["goal_7"]["earned"]  # but the day is over goal now


def test_full_month_badge_needs_every_day_of_a_finished_month():
    from datetime import date
    october = {date(2026, 10, d) for d in range(1, 32)}
    assert badges._full_months(october, date(2026, 11, 2)) == 1
    assert badges._full_months(october - {date(2026, 10, 9)}, date(2026, 11, 2)) == 0
    assert badges._full_months(october, date(2026, 10, 31)) == 0  # the month isn't over


def test_reserve_lives_for_today_only(client):
    assert client.get("/api/day").json()["reserve"] == 0
    assert client.put("/api/reserve", json={"kcal": 600}).json() == {"reserve": 600}
    assert client.get("/api/day").json()["reserve"] == 600
    yesterday = _local_today() - timedelta(days=1)
    assert client.get(f"/api/day?date={yesterday}").json()["reserve"] == 0
    assert client.put("/api/reserve", json={"kcal": 0}).status_code == 200
    assert client.get("/api/day").json()["reserve"] == 0
    for bad in (-1, 3001):
        assert client.put("/api/reserve", json={"kcal": bad}).status_code == 422


def test_coach_is_told_about_the_reserve(client, db):
    client.put("/api/reserve", json={"kcal": 600})
    today = coach.snapshot(db, db.query(User).one())["today"]
    assert today["reserved_for_evening_kcal"] == 600 and 0 <= today["hour_local"] <= 23
    assert "reserved_for_evening_kcal" in coach.SYSTEM and "холодильник" in coach.SYSTEM.lower()


def test_stats_carry_the_weight_outlook(client):
    assert client.get("/api/stats").json()["outlook"] is None
    client.put("/api/weight", json={"kg": 80})
    assert client.get("/api/stats").json()["outlook"]["last"]["kg"] == 80


def test_recent_meals_put_this_part_of_the_day_first(client):
    today = _local_today()
    for k in (1, 2, 3):
        _eat(client, f"{today - timedelta(days=k)}T13:00:00", name="Борщ", title="Борщ")     # lunches
    for k in (1, 2):
        _eat(client, f"{today - timedelta(days=k)}T08:00:00", name="Овсянка", title="Каша")  # breakfasts
    _eat(client, f"{today - timedelta(days=1)}T19:30:00", name="Рыба", title="Рыба")         # one dinner

    titles = lambda part="": [(m["title"], m.get("part_count")) for m in client.get(f"/api/meals/recent{part}").json()]
    assert titles() == [("Борщ", 0), ("Каша", 0), ("Рыба", 0)]            # no part: plain frequency
    assert titles("?part=breakfast") == [("Каша", 2), ("Борщ", 0), ("Рыба", 0)]
    assert titles("?part=dinner") == [("Рыба", 1), ("Борщ", 0), ("Каша", 0)]
    assert titles("?part=snack") == [("Борщ", 0), ("Каша", 0), ("Рыба", 0)]
    assert client.get("/api/meals/recent?part=brunch").status_code == 422


def _two_sharers(client, db):
    client.put("/api/profile/notify", json=NOTIFY)
    make_user(db, "alina")
    other = TestClient(app_main.app)
    other.post("/api/auth/login", json={"username": "alina", "password": "password1"})
    other.put("/api/profile/notify", json=NOTIFY)
    return other


def test_duel_of_the_week_counts_good_days_and_tells_the_winner(client, db):
    monday = diary.week_start(_local_today())
    last = monday - timedelta(days=7)
    metric, title = diary.duel_metric(last)
    assert client.get("/api/streaks").json().get("duel") is None  # not sharing
    other = _two_sharers(client, db)
    for c in (client, other):  # the default goals differ from nobody's: set them alike
        goals = c.get("/api/me").json()["goals"]
        c.put("/api/me", json={**goals, "kcal": 500, "protein": 40, "timezone": "Europe/Berlin"})
    good = dict(kcal=500, protein=40)  # in goal and with protein: counts whatever the week's metric
    for k in range(3):
        _eat(other, f"{last + timedelta(days=k)}T12:00:00", **good)
    _eat(client, f"{last}T12:00:00", **good)
    _eat(client, f"{last + timedelta(days=1)}T12:00:00", kcal=100, protein=1)  # logged, but not a good day

    result = diary.duel(db, db.query(User).all(), last)
    assert (result["metric"], result["title"]) == (metric, title)
    assert [(p["name"], p["score"]) for p in result["people"]] == [("alina", 3), ("admin", 1)]

    duel = client.get("/api/streaks").json()["duel"]  # the week going on: nobody has scored yet, or today only
    assert duel["from"] == monday.isoformat() and {p["name"] for p in duel["people"]} == {"admin", "alina"}
    assert [p["me"] for p in duel["people"] if p["name"] == "admin"] == [True]

    client.post("/api/push/subscribe", json=SUB)
    sent = []
    fake_send = lambda db_, user, title_, body, url="/": sent.append((user.username, title_, body)) or 1
    at = lambda hour: datetime(monday.year, monday.month, monday.day, hour, 5, tzinfo=BERLIN)
    assert reminders.run_due(db, at(9), fake_send) == []
    assert [k for _, k in reminders.run_due(db, at(10), fake_send)] == ["duel"]
    assert reminders.run_due(db, at(11), fake_send) == []
    assert sent == [("admin", "🏆 Дуэль недели", f"Победил(а) alina: 3 {title}. У тебя — 1. Новая неделя — новая дуэль.")]
    other.close()


def test_no_duel_push_alone_or_for_an_empty_week(client, db):
    monday = diary.week_start(_local_today())
    assert reminders.duel_text(db, db.query(User).one(), monday) is None
    other = _two_sharers(client, db)
    me = db.query(User).filter_by(username="admin").one()
    assert reminders.duel_text(db, me, monday) is None  # two people, nobody scored
    other.close()
