"""«Итоги месяца»: a month of the diary counted into facts, and the push about it on the 1st."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app import recap, reminders
from tests.test_app import SUB, _local_today


def _eat(client, day, name, kcal, protein=10.0, guess=None):
    body = {"eaten_at": f"{day}T12:00:00", "items": [
        {"name": name, "grams": 100, "kcal_100": kcal, "protein_100": protein, "fat_100": 0, "carbs_100": 0}]}
    if guess is not None:
        body["guess_kcal"] = guess
    r = client.post("/api/meals", json=body)
    assert r.status_code == 200, r.text


def _last_month():
    first = recap.previous(_local_today().replace(day=1))
    return first, first.strftime("%Y-%m")


def test_recap_counts_the_month_and_nothing_around_it(client):
    first, ym = _last_month()
    goal = client.get("/api/me").json()["goals"]
    client.put("/api/me", json={**goal, "kcal": 800, "protein": 40, "timezone": "Europe/Berlin"})
    day = lambda n: first + timedelta(days=n - 1)
    _eat(client, first - timedelta(days=1), "Суп", 800)          # the month before
    _eat(client, day(1), "Плов", 800, protein=30)                # in goal
    _eat(client, day(2), "Плов", 400, protein=20, guess=440)     # in goal with the kebab, protein 60
    _eat(client, day(2), "Шашлык", 400, protein=40, guess=600)
    _eat(client, day(3), "плов", 300, guess=300)                 # too little
    _eat(client, day(10), "Борщ", 900)                           # over
    client.put("/api/weight", json={"kg": 80.4, "date": day(2).isoformat()})
    client.put("/api/weight", json={"kg": 79.1, "date": day(10).isoformat()})

    r = client.get(f"/api/recap?month={ym}").json()
    assert r["month"] == ym and r["complete"] is True
    assert r["prev"] == recap.previous(first).strftime("%Y-%m") and r["next"] == _local_today().strftime("%Y-%m")
    assert (r["logged_days"], r["longest_run"], r["meals"]) == (4, 3, 5)
    assert (r["in_goal_days"], r["protein_days"]) == (2, 1)
    assert r["avg_kcal"] == round((800 + 800 + 300 + 900) / 4)
    assert r["best_day"] == {"date": day(2).isoformat(), "kcal": 800, "protein": 60}
    assert r["top"][0] == {"name": "Плов", "times": 3}           # case doesn't split a dish
    assert {t["name"] for t in r["top"]} == {"Плов", "Шашлык", "Борщ"}
    assert r["weight"] == {"from": 80.4, "to": 79.1, "delta": -1.3}
    assert r["guess"] == {"n": 3, "off_percent": 10}             # misses 10, 50, 0
    assert r["photos"] == []

    # Without a month the app gets the one that just ended.
    assert client.get("/api/recap").json()["month"] == ym
    assert client.get("/api/recap?month=2999-01").status_code == 400
    assert client.get("/api/recap?month=2026-13").status_code == 422


def test_young_diary_gets_the_month_going_on(client):
    today = _local_today()
    _eat(client, today, "Плов", 500)
    r = client.get("/api/recap").json()
    assert r["month"] == today.strftime("%Y-%m") and r["complete"] is False
    assert (r["logged_days"], r["prev"], r["next"]) == (1, None, None)
    assert r["days_total"] == today.day


def test_collage_is_spread_over_the_month():
    assert recap._spread(list(range(5)), 9) == list(range(5))
    picked = recap._spread(list(range(30)), 9)
    assert len(picked) == 9 and picked[0] == 0 and picked[-1] == 29 and picked == sorted(set(picked))


def test_monthly_push_on_the_first(client, db):
    first, _ = _last_month()
    the_first = recap.following(first)
    at = lambda day, hour: datetime(day.year, day.month, day.day, hour, 5, tzinfo=ZoneInfo("Europe/Berlin"))
    notify = {"evening": False, "hour": 21, "weekly": False, "share_streak": False}
    client.put("/api/profile/notify", json=notify)  # an old app says nothing about `monthly`: it stays on
    assert client.get("/api/profile").json()["notify"]["monthly"] is True
    client.post("/api/push/subscribe", json=SUB)
    for n in range(3):
        _eat(client, first + timedelta(days=n), "Плов", 500)
    sent = []
    fake_send = lambda db_, user, title, body, url="/": sent.append((title, body, url)) or 1

    assert reminders.run_due(db, at(the_first - timedelta(days=1), 12), fake_send) == []
    assert reminders.run_due(db, at(the_first, 9), fake_send) == []
    assert [k for _, k in reminders.run_due(db, at(the_first, 10), fake_send)] == ["monthly"]
    assert reminders.run_due(db, at(the_first, 11), fake_send) == []
    title, body, url = sent[0]
    assert title == f"📅 Итоги {recap.MONTHS_GEN[first.month - 1]}" and url == "/#recap"
    assert "Дней с записями: 3" in body and "плов" in body


def test_no_monthly_push_when_off_or_for_an_empty_month(client, db):
    first, _ = _last_month()
    the_first = datetime.combine(recap.following(first), datetime.min.time(), ZoneInfo("Europe/Berlin")).replace(hour=10)
    client.post("/api/push/subscribe", json=SUB)
    notify = {"evening": False, "hour": 21, "weekly": False, "share_streak": False}
    client.put("/api/profile/notify", json=notify)
    fake_send = lambda *a, **k: 1
    _eat(client, first, "Плов", 500)  # one day isn't a month to recap
    assert reminders.run_due(db, the_first, fake_send) == []

    from app.models import Profile
    for n in (1, 2):
        _eat(client, first + timedelta(days=n), "Плов", 500)
    client.put("/api/profile/notify", json={**notify, "monthly": False})
    db.expire_all()
    db.get(Profile, client.get("/api/me").json()["id"]).last_monthly_push = None
    db.commit()
    assert reminders.run_due(db, the_first, fake_send) == []
